from __future__ import annotations

import asyncio
import logging

from .realtime_controller import RealtimeController
from .episode_output import EpisodeSpeechOutput
from ..agent.episode import AgentEpisodeRuntime
from ..core.events import VoiceEvent, utc_now_iso
from ..core.state import TerminalState


logger = logging.getLogger(__name__)


class EpisodeController(RealtimeController):
    def __init__(self, *, agent_options, prefill_enabled=True, **kwargs):
        super().__init__(**kwargs)
        self.agent_options = agent_options
        self.prefill_enabled = prefill_enabled
        self.output = EpisodeSpeechOutput(self)
        self.episode = self._make_episode()
        self._endpoint_context = None
        self._barge_trace_state = None
        self.episode_history: dict[str, str] = {}
        self._input_history_committed: set[str] = set()

    def _make_episode(self):
        runtime = AgentEpisodeRuntime(self.llm, self.registry, self.output, self._agent_event,
                                      conversation=self.conversation, ledger=self.history,
                                      options=self.agent_options, tools_enabled=self.options.tools,
                                      prefill_enabled=self.prefill_enabled)
        self.output.runtime = runtime
        return runtime

    async def _agent_event(self, kind, payload):
        # Only an authoritative Episode snapshot may replace the compact
        # current-state projection.  Late trace/output events deliberately
        # retain their own source identity and must remain history-only.
        if kind == "agent.episode":
            self.state.agent = dict(payload)
        elif kind == "speech.completed":
            # The session ledger is the only durable conversational memory.
            # A speech record enters it only after the player reports what the
            # user actually heard.
            self.history.commit_delivery(str(payload.get("speech_id") or ""), str(payload.get("text") or ""),
                                         payload.get("delivery") if isinstance(payload.get("delivery"), dict) else None,
                                         termination=str(payload.get("status") or "completed"))
        await self._publish(VoiceEvent(kind, payload.get("input_id") or payload.get("turn_id") or self.episode.last_turn_id, payload))

    async def start_listening(self):
        if self.episode.closed:
            self.episode = self._make_episode()
        await super().start_listening()

    def _begin_tool_scope(self, turn_id):
        pass  # Tools belong to the episode, not the audio turn.

    def _reset_tool_scope(self, turn_id):
        pass

    def _cancel_tool_scope(self, turn_id):
        pass

    async def _detail(self, kind, **payload):
        await super()._detail(kind, **payload)
        ws = self.workspace
        if not ws:
            return
        if kind == "transcript.segment":
            await self.episode.observe("USER_SEGMENT_UPDATED", turn_id=ws.turn_id,
                                       text="".join(s["text"] for s in ws.segments),
                                       epoch=ws.epoch)
        elif kind == "turn.endpoint_candidate":
            key = (ws.turn_id, ws.epoch)
            if key != self._endpoint_context:
                self._endpoint_context = key
                await self.episode.observe("ENDPOINT_CANDIDATE", turn_id=ws.turn_id)

    async def _observe_activity(self, activity, frame):
        if not self.state.listening or self.episode.closed:
            return None
        if self.output.active and self.output.handle.workspace.playback_started:
            decision = self.barge_in_gate.feed(activity, now=frame.captured_at,
                                               near_voice=self._near_voice_activity(),
                                               protected_available=self._barge_in_protected_available())
            h = self.output.handle
            progress = self.player.progress(h.workspace.playback_key) if self.player else {}
            evidence = {"identity": h.identity, "turn_id": h.turn_id, "speech_id": h.speech_id,
                        "epoch": h.workspace.epoch, "vad_speech": bool(activity.is_speech),
                        "near_voice": self._near_voice_activity(),
                        "protected_available": self._barge_in_protected_available(),
                        "duration_ms": decision.duration_ms, "reason": decision.reason,
                        "played_ms": progress.get("played_ms"), "remaining_ms": progress.get("remaining_ms")}
            marker = (decision.candidate, decision.confirmed, decision.reason)
            previous = self._barge_trace_state
            if decision.confirmed and marker != previous:
                await self.episode.log("BARGE_IN_CONFIRMED", **evidence)
                logger.info("episode.barge_in kind=confirmed speech_id=%s vad_speech=%s near_voice=%s protected_available=%s duration_ms=%s reason=%s played_ms=%s remaining_ms=%s",
                            h.speech_id, evidence["vad_speech"], evidence["near_voice"], evidence["protected_available"],
                            evidence["duration_ms"], evidence["reason"], evidence["played_ms"], evidence["remaining_ms"])
            elif decision.candidate and not (previous and previous[0]):
                await self.episode.log("BARGE_IN_CANDIDATE_STARTED", **evidence)
                logger.info("episode.barge_in kind=candidate_started speech_id=%s vad_speech=%s near_voice=%s protected_available=%s duration_ms=%s reason=%s",
                            h.speech_id, evidence["vad_speech"], evidence["near_voice"], evidence["protected_available"],
                            evidence["duration_ms"], evidence["reason"])
            elif not decision.candidate and previous and previous[0]:
                await self.episode.log("BARGE_IN_CANDIDATE_CLEARED", **evidence)
                logger.info("episode.barge_in kind=candidate_cleared speech_id=%s duration_ms=%s reason=%s",
                            h.speech_id, evidence["duration_ms"], evidence["reason"])
            elif bool(activity.is_speech) and decision.reason in {"not_near_voice", "protected_audio_unavailable"} and marker != previous:
                await self.episode.log("BARGE_IN_REJECTED", **evidence)
                logger.info("episode.barge_in kind=rejected speech_id=%s vad_speech=%s near_voice=%s protected_available=%s duration_ms=%s reason=%s",
                            h.speech_id, evidence["vad_speech"], evidence["near_voice"], evidence["protected_available"],
                            evidence["duration_ms"], evidence["reason"])
            self._barge_trace_state = marker
            if not decision.confirmed:
                # Keep microphone/VAD running; echo/noise must not create a user turn.
                return None
            if self.player:
                progress = self.player.progress(h.workspace.playback_key)
                h.overlap_start_ms = max(0, int(progress.get("output_estimated_ms", 0)) - decision.duration_ms)
            await self.episode.speech_activity(True)
            self.barge_in_gate.reset()
            self._barge_trace_state = None
        else:
            await self.episode.speech_activity(bool(activity.is_speech))
        return activity

    async def _input_committed(self, ws, token):
        if ws.final_text:
            await self.episode.observe("USER_INPUT_FINALIZED", turn_id=ws.turn_id,
                                       text=ws.paraformer_final or ws.final_text,
                                       provisional_transcript=ws.paraformer_final, audio_cues=list(ws.audio_cues),
                                       analysis=dict(ws.analysis), epoch=ws.epoch,
                                       input_finished_at=ws.timings.get("commit"))
            ws.episode_id = self.episode.episode_id
            if ws.episode_id:
                self.episode_history[ws.turn_id] = ws.episode_id
            if not self._valid(token):
                return True
            if ws.paraformer_final and ws.paraformer_final != ws.final_text:
                await self.episode.observe("USER_INPUT_REFINED", turn_id=ws.turn_id, text=ws.final_text,
                                           audio_cues=list(ws.audio_cues), analysis=dict(ws.analysis),
                                           input_finished_at=ws.timings.get("commit"))
                ws.episode_id = self.episode.episode_id
                if ws.episode_id:
                    self.episode_history[ws.turn_id] = ws.episode_id
        await self._finish_v2(ws, "")
        return True

    async def _ensure_workspace_episode(self, ws):
        if ws.episode_id:
            self.episode_history[ws.turn_id] = ws.episode_id
            return ws.episode_id
        text = ws.final_text or ws.paraformer_final or ws.raw_partial or "".join(
            segment.get("text", "") for segment in ws.segments
        )
        if text.strip():
            await self.episode.ensure_episode(turn_id=ws.turn_id, text=text,
                                               audio_cues=list(ws.audio_cues),
                                               analysis=dict(ws.analysis))
            ws.episode_id = self.episode.episode_id
        if ws.episode_id:
            self.episode_history[ws.turn_id] = ws.episode_id
        return ws.episode_id

    async def _finish_v2(self, ws, response):
        if self.workspace is not ws:
            return
        await self._ensure_workspace_episode(ws)
        delivery = {"status": "none", "termination": "completed", "heard_text": "",
                    "uncertain_text": "", "unheard_text": "", "precision": "complete"}
        if ws.turn_id not in self._input_history_committed:
            self.history.commit_input(ws.turn_id, ws.final_text, details={"segments": list(ws.segments),
                                      "audio_cues": list(ws.audio_cues), "analysis": dict(ws.analysis)})
            record = self.history.append_record({"turn_id": ws.turn_id, "transcript": ws.final_text,
                                                 "delivery": delivery, "conversation_id": ws.episode_id,
                                                 "details": {"segments": list(ws.segments), "provisional_transcript": ws.paraformer_final,
                                                             "audio_cues": list(ws.audio_cues), "analysis": dict(ws.analysis)}})
            self._input_history_committed.add(ws.turn_id)
        else:
            record = next((item for item in self.history.records if item.turn_id == ws.turn_id), None)
        await self._detail("turn.completed", transcript=ws.final_text, response="", latency={},
                           delivery=delivery, status="completed", conversation_id=ws.episode_id,
                           completed_at=utc_now_iso(), details=record.details if record else {})
        await self._cancel_turn(reason="completed", record=False)
        await self._state_event(TerminalState.IDLE, listening=self.state.listening)

    async def _cancel_turn(self, *, reason="stopped", snapshot=None, record=True):
        # Input cleanup never blocks/cancels the independently owned output.
        ws = self.workspace
        if record and ws and ws.turn_id not in self._input_history_committed:
            await self._ensure_workspace_episode(ws)
            self.history.commit_input(ws.turn_id, ws.final_text or ws.raw_partial)
            record = self.history.append_record({
                "turn_id": ws.turn_id, "transcript": ws.final_text or ws.raw_partial,
                "delivery": {"termination": reason, "status": "none"},
                "conversation_id": ws.episode_id,
            })
            self._input_history_committed.add(ws.turn_id)
            await self._detail("turn.completed", **{k: v for k, v in record.to_payload().items()
                                                     if k != "turn_id"})
        self.workspace = None
        self.turns.invalidate()
        self._active_input = False
        stopped = self._stop_generation()
        if stopped:
            await asyncio.gather(*stopped, return_exceptions=True)
        for sub_id in self._sub_parts:
            try:
                self._queue_asr(sub_id)
            except asyncio.QueueFull:
                pass
        self._sub_parts.clear()
        self._sub_finals.clear()
        self._resume_started = None
        self._speech_samples = 0
        self._speech_start_sample = self._speech_end_sample = None
        self._vad_origin = None
        self._vad_epoch += 1
        self._reset_vad_pending = True

    def _on_playback_started(self, key):
        self.output.playback_started(key)

    async def _handle_asr_disconnect(self):
        await self.episode.speech_activity(None)
        self.output.invalidate("input_unavailable")
        await super()._handle_asr_disconnect()

    async def _error(self, code, message, *, recoverable, turn_id=None):
        if not recoverable or code == "AUDIO_PIPELINE_FAILED":
            if self.workspace is not None:
                await self._cancel_turn(reason="failed", record=True)
            await self.episode.end("fatal_error")
            await self.episode.speech_activity(None)
        await super()._error(code, message, recoverable=recoverable, turn_id=turn_id)

    async def stop_listening(self):
        self.state.listening = False
        if self.workspace is not None:
            await self._cancel_turn(reason="stopped", record=True)
        if not self.episode.closed:
            await self.episode.stop()
        await super().stop_listening()

    async def reset_session(self):
        """End the active Agent task while keeping microphone listening alive."""
        if self.workspace is not None:
            await self._cancel_turn(reason="reset", record=True)
        if not self.episode.closed:
            await self.episode.end("reset")
        self._endpoint_context = None
        self.barge_in_gate.reset()
        if self.vad is not None:
            self.vad.reset()
        if self.audio_processor is not None:
            self.audio_processor.reset()
        self.state.clear_current()
        self.state.agent = {}
        await self._state_event(TerminalState.IDLE, listening=self.state.listening)

    async def close(self):
        if self.workspace is not None:
            await self._cancel_turn(reason="stopped", record=True)
        if not self.episode.closed:
            await self.episode.stop()
        await super().close()

from __future__ import annotations

import asyncio
import logging
import time

from .realtime_controller import RealtimeController
from .episode_output import EpisodeSpeechOutput
from ..agent.episode import AgentEpisodeRuntime
from ..core.events import VoiceEvent, event, utc_now_iso
from ..core.state import TerminalState
from ..session.contracts import DeliveryReport


logger = logging.getLogger(__name__)


class EpisodeController(RealtimeController):
    def __init__(self, *, agent_options, persona_config=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_options = agent_options
        self.persona_config = persona_config
        self.output = EpisodeSpeechOutput(self)
        self.episode = self._make_episode()
        self._endpoint_context = None
        self._barge_trace_state = None
        self._aec_recovery_task: asyncio.Task | None = None
        self.episode_history: dict[str, str] = {}
        self._input_history_committed: set[str] = set()
        self._refinement_events: set[str] = set()
        self._refinement_agent_events: set[str] = set()
        self._committed_workspaces: dict[str, object] = {}
        self._settled_turns: set[str] = set()

    def _make_episode(self):
        mode = getattr(self, "reasoning_mode", "balanced")
        runtime = AgentEpisodeRuntime(self.llm, self.registry, self.output, self._agent_event,
                                      conversation=self.conversation, ledger=self.history,
                                      options=self.agent_options, tools_enabled=True,
                                       shadow_waker=self.shadow_waker,
                                       reasoning_mode=mode, persona_config=self.persona_config,
                                       on_final_system_check_settled=self._final_system_check_settled)
        self.output.runtime = runtime
        return runtime

    async def _on_kws_activated(self, source_id: str, generation: int) -> bool:
        try:
            await self.episode.schedule_system_check(
                "kws_wake", source_id, arm_silence=True,
                controller_generation=generation,
            )
        except Exception as exc:
            logger.exception("controller.kws_wake_schedule_failed")
            await self._error("KWS_WAKE_SCHEDULE_FAILED", str(exc), recoverable=True)
        # The acknowledgement is advisory.  Once ASR readiness has succeeded,
        # an LLM/TTS scheduling failure must not close the protected input path.
        return True

    async def _final_system_check_settled(self, context: dict) -> None:
        scheduler_generation = context.get("generation")
        activation_generation = self._activation_generation

        async def return_when_input_is_idle() -> None:
            while (self.state.listening and self.state.input_owner == "asr"
                   and activation_generation == self._activation_generation):
                if (isinstance(scheduler_generation, int)
                        and scheduler_generation != self.episode.scheduler.generation):
                    return
                if self.workspace is None and self.episode.user_speaking is not True:
                    await self._return_to_kws(activation_generation)
                    return
                await asyncio.sleep(0.05)

        task = asyncio.create_task(
            return_when_input_is_idle(), name="final-system-check-kws-return",
        )
        self._track(task)

    def set_reasoning_mode(self, mode: str) -> None:
        self.reasoning_mode = mode
        # Before the first user input there is no frozen schema yet, so make
        # the staged selection effective immediately without touching work.
        if self.episode.episode_id is None and not self.episode.closed:
            self.episode = self._make_episode()

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
            delivery = DeliveryReport.from_payload(
                payload.get("delivery") if isinstance(payload.get("delivery"), dict) else None,
                termination=str(payload.get("status") or "completed"),
            )
            speech_id = str(payload.get("speech_id") or "")
            added = self.history.commit_delivery(
                speech_id, str(payload.get("text") or ""), delivery,
            )
            await self.episode.log(
                "SPEECH_HISTORY_COMMITTED",
                identity=(payload.get("conversation_id"), payload.get("request_id")),
                turn_id=str(payload.get("turn_id") or ""),
                speech_id=speech_id,
                added=added,
                history_present=self.history.has_speech(speech_id),
            )
            logger.info(
                "speech.history_committed speech_id=%s turn_id=%s added=%s history_present=%s",
                speech_id, payload.get("turn_id"), added, self.history.has_speech(speech_id),
            )
            if payload.get("owner_scope") != "system_check":
                await self._settle_committed_turn(
                    str(payload.get("turn_id") or ""),
                    outcome=str(payload.get("status") or "completed"),
                    speech_id=str(payload.get("speech_id") or "") or None,
                    latency=payload.get("latency") if isinstance(payload.get("latency"), dict) else None,
                )
        elif kind == "turn.latency":
            latency = payload.get("latency")
            update = getattr(self.history, "update_turn_latency", None)
            if (payload.get("owner_scope") != "system_check"
                    and callable(update) and isinstance(latency, dict)):
                update(str(payload.get("turn_id") or ""), latency)
            if payload.get("owner_scope") != "system_check" and isinstance(latency, dict):
                current_values = dict(self.state.latency.get("values") or {})
                for key, value in latency.items():
                    if key in current_values and (value is None or isinstance(value, (int, float))):
                        current_values[key] = value
                self.state.latency = {"complete": True, "values": current_values}
        elif kind == "agent.turn_settled":
            await self._settle_committed_turn(
                str(payload.get("turn_id") or ""),
                outcome=str(payload.get("outcome") or "completed"),
            )
        await self._publish(VoiceEvent(kind, payload.get("input_id") or payload.get("turn_id") or self.episode.last_turn_id, payload))
        # Do not infer turn settlement from a snapshot which happens to say
        # ``agent_busy=False``.  There is a hand-off window between input
        # retirement and LLM dispatch where that snapshot is stale, while the
        # Bidi TTS prewarm is still connecting.  Cancelling it here closes the
        # warm socket immediately before the first speak can check it out and
        # turns the critical path back into a cold start.  The authoritative
        # terminal paths (speech.completed / agent.turn_settled, cancellation,
        # reset, and close) all dispose the prewarm explicitly.

    async def start_listening(self):
        if self.episode.closed:
            self.episode = self._make_episode()
        # Warm the shared DeepSeek transport on session activation.  This is
        # only a lightweight /models request (DNS/TCP/TLS/auth connection
        # reuse); it never starts model inference or consumes a scheduler
        # slot.  A slow/unavailable prewarm must not block microphone start.
        prewarm = getattr(self.llm, "prewarm", None)
        if callable(prewarm):
            try:
                await asyncio.wait_for(prewarm(), timeout=2.0)
                await self.episode.log("LLM_PREWARM_READY")
            except asyncio.TimeoutError:
                await self.episode.log("LLM_PREWARM_FALLBACK", reason="timeout")
            except Exception as exc:
                await self.episode.log("LLM_PREWARM_FALLBACK", reason=str(exc)[:200])
        await super().start_listening()

    async def accept_shadow_events(self, events) -> None:
        for item in events:
            await self.episode.accept_shadow_event(item)

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
                                       output_generation=ws.generation, sentences=list(ws.segments),
                                       sentence_ids=list(payload.get("changed_ids") or []))
        elif kind == "turn.endpoint_candidate":
            key = (ws.turn_id, ws.generation)
            if key != self._endpoint_context:
                self._endpoint_context = key
                await self.episode.observe("ENDPOINT_CANDIDATE", turn_id=ws.turn_id)

    async def _observe_activity(self, activity, frame):
        if not self.state.listening or self.episode.closed:
            return None
        handle = getattr(self.output, "handle", None)
        if self.output.active and handle and handle.workspace.playback_started:
            progress = self._playback_progress(handle)
            # The output transaction deliberately remains active for a brief
            # append window after TTS and PCM have drained.  That bookkeeping
            # window is not audible output: classifying later user speech as
            # barge-in would incorrectly settle a fully delivered sentence as
            # ``interrupted/full``.  Preserve the handle for same-turn suffix
            # appends, but return VAD ownership to the normal input path once
            # the player's conservative device-tail estimate is empty.
            if self.player and int(progress.get("remaining_ms", 0) or 0) <= 0:
                self.barge_in_gate.reset()
                self._barge_trace_state = None
                await self.episode.speech_activity(bool(activity.is_speech))
                return activity
            protected = self._barge_in_protected_available()
            if not protected:
                await self._handle_aec_unavailable(handle)
                # Never hand a playing-output frame to normal input handling:
                # AEC-unavailable audio is neither trustworthy user speech nor
                # eligible raw-VAD fallback.
                return None
            decision = self.barge_in_gate.feed(activity, now=frame.captured_at,
                                               near_voice=self._near_voice_activity(),
                                               protected_available=protected)
            h = handle
            evidence = {"identity": h.identity, "turn_id": h.turn_id, "speech_id": h.speech_id,
                        "output_generation": h.workspace.generation, "vad_speech": bool(activity.is_speech),
                        "near_voice": self._near_voice_activity(),
                        "protected_available": protected,
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
            elif bool(activity.is_speech) and decision.reason in {"not_near_voice", "near_voice_unknown", "protected_audio_unavailable"} and marker != previous:
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
            await self.episode.speech_activity(True, interrupt_reason="user_interrupted")
            self.barge_in_gate.reset()
            self._barge_trace_state = None
        else:
            await self.episode.speech_activity(bool(activity.is_speech))
        return activity

    async def _handle_aec_unavailable(self, handle) -> None:
        """Apply the explicit, non-raw-VAD fallback while audio is audible."""
        policy = getattr(self, "aec_failure_policy", "pause_recover")
        processor = self.audio_processor
        status = getattr(processor, "status", "UNAVAILABLE") if processor else "UNAVAILABLE"
        error = getattr(processor, "error", None) if processor else "processor_missing"
        if policy == "continue_without_barge_in":
            await self.episode.log("AEC_BARGE_IN_SUPPRESSED", policy=policy, status=status, error=error)
            return
        if policy == "stop_and_listen":
            await self.episode.log("AEC_FAILED", policy=policy, status=status, error=error)
            self.output.cancel_all("input_unavailable")
            self.barge_in_gate.reset()
            return
        task = self._aec_recovery_task
        if task is not None and not task.done():
            return
        pause = getattr(self.player, "pause", None)
        if not callable(pause) or not pause(handle.workspace.playback_key):
            await self.episode.log("AEC_FAILED", policy="stop_and_listen", status=status,
                                   error="unable_to_pause_playback")
            self.output.cancel_all("input_unavailable")
            return
        await self.episode.log("AEC_RECOVERING", policy=policy, status=status, error=error)
        self._aec_recovery_task = asyncio.create_task(
            self._recover_aec_playback(handle.workspace.playback_key), name="aec-playback-recovery")

    async def _recover_aec_playback(self, playback_key: str) -> None:
        processor = self.audio_processor
        try:
            recover = getattr(processor, "recover", None)
            ok = bool(recover and await asyncio.to_thread(recover))
            if ok:
                # The rebuilt processor is not protected until it has received
                # a fresh reverse callback.  Paused playback keeps callbacks
                # producing aligned silence references during this short wait.
                deadline = time.monotonic() + 0.75
                while time.monotonic() < deadline and not self._barge_in_protected_available():
                    await asyncio.sleep(0.02)
                if self._barge_in_protected_available() and self.player and self.player.resume(playback_key):
                    self.barge_in_gate.reset()
                    await self.episode.log("AEC_RECOVERED", recovery_ms=getattr(processor, "last_recovery_ms", None),
                                           recovery_count=getattr(processor, "recovery_count", None))
                    return
            await self.episode.log("AEC_FAILED", policy="stop_and_listen",
                                   status=getattr(processor, "status", "FAILED"),
                                   error=getattr(processor, "error", "recovery_timeout"))
            self.output.cancel_all("input_unavailable")
            self.barge_in_gate.reset()
        finally:
            self._aec_recovery_task = None

    def _playback_progress(self, handle):
        """Return player progress with a conservative audible-tail estimate.

        ``PcmPlayer.progress`` intentionally exposes raw queue and output
        positions.  Barge-in needs the derived remaining duration so it does
        not confuse the output layer's post-playback append window with audio
        the listener can still hear.
        """

        if not self.player:
            return {}
        progress = dict(self.player.progress(handle.workspace.playback_key) or {})
        try:
            if "enqueued_ms" not in progress or "output_estimated_ms" not in progress:
                raise ValueError("playback progress is incomplete")
            enqueued = int(progress.get("enqueued_ms", 0) or 0)
            output_estimated = int(progress.get("output_estimated_ms", 0) or 0)
            progress["remaining_ms"] = max(0, enqueued - output_estimated)
        except (TypeError, ValueError):
            # Preserve the existing conservative behavior if an alternate
            # player implementation cannot provide numeric progress.
            progress["remaining_ms"] = 1
        return progress

    async def _input_committed(self, ws, token):
        if ws.final_text:
            if not any(item.get("kind") == "asr_final" for item in ws.transcript_updates):
                ws.transcript_updates.append({
                    "kind": "asr_final", "source": "paraformer",
                    "text": ws.paraformer_final or ws.final_text, "revision": 1,
                })
            await self.episode.observe("USER_INPUT_FINALIZED", turn_id=ws.turn_id,
                                       text=ws.paraformer_final or ws.final_text,
                                       source="paraformer", source_revision=ws.generation,
                                       provisional_transcript=ws.paraformer_final, audio_cues=list(ws.audio_cues),
                                       analysis=dict(ws.analysis), output_generation=ws.generation,
                                       input_finished_at=ws.timings.get("commit"))
            ws.episode_id = self.episode.episode_id
            if ws.episode_id:
                self.episode_history[ws.turn_id] = ws.episode_id
            # Refinement that completed just before ASR final is applied only
            # after the primary input is marked final.  A late refinement
            # arriving afterwards uses the same hook below.
            if ws.refinement_available and ws.refined_text:
                await self._refinement_completed(ws, {
                    "text": ws.refined_text,
                    "segments": list(ws.audio_cues),
                })
        # Finalized input is not a completed dialogue turn.  Retire only the
        # capture workspace so ASR resources are released while Agent/TTS and
        # late SenseVoice refinement retain the same turn correlation.
        await self._commit_input_workspace(ws)
        if not ws.final_text:
            await self._settle_committed_turn(ws.turn_id, outcome="empty_input")
        await self._retire_committed_input(ws)
        return True

    async def _commit_input_workspace(self, ws):
        """Persist the primary transcript once, without publishing a terminal state."""

        await self._ensure_workspace_episode(ws)
        if ws.turn_id in self._input_history_committed:
            return next((item for item in self.history.records if item.turn_id == ws.turn_id), None)
        self.history.commit_input(ws.turn_id, ws.final_text, details={
            "segments": list(ws.segments), "audio_cues": list(ws.audio_cues),
            "analysis": dict(ws.analysis), "refined_text": ws.refined_text,
            "refinement_available": ws.refinement_available,
            "transcript_updates": [dict(item) for item in ws.transcript_updates],
        })
        record = self.history.append_record({
            "turn_id": ws.turn_id, "transcript": ws.final_text,
            "delivery": {"status": "pending", "termination": "pending"},
            "conversation_id": ws.episode_id,
            "details": {"segments": list(ws.segments), "provisional_transcript": ws.paraformer_final,
                        "audio_cues": list(ws.audio_cues), "analysis": dict(ws.analysis),
                        "refined_text": ws.refined_text,
                        "refinement_available": ws.refinement_available,
                        "transcript_updates": [dict(item) for item in ws.transcript_updates]},
        })
        self._input_history_committed.add(ws.turn_id)
        self._committed_workspaces[ws.turn_id] = ws
        if len(self._committed_workspaces) > 64:
            self._committed_workspaces.pop(next(iter(self._committed_workspaces)), None)
        return record

    async def _retire_committed_input(self, ws):
        """Release capture-only state after commit without cancelling output work."""

        if self.workspace is not ws:
            return
        self.workspace = None
        self._active_input = False
        if self._completion_task is asyncio.current_task():
            self._completion_task = None
        self._endpoint_task = None
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
        self._vad_generation += 1
        self._reset_vad_pending = True

    async def _settle_committed_turn(self, turn_id, *, outcome, speech_id=None, latency=None):
        """Emit one terminal record for a committed turn after its true outcome."""

        if not turn_id or turn_id in self._settled_turns:
            return
        ws = self._committed_workspaces.get(turn_id)
        if ws is None:
            return
        self._settled_turns.add(turn_id)
        record = next((item for item in self.history.records if item.turn_id == turn_id), None)
        # Formal speech owns its measured latency and emits it immediately
        # after speech.completed.  Control-only/empty-input paths have no
        # SpeechHandle, so settlement itself produces their single final
        # latency snapshot.
        if latency is None:
            ws.timings.setdefault("completed", time.monotonic())
            timings = ws.timings

            def elapsed(end, start):
                left = timings.get(end)
                right = ws.started_at if start == "start" else timings.get(start)
                return max(0, round((left - right) * 1000)) if left is not None and right is not None else None

            latency = {
                "wake_detection_ms": None,
                "asr_first_text_ms": elapsed("asr_first_text", "start"),
                "speech_end_confirmation_ms": elapsed("endpoint_accepted", "last_speech"),
                "asr_final_to_commit_ms": elapsed("commit", "asr_final"),
                "llm_first_token_ms": elapsed("first_token", "llm_request"),
                "llm_request_to_first_tts_text_ms": elapsed("first_tts_text", "llm_request"),
                "tts_text_to_first_audio_ms": elapsed("first_audio", "first_tts_text"),
                "first_audio_to_playback_ms": elapsed("playback", "first_audio"),
                "speech_end_to_playback_ms": elapsed("playback", "last_speech"),
                "turn_total_ms": elapsed("completed", "start"),
                "first_segment_ms": elapsed("first_segment", "start"),
                "smart_turn_ms": timings.get("smart_turn_ms"),
                "sensevoice_refine_ms": timings.get("sensevoice_refine_ms"),
                "refine_duration_ms": timings.get("sensevoice_refine_ms"),
                "refine_to_redecision_ms": elapsed("refinement_redecision_started", "refinement_completed"),
                "agent_request_to_speak_ms": elapsed("speak_requested", "llm_request"),
                "tts_prewarm_requested_ms": elapsed("tts_prewarm_requested", "start"),
                "tts_prewarm_ready_ms": elapsed("tts_prewarm_ready", "tts_prewarm_requested"),
                "tts_checkout_ms": elapsed("tts_prewarm_checkout", "tts_prewarm_requested"),
                "tts_cold_start_ms": elapsed("tts_session_ready", "tts_session_start")
                if not timings.get("tts_prewarm_used") else None,
                "speech_end_to_first_pcm_ms": elapsed("first_pcm_submitted", "last_speech"),
                "first_pcm_to_player_submit_ms": elapsed("first_pcm_submitted", "first_audio"),
                "replacement_count": int(timings.get("replacement_count", 0) or 0),
            }
            self.state.latency = {"complete": True, "values": dict(latency)}
            turn_latency = {
                "turn_id": turn_id,
                "input_id": turn_id,
                "speech_id": None,
                "asr_first_text_ms": latency.get("asr_first_text_ms"),
                "speech_end_confirmation_ms": latency.get("speech_end_confirmation_ms"),
                "asr_final_ms": None,
                "finalized_ms": None,
                "llm_first_token_ms": latency.get("llm_first_token_ms"),
                "speak_requested_ms": None,
                "tts_first_audio_ms": None,
                "player_first_pcm_submitted_ms": None,
                "refinement_ms": ws.timings.get("sensevoice_refine_ms"),
                "prewarm_status": getattr(ws, "tts_prewarm_status", None),
                "replacement_count": int(ws.timings.get("replacement_count", 0) or 0),
            }
            await self._publish(event("turn.latency", turn_id, input_id=turn_id,
                                      speech_id=None, turn=turn_latency, latency=latency))
        if record is not None:
            record.delivery = {"status": outcome, "termination": outcome}
            if isinstance(latency, dict):
                record.details["latency"] = dict(latency)
        await self._publish(VoiceEvent("turn.completed", turn_id, {
            "turn_id": turn_id, "input_id": turn_id, "speech_id": speech_id,
            "transcript": ws.final_text, "status": outcome,
            "conversation_id": ws.episode_id, "completed_at": utc_now_iso(),
            "details": record.details if record else {},
        }))
        self._committed_workspaces.pop(turn_id, None)
        if self.conversation.current_turn_id == turn_id and self.workspace is None and not self.output.active:
            await self._state_event(TerminalState.IDLE, listening=self.state.listening)
        await self._cancel_tts_prewarm(turn_id)

    async def _refinement_completed(self, ws, result: dict) -> None:
        """Attach SenseVoice evidence to one finalized turn and wake Agent once."""

        text = str(result.get("text") or "").strip()
        if not text:
            return
        if self.wake_enabled:
            from ..kws.keywords import strip_wake_prefix
            text = strip_wake_prefix(text, self.wake_keyword)
        ws.refined_text = text
        ws.refinement_available = True
        prior_refinement = next(
            (item for item in ws.transcript_updates
             if isinstance(item, dict) and item.get("kind") == "refinement"),
            None,
        )
        if prior_refinement is None:
            existing_revision = max(
                [int(item.get("revision") or 0) for item in ws.transcript_updates
                 if isinstance(item, dict)] or [0]
            )
            ws.transcript_updates.append({
                "kind": "refinement", "source": "sensevoice", "text": text,
                "revision": max(2, existing_revision + 1),
            })
        segments = list(result.get("segments") or [])
        if segments:
            ws.audio_cues = segments
        ws.timings.setdefault("refinement_completed", time.monotonic())

        if ws.turn_id not in self._refinement_events:
            self._refinement_events.add(ws.turn_id)
            if len(self._refinement_events) > 128:
                self._refinement_events = set(list(self._refinement_events)[-64:])
            payload = {
                "text": text,
                "source": "sensevoice",
                "workspace": ws.snapshot(),
            }
            if self.workspace is ws:
                await self._detail("transcript.refined", text=text, source="sensevoice")
                await self._detail("speech.emotion", status="READY", source="SenseVoiceSmall",
                                    segments=segments)
            else:
                await self._publish(event("transcript.refined", ws.turn_id, output_generation=ws.generation, **payload))
                await self._publish(event("speech.emotion", ws.turn_id, output_generation=ws.generation,
                                           status="READY", source="SenseVoiceSmall", segments=segments,
                                           workspace=ws.snapshot()))

        # Do not let a refinement that wins the race with finalization start a
        # provisional Agent decision.  The final observer below will call this
        # hook again once the input record is finalized.
        source = self.episode.inputs.get(ws.turn_id, {}) if self.episode.episode_id else {}
        if not source.get("final"):
            return
        if ws.turn_id in self._refinement_agent_events:
            return
        self._refinement_agent_events.add(ws.turn_id)
        updater = getattr(self.history, "update_input_refinement", None)
        if callable(updater):
            updater(ws.turn_id, text, details={"audio_cues": segments, "analysis": dict(ws.analysis)})
        if self.episode.closed or self.episode.episode_id is None:
            return
        # A late refinement remains an ordinary scheduler event even when a
        # newer capture turn is already active.  It is attached to its own
        # sentence/turn record; no semantic-version check may discard it.
        await self.episode.observe("USER_INPUT_REFINED", turn_id=ws.turn_id, text=text,
                                    source="sensevoice", source_revision=ws.generation,
                                    audio_cues=segments, analysis=dict(ws.analysis),
                                    input_finished_at=ws.timings.get("commit"), output_generation=ws.generation,
                                    sentences=list(ws.segments))
        ws.episode_id = self.episode.episode_id
        if ws.episode_id:
            self.episode_history[ws.turn_id] = ws.episode_id

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
                                      "audio_cues": list(ws.audio_cues), "analysis": dict(ws.analysis),
                                      "refined_text": ws.refined_text,
                                      "refinement_available": ws.refinement_available})
            record = self.history.append_record({"turn_id": ws.turn_id, "transcript": ws.final_text,
                                                 "delivery": delivery, "conversation_id": ws.episode_id,
                                                 "details": {"segments": list(ws.segments), "provisional_transcript": ws.paraformer_final,
                                                             "audio_cues": list(ws.audio_cues), "analysis": dict(ws.analysis),
                                                             "refined_text": ws.refined_text,
                                                             "refinement_available": ws.refinement_available}})
            self._input_history_committed.add(ws.turn_id)
        else:
            record = next((item for item in self.history.records if item.turn_id == ws.turn_id), None)
        # A turn without an output still needs a terminal timestamp.  If a
        # speech transaction is later created, EpisodeSpeechOutput replaces
        # this provisional value with the actual output-settlement time.
        ws.timings.setdefault("completed", time.monotonic())
        await self._latency(ws, complete=True)
        latency = dict(self.state.latency.get("values") or {})
        def since_turn(key):
            return round((ws.timings[key] - ws.timings["turn_started"]) * 1000) \
                if key in ws.timings and "turn_started" in ws.timings else None

        def elapsed(start, end):
            return round((ws.timings[end] - ws.timings[start]) * 1000) \
                if start in ws.timings and end in ws.timings else None

        turn_latency = {
            "turn_id": ws.turn_id,
            "input_id": ws.turn_id,
            "speech_id": None,
            "asr_first_text_ms": since_turn("asr_first_text"),
            "speech_end_confirmation_ms": elapsed("last_speech", "endpoint_accepted"),
            "asr_final_ms": since_turn("asr_final"),
            "finalized_ms": since_turn("commit"),
            "llm_first_token_ms": since_turn("first_token"),
            "speak_requested_ms": since_turn("speak_requested"),
            "tts_first_audio_ms": since_turn("first_audio"),
            "player_first_pcm_submitted_ms": since_turn("first_pcm_submitted"),
            "refinement_ms": ws.timings.get("sensevoice_refine_ms"),
            "prewarm_status": ws.tts_prewarm_status,
            "prewarm_used": bool(ws.timings.get("tts_prewarm_used")),
            "replacement_count": int(ws.timings.get("replacement_count", 0) or 0),
        }
        latency.update({"turn_id": ws.turn_id, "input_id": ws.turn_id,
                        "speech_id": None, "prewarm_status": ws.tts_prewarm_status,
                        "replacement_count": int(ws.timings.get("replacement_count", 0) or 0)})
        if record is not None:
            record.details["latency"] = dict(latency)
            record.details["turn_latency"] = dict(turn_latency)
        await self._publish(event("turn.latency", ws.turn_id,
                                  turn_id=ws.turn_id, input_id=ws.turn_id,
                                  speech_id=None, turn=turn_latency, latency=latency))
        await self._detail("turn.completed", transcript=ws.final_text, response="", latency=latency,
                           delivery=delivery, status="completed", conversation_id=ws.episode_id,
                           completed_at=utc_now_iso(), details=record.details if record else {})
        await self._cancel_turn(reason="completed", record=False)
        await self._state_event(TerminalState.IDLE, listening=self.state.listening)

    async def _cancel_turn(self, *, reason="stopped", snapshot=None, record=True):
        # Input cleanup never blocks/cancels the independently owned output.
        ws = self.workspace
        if reason != "completed":
            await self._cancel_tts_prewarm(ws.turn_id if ws else None)
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
        if reason != "completed":
            self.episode.cancel_active_request(reason)
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
        self._vad_generation += 1
        self._reset_vad_pending = True

    def _on_playback_started(self, key):
        self.output.playback_started(key)

    async def _handle_asr_disconnect(self):
        await self.episode.speech_activity(None, interrupt_reason="input_unavailable")
        self.output.cancel_all("input_unavailable")
        await super()._handle_asr_disconnect()

    async def _error(self, code, message, *, recoverable, turn_id=None):
        if not recoverable or code == "AUDIO_PIPELINE_FAILED":
            if self.workspace is not None:
                await self._cancel_turn(reason="failed", record=True)
            await self.episode.end("fatal_error")
            await self.episode.speech_activity(None, interrupt_reason="input_unavailable")
        await super()._error(code, message, recoverable=recoverable, turn_id=turn_id)

    async def stop_listening(self):
        self.state.listening = False
        if self.workspace is not None:
            await self._cancel_turn(reason="stopped", record=True)
        if not self.episode.closed:
            await self.episode.speech_activity(None, interrupt_reason="stopped")
            await self.episode.stop()
        await self._cancel_tts_prewarm()
        close_tts = getattr(self.tts, "close", None)
        if callable(close_tts):
            await close_tts()
        await super().stop_listening()

    async def reset_session(self):
        """End the active Agent task while keeping microphone listening alive."""
        if self.workspace is not None:
            await self._cancel_turn(reason="reset", record=True)
        if not self.episode.closed:
            await self.episode.speech_activity(None, interrupt_reason="reset")
            await self.episode.end("reset")
        await self.output.close()
        await self._cancel_tts_prewarm()
        close_tts = getattr(self.tts, "close", None)
        if callable(close_tts):
            await close_tts()
        self._endpoint_context = None
        self.barge_in_gate.reset()
        if self.vad is not None:
            self.vad.reset()
        if self.audio_processor is not None:
            self.audio_processor.reset()
        self.state.clear_current()
        self.state.agent = {}
        if self.wake_always_on and self.state.listening:
            if self.state.input_owner == "asr":
                await self._return_to_kws(self._activation_generation)
                return
            if self.kws is not None:
                self.kws.reset()
            await self._reset_owner_boundary()
        await self._state_event(TerminalState.IDLE, listening=self.state.listening)

    async def close(self):
        if self.workspace is not None:
            await self._cancel_turn(reason="stopped", record=True)
        if not self.episode.closed:
            await self.episode.speech_activity(None, interrupt_reason="stopped")
            await self.episode.stop()
        await self._cancel_tts_prewarm()
        close_tts = getattr(self.tts, "close", None)
        if callable(close_tts):
            await close_tts()
        await super().close()

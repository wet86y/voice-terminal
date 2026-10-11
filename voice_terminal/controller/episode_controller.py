from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from uuid import uuid4

from .realtime_controller import RealtimeController
from .episode_output import EpisodeSpeechOutput
from .turn_workspace import AudioFrame
from ..agent.episode import AgentEpisodeRuntime
from ..core.events import VoiceEvent, event, utc_now_iso
from ..core.state import TerminalState
from ..session.contracts import DeliveryReport
from ..session.conversation_log import USER_TURN, speech_kind
from ..speaker.rolling import RollingSpeakerIdentity
from ..speaker.contracts import SpeakerState


logger = logging.getLogger(__name__)


class EpisodeController(RealtimeController):
    def __init__(self, *, agent_options, persona_config=None,
                 custom_instructions_provider=None,
                 continuity_document_provider=None, on_user_activity=None,
                 on_silence_anchor=None, **kwargs):
        self.quiet_policy = kwargs.pop("quiet_policy", None)
        self.deferred_capture = kwargs.pop("deferred_capture", None)
        self.deferred_append = kwargs.pop("deferred_append", None)
        super().__init__(**kwargs)
        self.agent_options = agent_options
        self.persona_config = persona_config
        self.custom_instructions_provider = custom_instructions_provider
        self.continuity_document_provider = continuity_document_provider
        self.on_user_activity = on_user_activity
        self.on_silence_anchor = on_silence_anchor
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
        self._owner_barge_candidate_id: str | None = None
        self._owner_barge_start_sample: int | None = None
        self._owner_barge_started_at: float | None = None
        self._owner_barge_output_key: str | None = None
        self._owner_barge_expired = False
        self._early_silence_task: asyncio.Task | None = None  # 提前静默请求：保留到进入 KWS（关键词唤醒）待机。

    async def request_early_silence(self) -> str:
        """Queue the final check now; dispatch and KWS handover remain boundary-safe."""
        if not self.wake_always_on or self.kws is None:
            return "kws_disabled"
        if self.kws.error is not None or not self.kws.running:
            return "kws_unavailable"
        if not self.state.listening:
            return "not_listening"
        if self.state.input_owner != "asr":
            return "not_asr"
        if self.episode.closed:
            return "unavailable"
        if self.episode.scheduler.final_dispatched:
            return "already_finishing"
        task = self._early_silence_task
        if task is not None and not task.done():
            return "already_pending"
        self._early_silence_task = asyncio.create_task(
            self._advance_early_silence(), name="early-silence-request",
        )
        self._track(self._early_silence_task)
        # Let the task immediately mark an active ladder before acknowledging.
        # The background task remains alive to carry the request across later
        # input generations until KWS (关键词唤醒) owns the microphone again.
        await asyncio.sleep(0)
        return "scheduled"

    async def _cancel_early_silence(self) -> None:
        task = getattr(self, "_early_silence_task", None)
        self._early_silence_task = None
        was_pending = task is not None and task is not asyncio.current_task() and not task.done()
        if was_pending:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if was_pending:
            # Stopping/resetting input must not leave an early final check
            # behind to fire if the same Episode is later resumed.
            scheduler = getattr(getattr(self, "episode", None), "scheduler", None)
            if (scheduler is not None and scheduler.final_pending
                    and not scheduler.final_dispatched):
                scheduler.disarm()

    async def _advance_early_silence(self) -> None:
        activation_generation = self._activation_generation
        episode = self.episode
        marked_generation: int | None = None
        try:
            while (self.state.listening and self.state.input_owner == "asr"
                   and activation_generation == self._activation_generation
                   and episode is self.episode and not episode.closed):
                scheduler = episode.scheduler
                if scheduler.final_dispatched:
                    await asyncio.sleep(0.05)
                    continue
                if not scheduler.armed and not scheduler.is_deferred():
                    await episode.arm_silence_window(
                        f"early-silence-{uuid4().hex}", anchor_source="early_silence",
                    )
                # Marking the final check is immediate and independent of
                # live ASR (语音识别), Agent (代理) or TTS (语音合成) work.
                # Actual dispatch is separately gated by the runtime so no
                # current user input or playback is interrupted.
                if not scheduler.armed:
                    await asyncio.sleep(0.05)
                    continue
                if not scheduler.anchor_turn_id:
                    # System checks need a source even before the first user turn.
                    scheduler.anchor_turn_id = f"early-silence-{uuid4().hex}"
                if (marked_generation != scheduler.generation
                        and (scheduler.mark_final_pending() or scheduler.final_pending)):
                    episode._ensure_runner()
                    marked_generation = scheduler.generation
                    await episode.log(
                        "SCHEDULER_FINAL_PENDING", window_index=scheduler.FINAL_WINDOW,
                        reason="early_silence_request", scheduler_generation=scheduler.generation,
                    )
                await asyncio.sleep(0.05)
        finally:
            if self._early_silence_task is asyncio.current_task():
                self._early_silence_task = None

    def _make_episode(self):
        mode = getattr(self, "reasoning_mode", "deep")
        runtime = AgentEpisodeRuntime(self.llm, self.registry, self.output, self._agent_event,
                                      conversation=self.conversation, ledger=self.history,
                                      options=self.agent_options, tools_enabled=True,
                                       shadow_waker=self.shadow_waker,
                                       reasoning_mode=mode, persona_config=self.persona_config,
                                       custom_instructions_provider=self.custom_instructions_provider,
                                       defer_silence_until_output=self.wake_always_on,
                                       on_final_system_check_settled=self._final_system_check_settled,
                                       can_dispatch_final_silence=self._final_silence_dispatch_ready,
                                       continuity_document_provider=self.continuity_document_provider,
                                       on_user_activity=self.on_user_activity,
                                        on_silence_anchor=self.on_silence_anchor,
                                        on_speaker_heartbeat=self._speaker_heartbeat_pulse,
                                        admit_user_turn=self._speaker_admit_turn,
                                        defer_user_turn=self._speaker_defer_turn)
        runtime.quiet_policy = self.quiet_policy
        runtime.deferred_capture = self.deferred_capture
        runtime.deferred_append = self.deferred_append
        self.output.runtime = runtime
        return runtime

    def _speaker_admit_turn(self, turn_id: str) -> bool:
        ws = self.workspace
        if ws is not None and ws.turn_id == turn_id and ws.speaker_routing_finalized:
            if ws.speaker_mode != "filter":
                return True
            decision = ws.speaker_decision or {}
            return bool(
                not ws.speaker_blocked
                and decision.get("turn_id") == ws.turn_id
                and decision.get("generation") == ws.generation
                and decision.get("state") == "owner_confirmed"
                and decision.get("route") == "main"
                and not decision.get("degraded", False)
            )
        if self.speaker_vetoes_turn(turn_id):
            late = getattr(self.episode, "active_request_source_turn_id", None) == turn_id
            logger.info("speaker.request_veto turn_id=%s reason=rolling_non_owner late=%s",
                        turn_id, late)
            return False
        return True

    def _speaker_defer_turn(self, turn_id: str) -> bool:
        """filter（拦截模式）下将本轮全部未结算身份暂缓至最终路由。"""
        ws = self.workspace
        if (ws is None or ws.turn_id != turn_id or ws.speaker_mode != "filter"
                or ws.speaker_routing_finalized):
            return False
        # rolling（滚动）和 router（路由器）状态都可能在后续窗口中变化。
        # 临时 NON_OWNER（非主人）若直接进入 admit（准入）否决，会让 Agent
        # 删除尚未最终结算的输入，之后即使本轮聚合确认主人也无法恢复该待办。
        return True

    def _speaker_heartbeat_pulse(self, pulse) -> None:
        if self.speaker_sensor is None:
            return
        loop = asyncio.get_running_loop()
        interval = float(self.episode.scheduler.interval)
        next_heartbeat = pulse.scheduled_at + interval
        if loop.time() >= next_heartbeat:
            logger.info("speaker.heartbeat_skipped sequence=%s reason=late_pulse", pulse.sequence)
            return
        pending = self._speaker_timer_handle
        if pending is not None and not pending.cancelled():
            pending.cancel()
        self._speaker_timer_handle = loop.call_at(
            max(loop.time(), pulse.scheduled_at + max(0.0, interval - 0.1)),
            self._launch_speaker_analysis, pulse.episode_generation, pulse.sequence,
        )

    def _launch_speaker_analysis(self, generation: int, sequence: int) -> None:
        self._speaker_timer_handle = None
        if self.speaker_sensor is None or self.episode.closed:
            return
        if generation != self.episode._heartbeat_episode_generation:
            return
        if self._speaker_analysis_task is not None and not self._speaker_analysis_task.done():
            logger.info("speaker.analysis_skipped sequence=%s reason=inference_in_progress", sequence)
            return
        task = asyncio.create_task(self._analyze_speaker_snapshot(sequence), name="speaker-heartbeat-analysis")
        self._speaker_analysis_task = task

    async def _analyze_speaker_snapshot(self, sequence: int) -> None:
        sensor = self.speaker_sensor
        if sensor is None:
            return
        end_sample = sensor.latest_sample
        if end_sample is None:
            return
        ws = self.workspace
        owner_candidate = self._owner_barge_start_sample is not None
        barge_id = self._owner_barge_candidate_id if owner_candidate else None
        start_sample = (self._owner_barge_start_sample if owner_candidate
                        else self._speaker_turn_start_sample if ws is not None else None)
        turn_id = ws.turn_id if ws is not None and not owner_candidate else None
        turn_generation = ws.generation if ws is not None and not owner_candidate else None
        started = time.monotonic()
        snapshot = await sensor.analyze(
            candidate_start_sample=start_sample,
            candidate_end_sample=end_sample if start_sample is not None else None,
        )
        if sensor is not self.speaker_sensor:
            return
        if owner_candidate:
            if (self._owner_barge_candidate_id != barge_id
                    or self._owner_barge_start_sample != start_sample):
                logger.info("speaker.analysis_discarded sequence=%s reason=barge_candidate_changed", sequence)
                return
        elif ws is not None:
            if self.workspace is not ws or ws.generation != turn_generation:
                logger.info("speaker.analysis_discarded sequence=%s reason=input_turn_changed", sequence)
                return
        elif self.workspace is not None or self._owner_barge_candidate_id is not None:
            logger.info("speaker.analysis_discarded sequence=%s reason=candidate_started", sequence)
            return
        self._speaker_snapshot = snapshot
        self._speaker_snapshot_turn_id = turn_id
        self._speaker_snapshot_generation = turn_generation
        self._speaker_snapshot_barge_id = barge_id
        age_ms = max(0, round((time.monotonic() - snapshot.completed_at) * 1000, 2))
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        logger.info(
            "speaker.rolling_result sequence=%s turn_id=%s candidate_id=%s start_sample=%s "
            "end_sample=%s identity=%s quality=%s reason=%s score=%s latency_ms=%s age_ms=%s",
            sequence, turn_id, self._owner_barge_candidate_id if owner_candidate else None,
            snapshot.analysis_start_sample, snapshot.analysis_end_sample,
            snapshot.identity.value, snapshot.quality, snapshot.reason,
            snapshot.score, elapsed_ms, age_ms,
        )
        if self.speaker_mode != "off":
            self.state.current["speaker_rolling"] = {
                "state": snapshot.identity.value,
                "quality": snapshot.quality,
                "reason": snapshot.reason,
                "score": snapshot.score,
                "analysis_start_sample": snapshot.analysis_start_sample,
                "analysis_end_sample": snapshot.analysis_end_sample,
                "completed_at_monotonic": snapshot.completed_at,
            }
            await self._publish(event("speaker.rolling", turn_id,
                                      state=self.state.current["speaker_rolling"]))

    async def _on_kws_activated(self, source_id: str, generation: int) -> bool:
        quiet = getattr(self, "quiet_policy", None)
        if quiet is not None:
            quiet.user_interaction = True
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

    async def _on_model_speech_accepted(self) -> None:
        if callable(self.on_user_activity):
            self.on_user_activity()
        if await self.activate_asr_for_model_speech():
            # A model-initiated speech (scheduled/DSH wake task) moved the
            # microphone from KWS standby to ASR.  Arm the same silence window
            # a wake word arms, so ownership returns to KWS only after the
            # configured final silence has really elapsed.
            await self.episode.arm_silence_window()

    async def _final_system_check_settled(self, context: dict) -> None:
        scheduler_generation = context.get("generation")
        if not isinstance(scheduler_generation, int):
            # Only an elapsed silence ladder may hand input ownership back to
            # KWS.  A callback that cannot name its ladder generation cannot
            # prove the configured window expired, so it must not rearm KWS.
            return
        activation_generation = self._activation_generation

        async def return_when_input_is_idle() -> None:
            while (self.state.listening and self.state.input_owner == "asr"
                   and activation_generation == self._activation_generation):
                if scheduler_generation != self.episode.scheduler.generation:
                    return
                output_busy = self._output_is_busy()
                if (self.workspace is None and self.episode.user_speaking is not True
                        and not output_busy):
                    quiet = getattr(self, "quiet_policy", None)
                    if quiet is not None:
                        quiet.user_interaction = False
                    await self._return_to_kws(activation_generation)
                    return
                await asyncio.sleep(0.05)

        task = asyncio.create_task(
            return_when_input_is_idle(), name="final-system-check-kws-return",
        )
        self._track(task)

    def _output_is_busy(self) -> bool:
        episode = getattr(self, "episode", None)
        checker = getattr(episode, "_output_is_busy", None)
        if callable(checker):
            return bool(checker())
        output = getattr(self, "output", None)
        if (bool(getattr(output, "busy", False))
                or bool(getattr(output, "active", False))
                or bool(getattr(output, "_settling_speech_ids", None))):
            return True
        pending = getattr(output, "_pending", None)
        if not pending:
            return False
        return any(
            item is not None
            and not getattr(item, "reason", None)
            and not getattr(getattr(item, "task", None), "done", lambda: False)()
            for item in pending
        )

    def _final_silence_dispatch_ready(self) -> bool:
        """Whether a final system check may start without racing live input/output."""

        episode = getattr(self, "episode", None)
        state = getattr(self, "state", None)
        if (episode is None or state is None or not state.listening
                or state.input_owner != "asr" or self.workspace is not None
                or episode.user_speaking is True):
            return False
        return not self._output_is_busy()

    def set_reasoning_mode(self, mode: str) -> None:
        if mode not in {"fast", "deep"}:
            raise ValueError("reasoning_mode（运行模式）必须是 fast（快速）或 deep（深度）")
        self.reasoning_mode = mode
        # episode（代理会话）跨暂停保留，后续请求必须读取最新选择。
        # reasoning_effort（请求推理强度）在派发时已复制；更新模式不会改变当前请求。
        self.episode.reasoning_mode = mode

    def set_agent_options(self, options) -> None:
        """set_agent_options（更新代理参数）：当前静默窗口立即采用新时长。"""
        self.agent_options = options
        self.episode.options = options
        self.episode.scheduler.set_options(options)

    async def _agent_event(self, kind, payload):
        # Only an authoritative Episode snapshot may replace the compact
        # current-state projection.  Late trace/output events deliberately
        # retain their own source identity and must remain history-only.
        if kind == "agent.episode":
            self.state.agent = dict(payload)
        elif kind == "speech.completed":
            # Speech settlement never moves input ownership: only a settled
            # silence ladder may rearm KWS.  See _final_system_check_settled.
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
                metadata={key: payload.get(key) for key in (
                    "conversation_id", "request_id", "turn_id", "owner_scope", "kind", "envelope_validated")},
            )
            # Every settled transaction becomes one conversation line — also a
            # failed or interrupted one, whose status is what the user needs to
            # understand why they did not hear it.
            payload_delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
            self.history.conversation.append(
                speech_kind(payload.get("owner_scope")),
                conversation_id=str(payload.get("conversation_id") or ""),
                turn_id=str(payload.get("turn_id") or ""),
                speech_id=speech_id,
                text=str(payload.get("text") or ""),
                status=str(payload.get("status") or "completed"),
                delivery=dict(payload_delivery),
                termination=str(payload_delivery.get("termination") or payload.get("status") or ""),
            )
            await self.episode.log(
                "SPEECH_HISTORY_COMMITTED",
                identity=(payload.get("conversation_id"), payload.get("request_id")),
                turn_id=str(payload.get("turn_id") or ""),
                speech_id=speech_id,
                added=added,
                history_present=self.history.has_speech(speech_id),
                delivery_status=delivery.status, termination=delivery.termination, precision=delivery.precision,
                text_chars=len(str(payload.get("text") or "")), heard_chars=len(delivery.heard_text),
                uncertain_chars=len(delivery.uncertain_text), unheard_chars=len(delivery.unheard_text),
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
            # The continuity anchor callback is optional, exactly like every
            # other observer this path consults.
            if callable(getattr(self, "on_silence_anchor", None)):
                self.on_silence_anchor(time.time())
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
        was_listening = self.state.listening  # was_listening（重复开始保护）：不重置现有静默期限。
        if self.episode.closed:
            self.episode = self._make_episode()
        else:
            # A previous stop closed the input scope while leaving the running
            # request alive.  Starting the microphone again re-opens dispatch.
            open_input_scope = getattr(self.episode, "open_input_scope", None)
            if callable(open_input_scope):
                open_input_scope()
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
        if not was_listening and self.state.listening and self.wake_always_on:
            if self.quiet_policy is not None:
                self.quiet_policy.user_interaction = True
            await self.episode.arm_silence_window(f"manual-start-{uuid4().hex}", anchor_source="manual_start")

    async def accept_shadow_events(self, events) -> None:
        for item in events:
            await self.episode.accept_shadow_event(item)

    async def accept_durable_voice_event(self, item) -> None:
        await self.episode.accept_durable_voice_event(item)

    async def enqueue_scheduled_wake(self, task: dict) -> bool:
        if self.episode.closed:
            self.episode = self._make_episode()
        return await self.episode.enqueue_scheduled_wake(task)

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
            if not self._speaker_admit_turn(ws.turn_id) or self._speaker_defer_turn(ws.turn_id):
                # Final text still reaches the environment store at commit;
                # provisional text must not wake the model meanwhile.
                return
            await self.episode.observe("USER_SEGMENT_UPDATED", turn_id=ws.turn_id,
                                       text="".join(s["text"] for s in ws.segments),
                                       output_generation=ws.generation, sentences=list(ws.segments),
                                       sentence_ids=list(payload.get("changed_ids") or []))
        elif kind == "turn.endpoint_candidate":
            key = (ws.turn_id, ws.generation)
            if key != self._endpoint_context:
                self._endpoint_context = key
                await self.episode.observe("ENDPOINT_CANDIDATE", turn_id=ws.turn_id)

    def _admitted_passive_speech(self, activity) -> bool:
        """只让已准入的讲话改变待播输出的暂停状态，原始检测仍继续识别。"""
        ws = getattr(self, "workspace", None)
        mode = getattr(ws, "speaker_mode", getattr(self, "speaker_mode", "off"))
        if mode != "filter":
            return bool(activity.is_speech)
        decision = getattr(ws, "speaker_decision", {}) or {}
        return bool(
            activity.is_speech and ws is not None
            and ws.speaker_routing_finalized and not ws.speaker_blocked
            and decision.get("state") == "owner_confirmed" and decision.get("route") == "main"
            and not decision.get("degraded", False)
        )

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
                self._clear_owner_barge_candidate()
                self._owner_barge_expired = False
                self.barge_in_gate.reset()
                self._barge_trace_state = None
                await self.episode.speech_activity(self._admitted_passive_speech(activity))
                return activity
            protected = self._barge_in_protected_available()
            if not protected:
                self._clear_owner_barge_candidate()
                if getattr(self, "owner_only_barge_in", False):
                    self.barge_in_gate.reset()
                    await self.episode.log("SPEAKER_BARGE_SUPPRESSED", reason="aec_unavailable")
                    return None
                await self._handle_aec_unavailable(handle)
                # Never hand a playing-output frame to normal input handling:
                # AEC-unavailable audio is neither trustworthy user speech nor
                # eligible raw-VAD fallback.
                return None
            decision = self.barge_in_gate.feed(activity, now=frame.captured_at,
                                               near_voice=self._near_voice_activity(),
                                               protected_available=protected)
            if getattr(self, "owner_only_barge_in", False):
                return await self._observe_owner_barge(activity, frame, handle, progress, decision)
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
            await self.episode.speech_activity(self._admitted_passive_speech(activity))
        return activity

    def _clear_owner_barge_candidate(self) -> None:
        self._owner_barge_candidate_id = None
        self._owner_barge_start_sample = None
        self._owner_barge_started_at = None
        self._owner_barge_output_key = None

    async def _observe_owner_barge(self, activity, frame, handle, progress, decision):
        """Fail closed until this playback candidate has its own owner proof."""
        if self._owner_barge_expired:
            if not bool(activity.is_speech):
                self._owner_barge_expired = False
                self.barge_in_gate.reset()
            return None
        if not decision.candidate:
            if self._owner_barge_candidate_id is not None:
                logger.info("speaker.barge_candidate_cleared candidate_id=%s reason=%s",
                            self._owner_barge_candidate_id, decision.reason)
            self._clear_owner_barge_candidate()
            return None
        if self._owner_barge_candidate_id is None:
            self._owner_barge_candidate_id = uuid4().hex
            self._owner_barge_start_sample = (
                self._speech_start_sample if self._speech_start_sample is not None else frame.start_sample
            )
            self._owner_barge_started_at = (
                getattr(self.barge_in_gate, "candidate_started_at", None) or frame.captured_at
            )
            self._owner_barge_output_key = handle.workspace.playback_key
            logger.info("speaker.barge_candidate_started candidate_id=%s speech_id=%s start_sample=%s",
                        self._owner_barge_candidate_id, handle.speech_id,
                        self._owner_barge_start_sample)
        candidate_id = self._owner_barge_candidate_id
        elapsed_ms = max(0, (frame.captured_at - (self._owner_barge_started_at or frame.captured_at)) * 1000)
        if elapsed_ms >= 2500:
            logger.info("speaker.barge_denied candidate_id=%s reason=deadline elapsed_ms=%.1f",
                        candidate_id, elapsed_ms)
            self._owner_barge_expired = True
            self._clear_owner_barge_candidate()
            self.barge_in_gate.reset()
            return None
        snapshot = self._speaker_snapshot
        if snapshot is not None and snapshot.identity == RollingSpeakerIdentity.NON_OWNER and snapshot.quality == "ready" and (
            snapshot.candidate_start_sample == self._owner_barge_start_sample
            and snapshot.is_fresh()
        ):
            logger.info("speaker.barge_denied candidate_id=%s reason=non_owner score=%s",
                        candidate_id, snapshot.score)
            self._owner_barge_expired = True
            self._clear_owner_barge_candidate()
            self.barge_in_gate.reset()
            return None
        authorized = bool(
            decision.confirmed and snapshot is not None
            and snapshot.identity == RollingSpeakerIdentity.OWNER
            and snapshot.is_fresh()
            and snapshot.candidate_start_sample == self._owner_barge_start_sample
            and self._speaker_snapshot_barge_id == candidate_id
            and snapshot.candidate_end_sample is not None
            and snapshot.candidate_end_sample <= frame.end_sample
            and self._owner_barge_output_key == handle.workspace.playback_key
            and self._barge_in_protected_available()
        )
        if not authorized:
            return None
        start_sample = self._owner_barge_start_sample or frame.start_sample
        self._recent = deque(
            AudioFrame(f.pcm[max(0, start_sample - f.start_sample) * 2:],
                       max(start_sample, f.start_sample), f.captured_at)
            for f in self._recent if f.end_sample > start_sample
        )
        logger.info("speaker.barge_authorized candidate_id=%s speech_id=%s score=%s elapsed_ms=%.1f",
                    candidate_id, handle.speech_id, snapshot.score, elapsed_ms)
        await self.episode.log("SPEAKER_BARGE_AUTHORIZED", candidate_id=candidate_id,
                               speech_id=handle.speech_id, score=snapshot.score,
                               elapsed_ms=round(elapsed_ms, 1))
        handle.overlap_start_ms = max(
            0, int(progress.get("output_estimated_ms", 0)) - decision.duration_ms,
        )
        self._clear_owner_barge_candidate()
        self._owner_barge_expired = False
        self.barge_in_gate.reset()
        self.turns.invalidate()
        playback = self._stop_playback_snapshot(handle.workspace.playback_key)
        await self._state_event(TerminalState.INTERRUPTING, listening=True, turn_id=handle.turn_id)
        await self.episode.speech_activity(True, interrupt_reason="user_interrupted")
        await self._cancel_turn(reason="interrupted", snapshot=playback)
        await self._new_turn(frame)
        self._speaker_turn_start_sample = start_sample
        return None

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
        speaker_decision = await self._speaker_decision(ws)
        if ws.token != token or self.workspace is not ws:
            return True
        ws.speaker_routing_finalized = True
        mode = str(getattr(ws, "speaker_mode", getattr(self, "speaker_mode", "off")))
        final_owner_confirmed = bool(
            speaker_decision is not None
            and speaker_decision.turn_id == ws.turn_id
            and speaker_decision.generation == ws.generation
            and speaker_decision.state == SpeakerState.OWNER_CONFIRMED
            and speaker_decision.route == "main"
            and not speaker_decision.degraded
        )
        final_veto = mode == "filter" and not final_owner_confirmed
        if mode == "filter" and final_veto and ws.final_text:
            decision_payload = (speaker_decision.to_payload() if speaker_decision is not None else {})
            if self.ambient_store is not None:
                self.ambient_store.append({
                    "turn_id": ws.turn_id,
                    "text": ws.final_text,
                    "speaker_state": decision_payload.get("state", "unknown"),
                    "similarity": decision_payload.get("similarity"),
                    "reason": decision_payload.get("reason", "speaker_insufficient_evidence"),
                    "refined_text": ws.refined_text,
                })
            ws.speaker_blocked = True
            self.episode.discard_speaker_vetoed_input(ws.turn_id)
            await self._detail("speaker.blocked", decision=decision_payload, text=ws.final_text)
            logger.info("speaker.request_veto turn_id=%s reason=%s final=%s late=%s",
                        ws.turn_id, decision_payload.get("reason"), True,
                        getattr(self.episode, "active_request_source_turn_id", None) == ws.turn_id)
            # ASR partials may have started an optimistic Bidi/TTS prewarm
            # before the 150 ms speaker gate completed. A filtered turn must
            # never retain or emit that output resource.
            await self._cancel_tts_prewarm(ws.turn_id)
            await self._retire_committed_input(ws)
            # A filtered transcript is an ambient-only record, not the active
            # user input shown in the ordinary conversation projection.
            self.state.current.update(final_text="", partial_text="")
            await self._state_event(TerminalState.IDLE, listening=self.state.listening)
            return True
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
                                       analysis=dict(ws.analysis), speaker_decision=dict(ws.speaker_decision),
                                       output_generation=ws.generation,
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
            "speaker_decision": dict(ws.speaker_decision),
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
                        "speaker_decision": dict(ws.speaker_decision),
                        "transcript_updates": [dict(item) for item in ws.transcript_updates]},
        })
        self._input_history_committed.add(ws.turn_id)
        self._committed_workspaces[ws.turn_id] = ws
        if len(self._committed_workspaces) > 64:
            self._committed_workspaces.pop(next(iter(self._committed_workspaces)), None)
        # The user's line owns the position of the moment the user said it.  A
        # turn only settles after its reply has been generated, spoken and
        # settled, so creating this line at settlement filed every assistant
        # reply above the utterance it answered.  The commit (ASR final) is the
        # last boundary that provably precedes any reply, and settlement later
        # refreshes this same line in place — the log keys a line by
        # ``turn_id``, so the true outcome never moves it.
        if ws.final_text:
            self._record_user_turn(ws, text=ws.final_text, status="completed")
        return record

    def _record_user_turn(self, ws, *, text, status, delivery=None, termination=""):
        """Give the user's turn its one line in the ordered conversation.

        Identity is ``turn_id``: the commit creates the line, and every later
        settlement of the same turn (``turn.completed``/``turn.latency``) only
        refreshes its text and status, so a status that arrives late can never
        reorder the rail.
        """

        self.history.conversation.append(
            USER_TURN,
            conversation_id=str(ws.episode_id or ""),
            turn_id=ws.turn_id,
            text=text,
            status=status,
            delivery=dict(delivery or {"status": "pending", "termination": "pending"}),
            termination=termination,
        )

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
        # Settlement never decides the position of this turn: the commit gave it
        # one before the reply could exist, so this is an in-place refresh of
        # that line.  A turn that never committed a transcript (an empty input)
        # has no line yet and gets its single one here.
        self._record_user_turn(
            ws,
            text=ws.final_text,
            status=outcome,
            delivery={"status": outcome, "termination": outcome},
            termination=outcome,
        )
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
            text = strip_wake_prefix(text, self.wake_keywords)
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

        # A filtered turn has no ordinary SessionLedger/Agent entry.  A late
        # SenseVoice result may enrich only its session-scoped ambient record.
        if ws.speaker_blocked:
            if self.ambient_store is not None:
                self.ambient_store.update_refined(ws.turn_id, text)
            return

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
            # Same rule as the commit path: the user's line enters the ordered
            # conversation where the user said it, never where the turn settled.
            if ws.final_text:
                self._record_user_turn(ws, text=ws.final_text, status="completed")
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
            # A turn cancelled before its transcript became final has no line
            # yet; this is its one line, created where the capture stopped.
            self._record_user_turn(
                ws,
                text=ws.final_text or ws.raw_partial,
                status=reason,
                delivery={"termination": reason, "status": "none"},
                termination=reason,
            )
            await self._detail("turn.completed", **{k: v for k, v in record.to_payload().items()
                                                     if k != "turn_id"})
        self.workspace = None
        self.turns.invalidate()
        self._active_input = False
        if reason not in {"completed", "stopped"}:
            # Dropping the active Agent request belongs to lifecycle boundaries
            # (reset/failed/close/confirmed barge-in).  ``stopped`` is the
            # microphone boundary: a reply that is already being generated or
            # queued still has to be heard, so it is closed by input scope
            # instead of being cancelled.
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
        """Stop microphone input without cancelling the Agent output scope.

        Listening is an input boundary.  The Agent episode and its FIFO TTS
        output may already contain a response that still needs to be heard,
        so closing the episode here would incorrectly cancel that response.
        Full output teardown remains owned by reset/close/error paths.
        """
        await self._cancel_early_silence()
        self.state.listening = False
        if self.workspace is not None:
            await self._cancel_turn(reason="stopped", record=True)
        if not self.episode.closed:
            await self.episode.speech_activity(None)
        # No new turn may be dispatched for the closed input scope, but the
        # request already in flight and everything in the TTS FIFO survives.
        close_input_scope = getattr(self.episode, "close_input_scope", None)
        if callable(close_input_scope):
            close_input_scope()
        # A response queued while the user was speaking waits behind the
        # pre-playback hold.  Releasing it here is what lets the surviving
        # output actually be heard after the microphone stopped.
        release_held = getattr(getattr(self, "output", None), "release_held", None)
        if callable(release_held):
            release_held()
        await self._cancel_tts_prewarm()
        await super().stop_listening()

    async def reset_session(self):
        """End the active Agent task while keeping microphone listening alive."""
        await self._cancel_early_silence()
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
        await self._stop_speaker_sensor()
        self._clear_owner_barge_candidate()
        self._owner_barge_expired = False
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

from __future__ import annotations

import asyncio
from collections import deque
import logging
import time
from uuid import uuid4

from .turn_controller import InputController
from .speech_pipeline import SpeechPipeline
from .turn_workspace import AudioFrame, CurrentTurnAudioBuffer, TurnWorkspace
from .transcript_segmenter import TranscriptSegmenter
from .options import InputOptions
from ..analysis.client import AnalysisClient
from ..agent.shadow_registry import VoiceShadowRegistry
from ..asr.protocol import AudioChunk
from ..core.events import event, utc_now_iso
from ..core.state import TerminalState
from ..kws.keywords import strip_wake_prefix
from ..kws.service import KwsAudioFrame
from ..tts.alignment import DeliveryResolver
from ..speaker.client import SpeakerWorkerClient
from ..speaker.contracts import SpeakerDecision, SpeakerObservation, SpeakerMode, SpeakerState
from ..speaker.router import SpeakerRouter
from ..session.ambient import AmbientTranscriptStore

logger = logging.getLogger(__name__)


class RealtimeController(SpeechPipeline, InputController):
    """Realtime input orchestration used by the session runtime."""

    def __init__(self, *, options: InputOptions, analysis=None,
                 shadow_waker=None, shadow_available: bool = False, **kwargs) -> None:
        self.conversation = kwargs.pop("conversation")
        self.speaker_client: SpeakerWorkerClient | None = kwargs.pop("speaker_client", None)
        self.speaker_router: SpeakerRouter | None = kwargs.pop("speaker_router", None)
        self.speaker_mode = str(kwargs.pop("speaker_mode", "off"))
        self.speaker_wait_ms = int(kwargs.pop("speaker_wait_ms", 150))
        self.ambient_store: AmbientTranscriptStore | None = kwargs.pop("ambient_store", None)
        super().__init__(**kwargs)
        self.options = options
        self.analysis = analysis or AnalysisClient(options.analysis_url)
        self.analysis_health: dict = {}
        self.shadow_waker = shadow_waker
        self.registry = VoiceShadowRegistry(available=shadow_available)
        self.workspace: TurnWorkspace | None = None
        self.segmenter = TranscriptSegmenter(options.min_segment_chars, options.max_segment_chars)
        self._endpoint_task: asyncio.Task | None = None
        self._refine_task: asyncio.Task | None = None
        self._tts_prewarm_tasks: dict[str, asyncio.Task] = {}
        # Keep input timing dictionaries after the audio workspace is retired;
        # EpisodeSpeechOutput settles the independently owned response later.
        self._turn_timings: dict[str, dict] = {}
        self._background: set[asyncio.Task] = set()
        self._recent: deque[AudioFrame] = deque()
        self._vad_origin: int | None = None
        self._speech_end_sample: int | None = None
        self._speech_start_sample: int | None = None
        self._speech_samples = 0
        self._last_candidate = 0.0
        self._sub_sequence = 0
        self._sub_id: str | None = None
        self._sub_parts: dict[str, str] = {}
        self._sub_finals: dict[str, asyncio.Event] = {}
        self._asr_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._asr_sender: asyncio.Task | None = None
        self._asr_recovery_task: asyncio.Task | None = None
        self._asr_queue_overflow = False
        self._asr_queue_overflow_at: float | None = None
        self._last_frame: AudioFrame | None = None
        self._resume_started: int | None = None
        self._vad_generation = 0
        self._reset_vad_pending = False
        self._activation_generation = 0

    async def on_speaker_observation(self, observation: SpeakerObservation) -> None:
        router = getattr(self, "speaker_router", None)
        if router is None:
            return
        previous_state = router.machine.state
        enriched = router.observe(observation)
        ws = self.workspace
        if ws is None or ws.turn_id != enriched.turn_id or ws.generation != enriched.generation:
            return
        ws.speaker_observations.append(enriched.to_payload(include_embedding=False))
        if len(ws.speaker_observations) > 16:
            del ws.speaker_observations[:-8]
        decision = router.decision()
        # Window scores stay inside the sidecar.  Publish only a meaningful
        # state transition; the final per-turn decision is emitted below at
        # the semantic commit boundary.
        if decision.state != previous_state:
            ws.speaker_decision = decision.to_payload()
            ws.speaker_state = decision.state.value
            ws.speaker_degraded_reason = decision.reason if decision.degraded else ""
            self.state.current.update(speaker_decision=decision.to_payload())
            await self._publish(event("speaker.state", ws.turn_id,
                                      output_generation=ws.generation,
                                      state=decision.to_payload(),
                                      observation=enriched.to_payload(include_embedding=False)))

    async def _speaker_decision(self, ws: TurnWorkspace) -> SpeakerDecision | None:
        mode = str(getattr(ws, "speaker_mode", getattr(self, "speaker_mode", "off")))
        speaker_router = getattr(self, "speaker_router", None)
        if mode == SpeakerMode.OFF or speaker_router is None:
            return None
        router = speaker_router
        speaker_client = getattr(self, "speaker_client", None)
        if speaker_client is None or not speaker_client.connected:
            router.mark_unavailable("speaker_worker_unavailable")
            decision = router.decision(
                route="main" if mode == SpeakerMode.OBSERVE else "passthrough",
                degraded=True,
            )
        else:
            finished = await speaker_client.finish_turn(
                ws.turn_id, ws.generation,
                timeout_ms=getattr(self, "speaker_wait_ms", 150),
            )
            if not finished or speaker_client.degraded:
                reason = "speaker_queue_overflow" if speaker_client.degraded else "speaker_timeout"
                # A timeout is itself a degraded observation boundary.  Mark
                # the optional service accordingly while still fail-opening
                # the semantic route; a later reconnect/health check can
                # return it to READY.
                self.state.set_service("speaker", "DEGRADED")
                decision = router.decision(
                    route="main" if mode == SpeakerMode.OBSERVE else "passthrough",
                    degraded=True, reason=reason,
                )
            else:
                decision = router.decision()
                if decision.state == SpeakerState.NON_OWNER_CONFIRMED:
                    route = "ambient" if mode == SpeakerMode.FILTER else "main"
                    decision = router.decision(route=route)
                elif mode == SpeakerMode.OBSERVE:
                    decision = router.decision(route="main")
                elif decision.state in {SpeakerState.UNKNOWN, SpeakerState.UNAVAILABLE}:
                    decision = router.decision(
                        route="main" if mode == SpeakerMode.OBSERVE else "passthrough",
                        degraded=True,
                        reason=("speaker_unavailable" if decision.state == SpeakerState.UNAVAILABLE
                                else "speaker_insufficient_evidence"),
                    )
                else:
                    decision = router.decision(route="main")
        ws.speaker_decision = decision.to_payload()
        ws.speaker_state = decision.state.value
        ws.speaker_degraded_reason = decision.reason if decision.degraded else ""
        self.state.current.update(speaker_decision=decision.to_payload())
        await self._publish(event("speaker.decision", ws.turn_id,
                                  output_generation=ws.generation,
                                  decision=decision.to_payload()))
        return decision

    def _track(self, task: asyncio.Task | None) -> None:
        if task is None:
            return
        self._background.add(task)
        def done(t):
            self._background.discard(t)
            if not t.cancelled():
                t.exception()
        task.add_done_callback(done)

    def _valid(self, token: tuple[str, int]) -> bool:
        # Token validity is now an explicit workspace cancellation boundary.
        # ASR finalization and SenseVoice refinement do not invalidate the
        # active turn; resume/stop replaces the workspace generation.
        return self.workspace is not None and self.workspace.token == token

    async def _refinement_completed(self, ws: TurnWorkspace, result: dict) -> None:
        """Hook for the Episode controller's late semantic refinement path."""

    async def _publish_tts_prewarm(self, ws: TurnWorkspace, status: str, **payload) -> None:
        logger.info(
            "controller.tts_prewarm turn_id=%s status=%s requested_at=%s ready_at=%s",
            ws.turn_id,
            status,
            ws.timings.get("tts_prewarm_requested"),
            ws.timings.get("tts_prewarm_ready"),
        )
        if self.workspace is ws:
            await self._detail("tts.prewarm", event="tts.prewarm", status=status, **payload)
        else:
            await self._publish(event("tts.prewarm", ws.turn_id, output_generation=ws.generation,
                                       event="tts.prewarm", status=status,
                                       workspace=ws.snapshot(), **payload))

    async def _run_tts_prewarm(self, ws: TurnWorkspace) -> None:
        try:
            await self._publish_tts_prewarm(ws, "REQUESTED")
            prewarm = getattr(self.tts, "prewarm", None) if self.tts is not None else None
            if not callable(prewarm):
                ws.tts_prewarm_status = "DISABLED"
                ws.timings["tts_prewarm_status"] = ws.tts_prewarm_status
                await self._publish_tts_prewarm(ws, ws.tts_prewarm_status, reason="unsupported")
                return
            try:
                result = await prewarm(turn_id=ws.turn_id)
            except TypeError:
                result = await prewarm()
            if isinstance(result, dict) and result.get("status") == "READY":
                ws.timings["tts_prewarm_ready"] = time.monotonic()
                ws.tts_prewarm_status = "READY"
                ws.timings["tts_prewarm_status"] = ws.tts_prewarm_status
                await self._publish_tts_prewarm(ws, "READY")
            else:
                ws.tts_prewarm_status = "FALLBACK"
                ws.timings["tts_prewarm_status"] = ws.tts_prewarm_status
                ws.timings["tts_prewarm_fallback"] = True
                await self._publish_tts_prewarm(ws, "FALLBACK", reason=(result or {}).get("reason") if isinstance(result, dict) else None)
        except asyncio.CancelledError:
            ws.tts_prewarm_status = "CANCELLED"
            ws.timings["tts_prewarm_status"] = ws.tts_prewarm_status
            raise
        except Exception as exc:
            ws.tts_prewarm_status = "FAILED"
            ws.timings["tts_prewarm_status"] = ws.tts_prewarm_status
            ws.timings["tts_prewarm_fallback"] = True
            logger.info("controller.tts_prewarm_failed turn_id=%s error=%s", ws.turn_id, str(exc)[:300])
            await self._publish_tts_prewarm(ws, "FAILED", error=str(exc)[:300])
        finally:
            current = self._tts_prewarm_tasks.get(ws.turn_id)
            if current is asyncio.current_task():
                self._tts_prewarm_tasks.pop(ws.turn_id, None)

    def _start_tts_prewarm(self, ws: TurnWorkspace) -> None:
        if ws.tts_prewarm_started:
            return
        ws.tts_prewarm_started = True
        ws.timings.setdefault("tts_prewarm_requested", time.monotonic())
        if self.tts is None or getattr(self.tts, "transport", None) not in {
            "websocket", "websocket_bidi"
        }:
            ws.tts_prewarm_status = "DISABLED"
            ws.timings["tts_prewarm_status"] = ws.tts_prewarm_status
            return
        ws.tts_prewarm_status = "REQUESTED"
        task = asyncio.create_task(self._run_tts_prewarm(ws), name=f"tts-prewarm-{ws.turn_id}")
        self._tts_prewarm_tasks[ws.turn_id] = task
        self._track(task)

    async def _cancel_tts_prewarm(self, turn_id: str | None = None) -> None:
        tasks = []
        for key, task in list(self._tts_prewarm_tasks.items()):
            if turn_id is not None and key != turn_id:
                continue
            self._tts_prewarm_tasks.pop(key, None)
            if not task.done():
                task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A ready warm session is no longer represented by a running task.
        # If no speech transaction has checked it out, dispose of it on turn
        # cancellation so an abandoned turn cannot hold a socket forever.
        if self.tts is not None:
            discard = getattr(self.tts, "discard_prewarm", None)
            if callable(discard):
                try:
                    await discard(turn_id=turn_id)
                except TypeError:
                    await discard()

    def _tts_session_for_controller(self):
        """Return an output-owned session without assuming the controller type."""

        output = getattr(self, "output", None)
        return getattr(output, "_tts_session", None) if output is not None else getattr(self, "_tts_session", None)

    async def _detail(self, type_: str, **payload) -> None:
        ws = self.workspace
        if ws is None:
            return
        self.state.current.update(ws.snapshot(), turn_id=ws.turn_id, input_runtime=True)
        await self._publish(event(type_, ws.turn_id, output_generation=ws.generation,
                                  workspace=ws.snapshot(), **payload))

    async def initialize(self) -> None:
        await super().initialize()
        if self.options.smart_turn or self.options.sensevoice:
            try:
                health = await self.analysis.health()
                self.analysis_health = health if isinstance(health, dict) else {}
                status = "READY" if all(health.get(k, {}).get("status") == "READY" for k, enabled in
                                        (("endpoint", self.options.smart_turn), ("refine", self.options.sensevoice)) if enabled) else "DEGRADED"
            except Exception as exc:
                self.analysis_health = {
                    "endpoint": {"status": "DEGRADED", "error": str(exc) or "health unavailable"},
                    "refine": {"status": "DEGRADED", "error": str(exc) or "health unavailable"},
                }
                status = "DEGRADED"
        else:
            self.analysis_health = {}
            status = "DISABLED"
        self.state.services["analysis"] = status
        await self._state_event(TerminalState.IDLE, listening=False)

    def _analysis_meta(self, name: str, *, status: str) -> dict:
        """Return safe model metadata for the current-turn diagnostics."""

        value = self.analysis_health.get(name, {})
        meta = {"status": status}
        if isinstance(value, dict):
            for key in ("model", "model_loaded"):
                if key in value and value[key] is not None:
                    meta[key] = value[key]
            if status != "READY" and value.get("error"):
                meta["error"] = str(value["error"])
        return meta

    async def start_listening(self) -> None:
        # Analysis is optional and never becomes a mandatory start prerequisite.
        status = self.state.services.pop("analysis", "DISABLED")
        try:
            if not self.wake_always_on and not await self._ensure_asr_ready():
                return
            await super().start_listening()
        finally:
            self.state.services["analysis"] = status
        if self.state.listening and (self._asr_sender is None or self._asr_sender.done()):
            self._asr_sender = asyncio.create_task(self._send_asr_loop())
        if self.state.listening and self.wake_always_on and self.kws is not None:
            self.state.input_owner = "none"
            self.kws.reset()
            try:
                await self._reset_owner_boundary()
            except Exception as exc:
                await self._fail_kws_closed(f"KWS standby initialization failed: {exc}")
                return
            self.state.input_owner = "kws"
            await self._state_event(TerminalState.IDLE, listening=True)

    async def _reset_owner_boundary(self) -> None:
        self._drain_asr_queue()
        self._asr_queue_overflow = False
        self._asr_queue_overflow_at = None
        self._recent.clear()
        self._last_frame = None
        self._vad_origin = None
        self._speech_start_sample = None
        self._speech_end_sample = None
        self._speech_samples = 0
        self._resume_started = None
        self._vad_generation += 1
        self._reset_vad_pending = False
        if self.vad is not None:
            await asyncio.to_thread(self.vad.reset)

    async def _fail_kws_closed(self, message: str) -> None:
        self._activation_generation += 1
        self.state.input_owner = "none"
        self.state.listening = False
        self.state.set_service("kws", "ERROR")
        if self.workspace is not None:
            await self._cancel_turn(reason="failed", record=False)
        if self.capture is not None:
            await self.capture.stop()
            self.state.set_service("audio", "READY")
        await self._error("KWS_WORKER_FAILED", message, recoverable=True)
        await self._state_event(TerminalState.IDLE, listening=False)

    async def _on_kws_activated(self, source_id: str, generation: int) -> bool:
        return True

    async def _activate_kws_detection(self) -> None:
        if not self.wake_always_on or self.state.input_owner != "kws":
            return
        expected_generation = self._activation_generation
        if self.kws is None or self.kws.error is not None or not self.kws.running:
            await self._fail_kws_closed(
                self.kws.error if self.kws is not None and self.kws.error else "KWS worker unavailable"
            )
            return
        try:
            if self.asr is None:
                raise RuntimeError("ASR Worker 客户端未初始化。")
            if not self.asr.connected:
                await self.asr.connect()
            health = getattr(self.asr, "health", None)
            if callable(health):
                await health()
        except Exception as exc:
            self.state.set_service("asr", "ERROR")
            self.kws.reset()
            await self._error("ASR_WORKER_NOT_READY", str(exc), recoverable=True)
            await self._state_event(TerminalState.IDLE, listening=True)
            return
        if (not self.state.listening or self.state.input_owner != "kws"
                or expected_generation != self._activation_generation):
            return
        self._activation_generation += 1
        generation = self._activation_generation
        source_id = f"wake-{uuid4().hex}"
        self.kws.reset()
        await self._reset_owner_boundary()
        self.state.input_owner = "asr"
        self.state.set_service("kws", "READY")
        self.state.set_service("asr", "RUNNING")
        if not await self._on_kws_activated(source_id, generation):
            await self._return_to_kws(generation)
            return
        await self._state_event(TerminalState.IDLE, listening=True)

    async def activate_asr_for_model_speech(self) -> bool:
        """Rotate KWS→ASR when a model speech transaction is accepted.

        Returns ``True`` only when this call really moved input ownership, so
        the caller can arm the matching silence window.
        """
        if not self.wake_always_on or not self.state.listening or self.state.input_owner != "kws":
            return False
        generation = self._activation_generation
        try:
            if self.asr is None:
                raise RuntimeError("ASR Worker 客户端未初始化。")
            if not self.asr.connected:
                await self.asr.connect()
            health = getattr(self.asr, "health", None)
            if callable(health):
                await health()
        except Exception as exc:
            self.state.set_service("asr", "ERROR")
            await self._error("ASR_WORKER_NOT_READY", str(exc), recoverable=True)
            return False
        if not self.state.listening or self.state.input_owner != "kws" or generation != self._activation_generation:
            return False
        self._activation_generation += 1
        if self.kws is not None:
            self.kws.reset()
        await self._reset_owner_boundary()
        self.state.input_owner = "asr"
        self.state.set_service("kws", "READY")
        self.state.set_service("asr", "RUNNING")
        await self._state_event(TerminalState.IDLE, listening=True)
        return True

    async def _return_to_kws(self, generation: int | None = None) -> bool:
        if (not self.wake_always_on or not self.state.listening
                or self.state.input_owner != "asr"):
            return False
        if generation is not None and generation != self._activation_generation:
            return False
        if self.kws is None or self.kws.error is not None or not self.kws.running:
            await self._fail_kws_closed(
                self.kws.error if self.kws is not None and self.kws.error else "KWS worker unavailable"
            )
            return False
        self.state.input_owner = "none"
        self._activation_generation += 1
        transition_generation = self._activation_generation
        self.kws.reset()
        try:
            await self._reset_owner_boundary()
        except Exception as exc:
            await self._fail_kws_closed(f"KWS rearm failed: {exc}")
            return False
        if (not self.state.listening
                or transition_generation != self._activation_generation):
            return False
        self.state.input_owner = "kws"
        self.state.set_service("kws", "RUNNING")
        self.state.set_service(
            "asr", "READY" if self.asr is not None and self.asr.connected else "ERROR",
        )
        await self._state_event(TerminalState.IDLE, listening=True)
        return True

    async def _ensure_asr_ready(self, *, preserve_listening: bool = False) -> bool:
        """Reconnect and perform an application-level ASR readiness check."""

        if self.asr is None:
            self.state.services["asr"] = "ERROR"
            await self._error("ASR_WORKER_UNAVAILABLE", "ASR Worker 客户端未初始化。", recoverable=True)
            await self._state_event(
                TerminalState.IDLE,
                listening=self.state.listening if preserve_listening else False,
            )
            return False
        try:
            if not self.asr.connected:
                await self.asr.connect()
            health = getattr(self.asr, "health", None)
            if health is not None:
                await health()
            self.state.services["asr"] = "READY"
            return True
        except Exception as exc:
            code = getattr(exc, "code", "ASR_WORKER_NOT_READY")
            self.state.services["asr"] = "ERROR"
            await self._error(str(code), str(exc), recoverable=True)
            await self._state_event(
                TerminalState.IDLE,
                listening=self.state.listening if preserve_listening else False,
            )
            return False

    async def _handle_asr_disconnect(self) -> None:
        """Quiesce a broken input transport and schedule one safe reconnect."""

        self.state.services["asr"] = "ERROR"
        if self.wake_always_on and self.state.listening:
            sender = self._asr_sender
            if sender is not None and sender is not asyncio.current_task() and not sender.done():
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            self._asr_sender = None
            self._drain_asr_queue()
            if self.kws is None or self.kws.error is not None or not self.kws.running:
                await self._fail_kws_closed(
                    self.kws.error if self.kws is not None and self.kws.error else "KWS worker unavailable"
                )
                return
            self.state.input_owner = "none"
            self._activation_generation += 1
            transition_generation = self._activation_generation
            self.kws.reset()
            try:
                await self._reset_owner_boundary()
            except Exception as exc:
                await self._fail_kws_closed(f"KWS recovery failed: {exc}")
                return
            if (not self.state.listening
                    or transition_generation != self._activation_generation):
                return
            self.state.input_owner = "kws"
            self.state.services["kws"] = "RUNNING"
            self.state.services["audio"] = "RUNNING"
            if (not self._closed and
                    (self._asr_recovery_task is None or self._asr_recovery_task.done())):
                self._asr_recovery_task = asyncio.create_task(
                    self._recover_asr_session(), name="asr-session-recovery",
                )
                self._track(self._asr_recovery_task)
            return
        self.state.listening = False
        listen_task = self._listen_task
        if listen_task is not None and listen_task is not asyncio.current_task() and not listen_task.done():
            listen_task.cancel()
            await asyncio.gather(listen_task, return_exceptions=True)
        self._listen_task = None
        sender = self._asr_sender
        if sender is not None and sender is not asyncio.current_task() and not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        self._asr_sender = None
        while True:
            try:
                self._asr_queue.get_nowait()
                self._asr_queue.task_done()
            except asyncio.QueueEmpty:
                break
        if self.capture is not None:
            try:
                await self.capture.stop()
            except Exception:
                # The transport failure is the actionable error; a concurrent
                # capture shutdown failure must not hide it.
                pass
            self.state.services["audio"] = "READY"
        self.state.services["kws"] = "READY" if self.wake_enabled and self.kws is not None else "DISABLED"
        if (not self._closed and
                (self._asr_recovery_task is None or self._asr_recovery_task.done())):
            self._asr_recovery_task = asyncio.create_task(
                self._recover_asr_session(),
                name="asr-session-recovery",
            )
            self._track(self._asr_recovery_task)

    async def _recover_asr_session(self) -> None:
        """Attempt one delayed restart after a transient ASR worker loss.

        The failed sender has already been detached before this task is
        created.  Retrying after a short delay keeps the normal readiness
        handshake as the sole authority and avoids a reconnect storm when the
        worker is genuinely down; a failed retry leaves the visible runtime in
        a restartable idle/error state for the user.
        """

        try:
            await asyncio.sleep(0.5)
            if self._closed:
                return
            if self.wake_always_on and self.state.listening and self.state.input_owner == "kws":
                logger.info("controller.asr_recovery_attempt")
                if await self._ensure_asr_ready(preserve_listening=True):
                    self.state.services["asr"] = "READY"
                    if self._asr_sender is None or self._asr_sender.done():
                        self._asr_sender = asyncio.create_task(self._send_asr_loop())
                    await self._state_event(TerminalState.IDLE, listening=True)
                    logger.info("controller.asr_recovery_succeeded")
                return
            if self.state.listening:
                return
            logger.info("controller.asr_recovery_attempt")
            await self.start_listening()
            if self.state.listening:
                logger.info("controller.asr_recovery_succeeded")
            else:
                logger.warning("controller.asr_recovery_not_ready")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("controller.asr_recovery_failed")

    def _queue_asr(self, item) -> None:
        # No awaited network writes on the capture path.  A full queue means
        # ASR can no longer be given a contiguous input stream.  Do not throw
        # from this helper: that used to stop the entire microphone session.
        # The next capture frame performs a controlled resynchronisation.
        try:
            self._asr_queue.put_nowait(item)
        except asyncio.QueueFull:
            self._asr_queue_overflow = True
            self._asr_queue_overflow_at = time.monotonic()
            logger.warning(
                "controller.asr_queue_overflow queue_capacity=%d item_type=%s turn_id=%s",
                self._asr_queue.maxsize,
                type(item).__name__,
                getattr(item, "turn_id", item),
            )

    def _drain_asr_queue(self) -> None:
        while True:
            try:
                self._asr_queue.get_nowait()
                self._asr_queue.task_done()
            except asyncio.QueueEmpty:
                return

    async def _send_asr_loop(self) -> None:
        while True:
            item = await self._asr_queue.get()
            try:
                if isinstance(item, AudioChunk):
                    if item.turn_id in self._sub_parts:
                        await self.asr.send_audio(item)
                else:
                    await self.asr.send_finish(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._cancel_turn(reason="failed", record=False)
                code = getattr(exc, "code", None)
                if not isinstance(code, str):
                    code = "ASR_WORKER_DISCONNECTED" if "ConnectionClosed" in type(exc).__name__ else "ASR_SEND_FAILED"
                # Any failed write leaves the sender's transport unusable.
                # Keep the recovery path identical for protocol disconnects
                # and other send failures so the capture loop cannot continue
                # feeding a dead ASR connection.
                await self._handle_asr_disconnect()
                await self._error(code, str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=self.state.listening)
                return
            finally:
                self._asr_queue.task_done()

    async def _recover_input_continuity(
        self,
        frame: AudioFrame,
        *,
        reason: str,
        expected_start_sample: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Discard only an unverifiable input transaction and keep listening.

        PCM must never be joined across a capture or ASR backpressure hole.
        The old path correctly rejected the corrupt turn but escalated it to a
        terminal input-loop failure.  Output ownership deliberately remains
        separate: abandoning an input cannot silence an already authorised
        speech transaction.
        """

        if not hasattr(self, "_vad_generation"):
            self._vad_generation = 0
        ws = self.workspace
        capture_diagnostics = getattr(self.capture, "diagnostics", {}) if self.capture is not None else {}
        detail = {
            "reason": reason,
            "expected_start_sample": expected_start_sample,
            "actual_start_sample": frame.start_sample,
            "dropped_chunks": capture_diagnostics.get("dropped_chunks"),
            "asr_queue_depth": self._asr_queue.qsize(),
        }
        if error is not None:
            # A per-frame controller exception is just as unsafe to append to
            # the in-flight ASR segment as a physical sample gap.  Preserve
            # its class/message for diagnosis, but never turn it into a
            # user-visible stop request or silently lose the cause.
            detail["error_type"] = type(error).__name__
            detail["error"] = str(error)[:300]
        logger.warning(
            "controller.input_continuity_recovered reason=%s turn_id=%s expected_start_sample=%s "
            "actual_start_sample=%s asr_queue_depth=%d",
            reason,
            ws.turn_id if ws else None,
            expected_start_sample,
            frame.start_sample,
            self._asr_queue.qsize(),
        )
        self._drain_asr_queue()
        self._asr_queue_overflow = False
        self._asr_queue_overflow_at = None
        if ws is not None:
            await self._cancel_turn(reason="capture_gap", record=True)
        self._recent.clear()
        self._last_frame = frame
        self._vad_origin = frame.start_sample
        self._speech_start_sample = None
        self._speech_end_sample = None
        self._speech_samples = 0
        self._resume_started = None
        self.barge_in_gate.reset()
        self._vad_generation += 1
        if self.vad is not None:
            await asyncio.to_thread(self.vad.reset)
        self._reset_vad_pending = False
        self.state.current.update(capture_diagnostics=detail)
        error_code = "AUDIO_CAPTURE_GAP" if error is None else "AUDIO_INPUT_FRAME_FAILED"
        error_message = (
            "音频采集出现缺口，已放弃当前未完成输入并继续监听。"
            if error is None
            else "音频帧处理异常，已放弃当前未完成输入并继续监听。"
        )
        await self._error(
            error_code,
            error_message,
            recoverable=True,
            turn_id=ws.turn_id if ws else None,
        )
        await self._state_event(TerminalState.LISTENING, listening=True)

    async def _listen_loop(self) -> None:
        while self.state.listening and not self._closed:
            try:
                frame = await self.capture.read_frame()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Failure to read the physical capture source means there is
                # no trustworthy next frame and the session must be settled.
                # Keep this separate from below: a controller/VAD/ASR queue
                # exception while handling an otherwise valid frame is
                # recoverable and must not make TTS end the user's listening
                # session.
                logger.exception("controller.input_capture_read_failed")
                await self._cancel_turn(reason="failed", record=False)
                self.state.listening = False
                try:
                    await self.capture.stop()
                except Exception:
                    logger.exception("controller.input_audio_loop_stop_capture_failed")
                sender = self._asr_sender
                if sender is not None and sender is not asyncio.current_task() and not sender.done():
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                self._asr_sender = None
                while True:
                    try:
                        self._asr_queue.get_nowait()
                        self._asr_queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                if self.asr is not None:
                    try:
                        await self.asr.close()
                    except Exception:
                        logger.exception("controller.input_audio_loop_close_asr_failed")
                self.state.services["audio"] = "READY" if self.capture is not None else "ERROR"
                self.state.services["kws"] = "READY" if self.wake_enabled and self.kws is not None else "DISABLED"
                self.state.services["asr"] = "READY" if self.asr is not None and self.vad is not None else "ERROR"
                await self._error("AUDIO_PIPELINE_FAILED", str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=self.state.listening)
                break
            try:
                await self.handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "controller.input_frame_failed reason=frame_processing_error start_sample=%s",
                    frame.start_sample,
                )
                try:
                    await self._recover_input_continuity(
                        frame,
                        reason="frame_processing_error",
                        expected_start_sample=self._last_frame.end_sample if self._last_frame else None,
                        error=exc,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Do not hide the original error, and do not spin a
                    # broken listener indefinitely.  This is now the only
                    # path where frame-level recovery itself failed.
                    logger.exception("controller.input_frame_recovery_failed")
                    await self._cancel_turn(reason="failed", record=False)
                    self.state.listening = False
                    try:
                        await self.capture.stop()
                    except Exception:
                        logger.exception("controller.input_frame_recovery_stop_capture_failed")
                    if self.asr is not None:
                        try:
                            await self.asr.close()
                        except Exception:
                            logger.exception("controller.input_frame_recovery_close_asr_failed")
                    self.state.services["audio"] = "READY" if self.capture is not None else "ERROR"
                    self.state.services["asr"] = "READY" if self.asr is not None and self.vad is not None else "ERROR"
                    await self._error("AUDIO_PIPELINE_FAILED", "音频帧恢复失败，请重新开始监听。", recoverable=True)
                    await self._state_event(TerminalState.IDLE, listening=False)
                    break

    async def handle_frame(self, frame: AudioFrame) -> None:
        if not hasattr(self, "_vad_generation"):
            self._vad_generation = 0
        expected_start_sample = self._last_frame.end_sample if self._last_frame else None
        if self._asr_queue_overflow:
            await self._recover_input_continuity(
                frame,
                reason="asr_queue_overflow",
                expected_start_sample=expected_start_sample,
            )
            return
        if expected_start_sample is not None and frame.start_sample != expected_start_sample:
            await self._recover_input_continuity(
                frame,
                reason="capture_gap",
                expected_start_sample=expected_start_sample,
            )
            return
        processed = (
            await asyncio.to_thread(self._process_audio, frame.pcm)
            if self.audio_processor is not None and self.audio_processor.enabled
            else frame.pcm
        )
        if processed != frame.pcm:
            frame = AudioFrame(processed, frame.start_sample, frame.captured_at)
        always_on = bool(getattr(self, "wake_always_on", False))
        if always_on:
            if self.state.input_owner == "kws":
                if self.kws is None or self.kws.error is not None or not self.kws.running:
                    await self._fail_kws_closed(
                        self.kws.error if self.kws is not None and self.kws.error else "KWS worker unavailable"
                    )
                    return
                self.kws.submit(KwsAudioFrame(
                    pcm=frame.pcm,
                    start_sample=frame.start_sample,
                    end_sample=frame.end_sample,
                    captured_at=frame.captured_at,
                ))
                if self.kws.poll() is not None:
                    await self._activate_kws_detection()
                return
            if self.state.input_owner != "asr":
                return
        self._last_frame = frame
        self._recent.append(frame)
        while self._recent and frame.end_sample - self._recent[0].start_sample > max(1200, self.wake_preroll_ms) * 16:
            self._recent.popleft()
        # KWS has its own native decoder thread.  The controller only hands it
        # PCM while there is no active user turn; decoder work cannot stall
        # VAD, ASR queueing, or the asyncio event loop.
        if (self.workspace is None and self.wake_enabled and not always_on
                and self.kws is not None):
            self.kws.submit(KwsAudioFrame(
                pcm=frame.pcm,
                start_sample=frame.start_sample,
                end_sample=frame.end_sample,
                captured_at=frame.captured_at,
            ))
        if self._vad_origin is None:
            self._vad_origin = frame.start_sample
        if self._reset_vad_pending:
            await asyncio.to_thread(self.vad.reset)
            self._reset_vad_pending = False
        generation = self._vad_generation
        activity = await asyncio.to_thread(self.vad.feed, frame.pcm)
        if generation != self._vad_generation:
            return  # Turn was cancelled while native VAD was running.
        if activity.speech_started:
            self._speech_start_sample = self._vad_origin + (activity.speech_start_sample or 0)
            self._speech_end_sample = None
        if activity.speech_end_sample is not None:
            self._speech_end_sample = self._vad_origin + activity.speech_end_sample
            if self._speech_start_sample is not None:
                self._speech_samples += max(0, self._speech_end_sample - self._speech_start_sample)
                self._speech_start_sample = None

        # Preserve contiguous PCM for an existing turn *before* protected
        # barge-in may deliberately return ``None`` below.  Previously, a
        # speaker/AEC-rejected frame was omitted from ``ws.audio`` while
        # ``_last_frame`` correctly advanced.  When TTS completed and the
        # next frame was accepted, CurrentTurnAudioBuffer quite rightly saw
        # a one-block hole and raised ``audio capture gap``.  The rejected
        # frame remains excluded from user-turn semantics; this only keeps
        # the bounded source buffer sample-contiguous.
        workspace_before_activity = self.workspace
        if workspace_before_activity is not None:
            workspace_before_activity.audio.append(frame)
            if self._active_input:
                self._queue_asr(AudioChunk(self._sub_id, frame.pcm))

        activity = await self._observe_activity(activity, frame)
        if activity is None:
            return
        ws = self.workspace
        if ws is None:
            if self.wake_enabled and not always_on:
                if self.kws is None or self.kws.error is not None:
                    self.state.set_service("kws", "ERROR")
                    await self._error("KWS_WORKER_FAILED", self.kws.error if self.kws else "KWS worker unavailable", recoverable=True)
                    return
                detected = self.kws.poll()
                if detected is None:
                    return
                # A detection is a hard boundary: no queued suffix from the
                # wake phrase may trigger a second turn after this one ends.
                self.kws.reset()
            elif not (activity.speech_started or activity.is_speech):
                return
            await self._new_turn(frame)
            ws = self.workspace
        elif ws is not workspace_before_activity:
            # ``_observe_activity`` can await Agent work which finalizes an
            # old turn or resumes/replaces it.  A replacement workspace did
            # not exist at the continuity append above, so give it the frame
            # exactly once after returning from that await boundary.
            ws.audio.append(frame)
            if self._active_input:
                self._queue_asr(AudioChunk(self._sub_id, frame.pcm))

        speaker_client = getattr(self, "speaker_client", None)
        if speaker_client is not None and ws is not None:
            submitted = speaker_client.submit_frame(
                turn_id=ws.turn_id,
                generation=ws.generation,
                pcm=frame.pcm,
                start_sample=frame.start_sample,
                end_sample=frame.end_sample,
                # Hand the VAD verdict to the sidecar so its windows are gated
                # on real speech rather than a fixed amplitude threshold that
                # drifts with microphone gain.
                speech=bool(activity.is_speech),
            )
            if not submitted and speaker_client.degraded:
                # Queue overflow never backpressures this audio callback; it
                # only downgrades the optional service so the final gate will
                # pass the turn through.
                self.state.set_service("speaker", "DEGRADED")

        if not self._active_input:
            if activity.speech_started:
                self._resume_started = self._vad_origin + (activity.speech_start_sample or 0)
            near_voice = self._near_voice_activity()
            protected_available = self._barge_in_protected_available()
            decision = self.barge_in_gate.feed(
                activity,
                now=frame.captured_at,
                near_voice=near_voice,
                protected_available=protected_available,
            )
            if activity.speech_started or decision.confirmed:
                logger.info(
                    "controller.barge_in_gate turn_id=%s vad_speech=%s near_voice=%s protected=%s "
                    "candidate=%s confirmed=%s duration_ms=%d reason=%s",
                    ws.turn_id,
                    bool(activity.is_speech),
                    near_voice,
                    protected_available,
                    decision.candidate,
                    decision.confirmed,
                    decision.duration_ms,
                    decision.reason,
                )
            valid_resume = self._resume_started is not None and (
                (frame.end_sample if activity.is_speech else self._speech_end_sample or self._resume_started) - self._resume_started
            ) >= self.vad_min_speech_ms * 16
            if valid_resume and self._resume_started is not None and not decision.confirmed:
                speech_started_at = frame.captured_at - max(0, frame.end_sample - self._resume_started) / 16000
                self.barge_in_gate.prime(speech_started_at)
                decision = self.barge_in_gate.feed(
                    activity,
                    now=frame.captured_at,
                    near_voice=near_voice,
                    protected_available=protected_available,
                )
            if valid_resume:
                # A user continuation before the first output sample is the
                # existing same-turn revision path.  It must remain usable
                # when AEC is degraded because there is no assistant audio to
                # confuse the microphone.  Once output has started, only the
                # protected gate may interrupt it; a hard-closed turn is the
                # one deterministic exception used to start the next turn.
                has_started = ws.playback_started or bool(
                    self.player is not None
                    and getattr(self.player, "has_started", lambda _key: False)(ws.playback_key)
                )
                hard_boundary = ws.hard_closed or frame.captured_at - ws.started_at >= self.options.max_turn_ms / 1000
                if not has_started and not hard_boundary:
                    await self._resume(frame)
                elif decision.confirmed or hard_boundary:
                    # Invalidate the old generation before publishing the
                    # interruption state so a concurrent LLM delta cannot
                    # enqueue stale TTS between the snapshot and cancellation.
                    self.turns.invalidate()
                    snapshot = self._stop_playback_snapshot(ws.playback_key)
                    split = self._resume_started if ws.hard_closed else None
                    await self._state_event(TerminalState.INTERRUPTING, listening=True, turn_id=ws.turn_id)
                    await self._record_interruption(
                        ws.turn_id,
                        snapshot,
                        speech_onset_at=self.barge_in_gate.candidate_started_at,
                        interrupt_confirmed_at=frame.captured_at,
                    )
                    await self._cancel_turn(reason="interrupted", snapshot=snapshot)
                    if split is not None:
                        self._recent = deque(AudioFrame(f.pcm[max(0, split - f.start_sample) * 2:], max(split, f.start_sample), f.captured_at)
                                             for f in self._recent if f.end_sample > split)
                    await self._new_turn(frame)
                self._resume_started = None
            elif activity.speech_ended:
                self._resume_started = None
            return

        if activity.speech_started:
            self._last_candidate = 0
            if self._endpoint_task:
                self._endpoint_task.cancel()
        if activity.speech_ended:
            await self._segment(ws.raw_partial, pause=True)
        elapsed_ms = (frame.captured_at - ws.started_at) * 1000
        if elapsed_ms >= self.options.max_turn_ms:
            ws.hard_closed = True
            await self._freeze("max_duration", frame)
            return
        if self._speech_end_sample is None:
            return
        silence_ms = max(0, (frame.end_sample - self._speech_end_sample) / 16)
        if silence_ms < self.options.candidate_ms:
            return
        ws.timings["last_speech"] = frame.captured_at - (frame.end_sample - self._speech_end_sample) / 16000
        if not self.options.smart_turn or silence_ms >= self.options.hard_silence_ms:
            await self._freeze("hard_silence" if self.options.smart_turn else "vad_end", frame)
        elif (self._endpoint_task is None or self._endpoint_task.done()) and time.monotonic() - self._last_candidate >= self.options.recheck_ms / 1000:
            self._last_candidate = time.monotonic()
            self._endpoint_task = asyncio.create_task(self._endpoint(ws, frame))
            self._track(self._endpoint_task)

    async def _new_turn(self, frame: AudioFrame) -> None:
        turn_id = self.turns.begin()
        self.state.clear_current()
        self.conversation.begin_turn(turn_id)
        self.workspace = ws = TurnWorkspace(
            turn_id,
            started_at=frame.captured_at,
            speaker_mode=self.speaker_mode,
        )
        if self.speaker_router is not None:
            self.speaker_router.reset(turn_id=turn_id, generation=ws.generation)
        ws.timings["turn_started"] = ws.started_at
        self._turn_timings[turn_id] = ws.timings
        if len(self._turn_timings) > 64:
            self._turn_timings.pop(next(iter(self._turn_timings)), None)
        self._begin_tool_scope(turn_id)
        ws.audio = CurrentTurnAudioBuffer(min(62000, self.options.max_turn_ms + max(1200, self.wake_preroll_ms)) * 16)
        for previous in self._recent:
            ws.audio.append(previous)
        if self._speech_start_sample is not None:
            ws.started_at = frame.captured_at - max(0, frame.end_sample - self._speech_start_sample) / 16000
        self.segmenter = TranscriptSegmenter(self.options.min_segment_chars, self.options.max_segment_chars)
        self._active_input = True
        self.barge_in_gate.reset()
        self._last_candidate = 0
        self._turn_cancel = asyncio.Event()
        self._sub_parts.clear()
        self._sub_finals.clear()
        self._new_sub(bytes(ws.audio.pcm))
        for name, details in self.analysis_health.items():
            if isinstance(details, dict):
                ws.analysis[name] = dict(details)
        if self.player:
            self.player.block()
        await self._detail("turn.started")
        await self._state_event(TerminalState.LISTENING, listening=True, turn_id=turn_id)

    def _new_sub(self, pcm: bytes) -> None:
        self._sub_sequence += 1
        self._sub_id = f"{self.workspace.turn_id}/asr/{self._sub_sequence}"
        self._sub_parts[self._sub_id] = ""
        self._sub_finals[self._sub_id] = asyncio.Event()
        self._queue_asr(AudioChunk(self._sub_id, pcm))

    def _stop_generation(self) -> list[asyncio.Task]:
        stopped: list[asyncio.Task] = []
        for task in (self._completion_task, self._refine_task, self._endpoint_task):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                stopped.append(task)
                self._track(task)
        self._completion_task = self._refine_task = self._endpoint_task = None
        if self._turn_cancel:
            self._turn_cancel.set()
        return stopped

    async def _resume(self, frame: AudioFrame) -> None:
        ws = self.workspace
        # A same-turn continuation used to clear the frozen answer here even
        # when the Episode output had already atomically held it before the
        # first real PCM callback. That discarded a valid text/TTS candidate
        # and left its provider stream behind a closed player gate. Preserve
        # the held queue; explicit barge-in/lifecycle cancellation still uses
        # the stop-and-snapshot path when no passive hold is active.
        output = getattr(self, "output", None)
        if not bool(getattr(output, "playback_hold_active", False)):
            self._stop_playback_snapshot(ws.playback_key)
        ws.resume()
        # A resumed same-turn segment is a new semantic speech segment.  Pick
        # up a mode change made while the previous segment was finalizing, but
        # keep the old segment's captured mode for its already-running gate.
        ws.speaker_mode = self.speaker_mode
        if self.speaker_router is not None:
            # A resumed turn keeps its public turn_id but starts a new
            # speaker generation.  Any late embedding from the previous
            # generation must therefore be rejected just like a stale ASR or
            # refinement callback.
            self.speaker_router.reset(turn_id=ws.turn_id, generation=ws.generation)
        stopped = self._stop_generation()
        if stopped:
            await asyncio.gather(*stopped, return_exceptions=True)
        # A continuation is a new generation of the same turn.  Tool cache
        # entries and visible tool states from the interrupted generation must
        # not be reused or reported as if they belonged to the new input.
        self._reset_tool_scope(ws.turn_id)
        ws.tools.clear()
        ws.paraformer_final = ""
        ws.refined_text = ""
        ws.refinement_available = False
        ws.analysis = {
            name: dict(details)
            for name, details in self.analysis_health.items()
            if isinstance(details, dict)
        }
        self._turn_cancel = asyncio.Event()
        self._active_input = True
        keep_timings = {"turn_started", "first_segment", "asr_first_text"}
        keep_timings.update(k for k in ws.timings if k.startswith("tts_prewarm"))
        ws.timings = {k: v for k, v in ws.timings.items() if k in keep_timings}
        self._turn_timings[ws.turn_id] = ws.timings
        self.state.current.update(
            final_text="", assistant_text="", tts_text="", tts_status="IDLE",
            audio_buffer_ms=0, tts_returned_ms=0, tts_played_ms=0,
            tts_output_estimated_ms=0, tts_remaining_ms=0,
            delivery={},
        )
        self.barge_in_gate.reset()
        # New ASR cache for the resumed subsegment; earlier final callbacks still
        # update their own part and cannot overwrite the resumed part.
        seed = b"".join(f.pcm for f in self._recent if f.end_sample > (self._resume_started or frame.start_sample))
        self._new_sub(seed)
        await self._detail("turn.resumed")
        await self._state_event(TerminalState.LISTENING, listening=True, turn_id=ws.turn_id)

    async def _endpoint(self, ws: TurnWorkspace, frame: AudioFrame) -> None:
        token = ws.token
        await self._detail("turn.endpoint_candidate", audio_end_sample=frame.end_sample)
        try:
            reply = await self.analysis.analyze("endpoint", ws.audio.tail(), *token, self.options.smart_timeout_ms)
            if not self._valid(token) or not self._active_input:
                return
            result = reply["result"]
            ws.analysis["endpoint"] = {**result, "status": "READY"}
            ws.timings["smart_turn_ms"] = reply["elapsed_ms"]
            await self._detail("turn.semantic_state", **result)
            if result["probability"] >= self.options.threshold:
                await self._freeze("smart_turn", self._last_frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._valid(token):
                ws.analysis["endpoint"] = {"status": "DEGRADED", "error": str(exc) or "timeout"}
                await self._detail("turn.semantic_state", **ws.analysis["endpoint"])

    async def _freeze(self, reason: str, frame: AudioFrame) -> None:
        if not self._active_input:
            return
        ws = self.workspace
        self._active_input = False
        token = ws.freeze()
        if ws.hard_closed and self._speech_start_sample is not None:
            self._resume_started = frame.end_sample
        self._queue_asr(AudioChunk(self._sub_id, b"", is_final=True))
        ws.timings["endpoint_accepted"] = time.monotonic()
        if "last_speech" not in ws.timings:
            ws.timings["last_speech"] = frame.captured_at
        audio = bytes(ws.audio.pcm)
        self._refine_task = asyncio.create_task(self._refine(ws, token, audio)) if self.options.sensevoice else None
        self._track(self._refine_task)
        await self._detail("turn.finalizing", reason=reason)
        await self._state_event(TerminalState.FINALIZING, listening=True, turn_id=ws.turn_id)
        self._completion_task = asyncio.create_task(self._complete(ws, token, self._sub_id, self._refine_task))
        self._track(self._completion_task)

    async def _refine(self, ws, token, audio):
        try:
            reply = await self.analysis.analyze("refine", audio, *token, self.options.refine_timeout_ms)
            result = dict(reply["result"])
            if not result.get("complete") or not isinstance(result.get("text"), str):
                raise ValueError("incomplete refinement")
            refined_text = result["text"].strip()
            if self.wake_enabled:
                refined_text = strip_wake_prefix(refined_text, self.wake_keywords)
            result["text"] = refined_text
            # SenseVoice is a supplemental result.  Keep it attached to the
            # same workspace even after the fast Paraformer commit has
            # retired that workspace from the live input controller.
            ws.refined_text = refined_text
            ws.refinement_available = True
            ws.audio_cues = list(result.get("segments") or [])
            ws.timings["sensevoice_refine_ms"] = reply.get("elapsed_ms")
            ws.timings["refinement_completed"] = time.monotonic()
            ws.analysis["refine"] = self._analysis_meta("refine", status="READY")
            # A fast refinement can technically beat the ASR final callback.
            # Store it now, but only notify the Agent after the primary input
            # has crossed the formal commit boundary.
            if ws.committed:
                await self._refinement_completed(ws, result)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            details = self._analysis_meta("refine", status="DEGRADED")
            details.update({"error": str(exc) or "timeout", "fallback": "paraformer"})
            ws.analysis["refine"] = details
            return None

    async def _complete(self, ws, token, sub_id, refinement) -> None:
        try:
            try:
                await asyncio.wait_for(self._sub_finals[sub_id].wait(), 5)
            except asyncio.TimeoutError:
                await self._error("ASR_FINAL_TIMEOUT", "ASR final 超时，采用当前累计草稿。", recoverable=True, turn_id=ws.turn_id)
            if not self._valid(token):
                return
            ws.paraformer_final = "".join(self._sub_parts.values()).strip()
            # Paraformer is the first-class finalized input.  SenseVoice may
            # still be running and must not delay the Agent decision.
            ws.final_text = ws.paraformer_final
            if self.wake_enabled:
                ws.final_text = strip_wake_prefix(ws.final_text, self.wake_keywords)
            # If SenseVoice happened to finish first, retain its supplemental
            # fields; otherwise these remain the empty defaults until the
            # background refinement task completes.
            if not ws.refinement_available:
                ws.refined_text = ""
                ws.audio_cues = []
            refine_meta = ws.analysis.get("refine", {}) if isinstance(ws.analysis, dict) else {}
            refine_ready = isinstance(refine_meta, dict) and refine_meta.get("status") == "READY"
            speech_emotion = {
                "status": "READY" if refine_ready else "UNAVAILABLE",
                "source": (
                    refine_meta.get("model") or "SenseVoiceSmall"
                    if refine_ready else "paraformer_fallback"
                ),
                "segments": list(ws.audio_cues),
            }
            if isinstance(refine_meta, dict) and "model_loaded" in refine_meta:
                speech_emotion["model_loaded"] = bool(refine_meta["model_loaded"])
            valid_samples = self._speech_samples + (max(0, self._last_frame.end_sample - self._speech_start_sample) if self._speech_start_sample is not None else 0)
            if valid_samples < self.vad_min_speech_ms * 16:
                ws.final_text = ""
            ws.committed = True
            ws.timings["commit"] = time.monotonic()
            if "asr_final" in ws.timings:
                ws.timings["asr_final_to_commit_ms"] = round((ws.timings["commit"] - ws.timings["asr_final"]) * 1000)
            self.state.current.update(final_text=ws.final_text, partial_text="")
            await self._detail("transcript.final", text=ws.final_text, source="paraformer")
            await self._detail("speech.emotion", **speech_emotion)
            await self._detail("turn.committed")
            # EpisodeController retires the audio workspace as soon as the
            # primary input is committed.  Detach the refinement task first
            # so that normal turn cleanup does not cancel this late result.
            if self._refine_task is refinement:
                self._refine_task = None
            if await self._input_committed(ws, token):
                return
            if not ws.final_text:
                await self._finish_v2(ws, "")
                return
            await self._state_event(TerminalState.THINKING, listening=True, turn_id=ws.turn_id)
            response = await self._answer(ws, token)
            if self._valid(token):
                await self._finish_v2(ws, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._valid(token):
                await self._error("TURN_FAILED", str(exc), recoverable=True, turn_id=ws.turn_id)
                await self._finish_v2(ws, ws.staged_response)

    async def _on_asr_result(self, message: dict) -> None:
        sub_id, text = message.get("turn_id"), message.get("text")
        if message.get("type") == "error":
            if sub_id is None or sub_id in self._sub_parts:
                await self._cancel_turn(reason="failed", record=False)
                code = str(message.get("code") or "ASR_WORKER_ERROR")
                if code == "ASR_WORKER_DISCONNECTED":
                    await self._handle_asr_disconnect()
                await self._error(code, str(message.get("message")), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=self.state.listening)
            return
        if sub_id not in self._sub_parts or not isinstance(text, str) or self.workspace is None:
            return
        self._sub_parts[sub_id] = text or self._sub_parts[sub_id]
        ws = self.workspace
        if text and text.strip():
            ws.timings.setdefault("asr_first_text", time.monotonic())
            # The first non-empty local ASR result is the earliest reliable
            # signal that this turn may need an answer.  Start the Bidi
            # handshake in the background; it must never sit on the ASR/VAD
            # capture path or wait for endpoint/final/LLM work.
            self._start_tts_prewarm(ws)
        ws.raw_partial = "".join(self._sub_parts.values())
        if message.get("type") == "asr.final":
            ws.timings.setdefault("asr_final", time.monotonic())
            self._sub_finals[sub_id].set()
        if self._active_input:
            self.state.current["partial_text"] = ws.raw_partial
            await self._detail("transcript.partial", text=ws.raw_partial)
            await self._segment(ws.raw_partial)
        await self._latency(ws)

    async def _segment(self, text: str, *, pause: bool = False) -> None:
        ws = self.workspace
        result = self.segmenter.feed(text, pause=pause)
        if result is None or ws is None:
            return
        ws.segments = list(result.get("sentences") or [{"text": s} for s in result["segments"]])
        ws.timings.setdefault("first_segment", time.monotonic())
        await self._detail("transcript.segment", **result)

    def _on_playback_started(self, key: str) -> None:
        ws = self.workspace
        if ws is None or key != ws.playback_key or not ws.committed:
            return
        # Callback sets the boundary immediately, before the loop publishes it.
        ws.playback_started = True
        ws.timings.setdefault("playback", time.monotonic())
        if self._loop and not self._loop.is_closed():
            async def publish():
                if self.workspace is ws and key == ws.playback_key:
                    await self._state_event(TerminalState.SPEAKING, listening=True, turn_id=ws.turn_id)
                    await self._latency(ws)
            self._loop.call_soon_threadsafe(lambda: self._track(asyncio.create_task(publish())))

    async def _latency(self, ws, *, complete=False):
        if self.workspace is not ws:
            return
        t = ws.timings
        def delta(end, start):
            a, b = t.get(end), ws.started_at if start == "start" else t.get(start)
            return max(0, round((a - b) * 1000)) if a is not None and b is not None else None
        values = {
            "wake_detection_ms": None, "asr_first_text_ms": delta("asr_first_text", "start"),
            "speech_end_confirmation_ms": delta("endpoint_accepted", "last_speech"),
            "asr_final_to_commit_ms": delta("commit", "asr_final"),
            "llm_first_token_ms": delta("first_token", "llm_request"),
            "llm_request_to_first_tts_text_ms": delta("first_tts_text", "llm_request"),
            "tts_text_to_first_audio_ms": delta("first_audio", "first_tts_text"),
            "first_audio_to_playback_ms": delta("playback", "first_audio"),
            "speech_end_to_playback_ms": delta("playback", "last_speech"), "turn_total_ms": delta("completed", "start"),
            "first_segment_ms": delta("first_segment", "start"),
            "smart_turn_ms": t.get("smart_turn_ms"), "sensevoice_refine_ms": t.get("sensevoice_refine_ms"),
            "commit_to_final_llm_ms": delta("llm_request", "commit"),
            "refine_duration_ms": t.get("sensevoice_refine_ms"),
            "refine_to_redecision_ms": delta("refinement_redecision_started", "refinement_completed"),
            "agent_request_to_speak_ms": delta("speak_requested", "llm_request"),
            "tts_prewarm_requested_ms": delta("tts_prewarm_requested", "start"),
            "tts_prewarm_ready_ms": delta("tts_prewarm_ready", "tts_prewarm_requested"),
            "tts_checkout_ms": delta("tts_prewarm_checkout", "tts_prewarm_requested"),
            "tts_cold_start_ms": delta("tts_session_ready", "tts_session_start")
            if not t.get("tts_prewarm_used") else None,
            "speech_end_to_first_pcm_ms": delta("first_pcm_submitted", "last_speech"),
            "first_pcm_to_player_submit_ms": delta("first_pcm_submitted", "first_audio"),
            "replacement_count": int(t.get("replacement_count", 0) or 0),
        }
        self.state.latency = {"complete": complete, "values": values}
        await self._detail("latency.snapshot", complete=complete, values=values)

    async def _finish_v2(self, ws, response):
        if self.workspace is not ws:
            return
        delivery = {
            "status": "full" if response else "none",
            "termination": "completed",
            "heard_text": response,
            "uncertain_text": "",
            "unheard_text": "",
            "precision": "complete",
        }
        if self.player is not None:
            progress_reader = getattr(self.player, "progress", None)
            if callable(progress_reader):
                progress = progress_reader(ws.playback_key)
                delivery["played_ms"] = int(progress.get("output_estimated_ms", progress.get("played_ms", 0)) or 0)
                delivery["callback_played_ms"] = int(progress.get("played_ms", 0) or 0)
                delivery["output_tail_estimated_ms"] = max(
                    0,
                    delivery["callback_played_ms"] - delivery["played_ms"],
                )
                delivery["playback_precision"] = "device_time" if progress.get("device_output_time") is not None else "callback"
        if self.state.current.get("tts_status") == "DEGRADED":
            snapshot = self._stop_playback_snapshot(ws.playback_key)
            resolver = self._delivery_resolvers.get(ws.turn_id)
            if resolver is not None:
                delivery = resolver.resolve(snapshot or {"started": False}, response)
            else:
                delivery = {
                    "status": "unknown",
                    "heard_text": "",
                    "uncertain_text": response,
                    "unheard_text": "",
                    "precision": "unknown",
                    "played_ms": snapshot.output_estimated_ms if snapshot else 0,
                }
            delivery["termination"] = "failed"
            self._add_playback_delivery_fields(delivery, snapshot)
        ws.delivery = delivery
        self.state.current["delivery"] = dict(delivery)
        if ws.final_text:
            self.history.append_exchange(ws.final_text, delivery.get("heard_text", ""), delivery=delivery)
        ws.timings["completed"] = time.monotonic()
        await self._latency(ws, complete=True)
        latency = self.state.latency["values"]
        self.history.append_record(
            turn_id=ws.turn_id,
            transcript=ws.final_text,
            response=response,
            latency=latency,
            delivery=delivery,
        )
        # Preserve a provider/playback degradation that was reported while
        # the textual answer was still completed.  Replacing it with
        # FINISHED would make the WebUI hide the reason the tail was not
        # spoken, even though the turn itself was committed successfully.
        if self.state.current.get("tts_status") == "DEGRADED":
            self.state.current["audio_buffer_ms"] = 0
        else:
            self.state.current.update(tts_status="FINISHED", audio_buffer_ms=0)
        self._interrupted_turns.add(ws.turn_id)
        await self._detail(
            "turn.completed",
            transcript=ws.final_text,
            response=response,
            completed_at=utc_now_iso(),
            latency=latency,
            status=delivery.get("termination", "completed"),
            delivery=delivery,
        )
        await self._cancel_turn(reason="completed")
        await self._state_event(TerminalState.IDLE, listening=self.state.listening)

    async def _cancel_turn(
        self,
        *,
        reason: str = "stopped",
        snapshot=None,
        record: bool = True,
    ) -> None:
        ws = self.workspace
        if record and ws is not None and reason != "interrupted" and ws.turn_id not in self._interrupted_turns:
            if snapshot is None and self.player:
                snapshot = self._stop_playback_snapshot(ws.playback_key)
            await self._record_interruption(
                ws.turn_id,
                snapshot,
                termination=reason,
                publish_interrupted=False,
            )
        self.workspace = None
        self.turns.invalidate()
        self._active_input = False
        self.barge_in_gate.reset()
        if self.player and snapshot is None:
            # Gate and snapshot the old playback before sending a network
            # cancellation.  A late audio callback then has no route into the
            # next generation.
            self._stop_playback_snapshot(ws.playback_key if ws else None)
        active_tts = getattr(self, "_tts_session", None)
        keep_tts_session = False
        if active_tts is not None:
            cancel = getattr(active_tts, "cancel", None)
            if reason == "completed" and getattr(active_tts, "generation_finished", False):
                # Normal Bidi completion already crossed task_flushed.  Keep
                # the connection idle for the next turn; do not turn the
                # normal finish into a second task_cancel.
                keep_tts_session = True
            elif getattr(active_tts, "bidi", False) and cancel is not None:
                idle_generation = bool(getattr(active_tts, "generation_finished", False)) and not bool(
                    getattr(active_tts, "generation_active", True)
                )
                if idle_generation:
                    keep_tts_session = True
                elif self._tts_cancel_task is None or self._tts_cancel_task.done():
                    self._tts_cancel_task = asyncio.create_task(
                        self._cancel_bidi_in_background(active_tts, cancel),
                        name=f"tts-cancel-{ws.turn_id if ws else 'idle'}",
                    )
                    keep_tts_session = True
                else:
                    keep_tts_session = True
            elif cancel is not None:
                try:
                    keep_tts_session = bool(await asyncio.wait_for(cancel(timeout_seconds=2.0), 2.0))
                except Exception:
                    logger.warning("controller.tts_cancel_degraded turn_id=%s", ws.turn_id if ws else None, exc_info=True)
        stopped = self._stop_generation()
        if stopped:
            await asyncio.gather(*stopped, return_exceptions=True)
        if active_tts is not None and not (keep_tts_session and getattr(active_tts, "reusable", False)):
            await active_tts.close()
        if getattr(self, "_tts_session", None) is active_tts:
            self._tts_session = active_tts if keep_tts_session and getattr(active_tts, "reusable", False) else None
        if ws:
            self._cancel_tool_scope(ws.turn_id)
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

    async def stop_listening(self) -> None:
        self._activation_generation += 1
        recovery = self._asr_recovery_task
        if recovery is not None and recovery is not asyncio.current_task() and not recovery.done():
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        self._asr_recovery_task = None
        await super().stop_listening()
        if self._asr_sender:
            self._asr_sender.cancel()
            await asyncio.gather(self._asr_sender, return_exceptions=True)
            self._asr_sender = None
        while not self._asr_queue.empty():
            self._asr_queue.get_nowait()
            self._asr_queue.task_done()
        self._asr_queue_overflow = False
        self._asr_queue_overflow_at = None
        self._recent.clear()
        self._last_frame = None

    async def close(self) -> None:
        await super().close()
        self.registry.close()
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.wait(self._background, timeout=1)

    async def _observe_activity(self, activity, frame):
        return activity

    async def _input_committed(self, ws, token):
        return False

    def _begin_tool_scope(self, turn_id):
        self.registry.begin_turn(turn_id)

    def _reset_tool_scope(self, turn_id):
        self.registry.cancel_turn(turn_id)
        self.registry.begin_turn(turn_id)

    def _cancel_tool_scope(self, turn_id):
        self.registry.cancel_turn(turn_id)

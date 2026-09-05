from __future__ import annotations

import asyncio
from collections import deque
import logging
import time

from .turn_controller import TurnController
from .turn_workspace import AudioFrame, CurrentTurnAudioBuffer, TurnWorkspace
from .transcript_segmenter import TranscriptSegmenter
from .agent_scheduler import PredictionScheduler
from .options import V02Options
from ..analysis.client import AnalysisClient
from ..agent.runtime import AgentRuntime
from ..agent.speculative_tools import SpeculativeTools, ToolSelectionInvalid
from ..agent.tool_registry import FileTools
from ..asr.protocol import AudioChunk
from ..core.events import event, utc_now_iso
from ..core.state import TerminalState
from ..kws.keywords import strip_wake_prefix
from ..stream.text_bridge import TextBridge

logger = logging.getLogger(__name__)


class RealtimeController(TurnController):
    """v0.2 orchestration; v0.1 stays available through the feature switch."""

    def __init__(self, *, options: V02Options, analysis=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.options = options
        self.analysis = analysis or AnalysisClient(options.analysis_url)
        self.analysis_health: dict = {}
        self.registry = SpeculativeTools(FileTools(options.allowed_roots))
        self.agent = AgentRuntime(self.llm, self.registry, tools_enabled=options.tools)
        self.workspace: TurnWorkspace | None = None
        self.segmenter = TranscriptSegmenter(options.min_segment_chars, options.max_segment_chars)
        self.scheduler: PredictionScheduler | None = None
        self._endpoint_task: asyncio.Task | None = None
        self._refine_task: asyncio.Task | None = None
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
        self._last_frame: AudioFrame | None = None
        self._resume_started: int | None = None
        self._vad_epoch = 0
        self._reset_vad_pending = False

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
        return self.workspace is not None and self.workspace.token == token and self.turns.is_current(token[0])

    def _prediction_valid(self, ws: TurnWorkspace, token: tuple[str, int], revision: int) -> bool:
        return self._valid(token) and ws.input_revision == revision and self._active_input

    async def _detail(self, type_: str, **payload) -> None:
        ws = self.workspace
        if ws is None:
            return
        self.state.current.update(ws.snapshot(), turn_id=ws.turn_id, v02=True)
        await self._publish(event(type_, ws.turn_id, input_revision=ws.input_revision,
                                  generation_id=ws.generation_id, workspace=ws.snapshot(), **payload))

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
            if not await self._ensure_asr_ready():
                return
            await super().start_listening()
        finally:
            self.state.services["analysis"] = status
        if self.state.listening and (self._asr_sender is None or self._asr_sender.done()):
            self._asr_sender = asyncio.create_task(self._send_asr_loop())

    async def _ensure_asr_ready(self) -> bool:
        """Reconnect and perform an application-level ASR readiness check."""

        if self.asr is None:
            self.state.services["asr"] = "ERROR"
            await self._error("ASR_WORKER_UNAVAILABLE", "ASR Worker 客户端未初始化。", recoverable=True)
            await self._state_event(TerminalState.IDLE, listening=False)
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
            await self._state_event(TerminalState.IDLE, listening=False)
            return False

    async def _handle_asr_disconnect(self) -> None:
        """Stop capture after a transport loss and leave a restartable state."""

        self.state.services["asr"] = "ERROR"
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

    def _queue_asr(self, item) -> None:
        # No awaited network writes on the capture path. Overflow is a visible
        # failure, never a silent PCM drop or an unbounded queue.
        self._asr_queue.put_nowait(item)

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
                await self._cancel_turn()
                code = getattr(exc, "code", None)
                if not isinstance(code, str):
                    code = "ASR_WORKER_DISCONNECTED" if "ConnectionClosed" in type(exc).__name__ else "ASR_SEND_FAILED"
                # Any failed write leaves the sender's transport unusable.
                # Keep the recovery path identical for protocol disconnects
                # and other send failures so the capture loop cannot continue
                # feeding a dead ASR connection.
                await self._handle_asr_disconnect()
                await self._error(code, str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=False)
                return
            finally:
                self._asr_queue.task_done()

    async def _listen_loop(self) -> None:
        while self.state.listening and not self._closed:
            try:
                frame = await self.capture.read_frame()
                await self.handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._cancel_turn()
                self.state.listening = False
                try:
                    await self.capture.stop()
                except Exception:
                    logger.exception("controller.v02_audio_loop_stop_capture_failed")
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
                        logger.exception("controller.v02_audio_loop_close_asr_failed")
                self.state.services["audio"] = "READY" if self.capture is not None else "ERROR"
                self.state.services["kws"] = "READY" if self.wake_enabled and self.kws is not None else "DISABLED"
                self.state.services["asr"] = "READY" if self.asr is not None and self.vad is not None else "ERROR"
                await self._error("AUDIO_PIPELINE_FAILED", str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=False)
                break

    async def handle_frame(self, frame: AudioFrame) -> None:
        if self._last_frame and frame.start_sample != self._last_frame.end_sample:
            self._recent.clear()
            if self.workspace:
                raise ValueError("audio capture gap; current turn discarded")
        self._last_frame = frame
        self._recent.append(frame)
        while self._recent and frame.end_sample - self._recent[0].start_sample > max(1200, self.wake_preroll_ms) * 16:
            self._recent.popleft()
        if self._vad_origin is None:
            self._vad_origin = frame.start_sample
        if self._reset_vad_pending:
            await asyncio.to_thread(self.vad.reset)
            self._reset_vad_pending = False
        epoch = self._vad_epoch
        activity = await asyncio.to_thread(self.vad.feed, frame.pcm)
        if epoch != self._vad_epoch:
            return  # Turn was cancelled while native VAD was running.
        if activity.speech_started:
            self._speech_start_sample = self._vad_origin + (activity.speech_start_sample or 0)
            self._speech_end_sample = None
        if activity.speech_end_sample is not None:
            self._speech_end_sample = self._vad_origin + activity.speech_end_sample
            if self._speech_start_sample is not None:
                self._speech_samples += max(0, self._speech_end_sample - self._speech_start_sample)
                self._speech_start_sample = None

        ws = self.workspace
        if ws is None:
            if self.wake_enabled:
                detected = await asyncio.to_thread(self.kws.accept_pcm, frame.pcm) if self.kws else False
                if not detected:
                    return
            elif not (activity.speech_started or activity.is_speech):
                return
            await self._new_turn(frame)
            ws = self.workspace
        else:
            # Keep sample continuity in the bounded turn cache while a
            # frozen generation is awaiting refinement/answer.  The closed
            # ASR subsegment receives no more audio; a later pre-playback
            # resume can therefore append its first frame after the preserved
            # silence without being mistaken for a capture gap.
            ws.audio.append(frame)
            if self._active_input:
                self._queue_asr(AudioChunk(self._sub_id, frame.pcm))

        if not self._active_input:
            if activity.speech_started:
                self._resume_started = self._vad_origin + (activity.speech_start_sample or 0)
            valid_resume = self._resume_started is not None and (
                (frame.end_sample if activity.is_speech else self._speech_end_sample or self._resume_started) - self._resume_started
            ) >= self.vad_min_speech_ms * 16
            if valid_resume:
                played = self.player.block() if self.player else ws.playback_started
                if played or ws.playback_started or ws.hard_closed or frame.captured_at - ws.started_at >= self.options.max_turn_ms / 1000:
                    split = self._resume_started if ws.hard_closed else None
                    await self._cancel_turn()
                    if split is not None:
                        self._recent = deque(AudioFrame(f.pcm[max(0, split - f.start_sample) * 2:], max(split, f.start_sample), f.captured_at)
                                             for f in self._recent if f.end_sample > split)
                    await self._new_turn(frame)
                else:
                    await self._resume(frame)
                self._resume_started = None
            elif activity.speech_ended:
                self._resume_started = None
            return

        if activity.speech_started:
            ws.speech_version += 1
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
        self.workspace = ws = TurnWorkspace(turn_id, started_at=frame.captured_at)
        self.registry.begin_turn(turn_id)
        ws.audio = CurrentTurnAudioBuffer(min(62000, self.options.max_turn_ms + max(1200, self.wake_preroll_ms)) * 16)
        for previous in self._recent:
            ws.audio.append(previous)
        if self._speech_start_sample is not None:
            ws.started_at = frame.captured_at - max(0, frame.end_sample - self._speech_start_sample) / 16000
        self.segmenter = TranscriptSegmenter(self.options.min_segment_chars, self.options.max_segment_chars)
        self._active_input = True
        self._last_candidate = 0
        self._turn_cancel = asyncio.Event()
        self._sub_parts.clear()
        self._sub_finals.clear()
        self._new_sub(bytes(ws.audio.pcm))
        self._start_scheduler()
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

    def _start_scheduler(self) -> None:
        self.scheduler = PredictionScheduler(self._predict, self.options.prediction_interval_ms)

    def _stop_generation(self) -> None:
        if self.scheduler:
            self._track(self.scheduler.stop())
        for task in (self._completion_task, self._refine_task, self._endpoint_task):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                self._track(task)
        self._completion_task = self._refine_task = self._endpoint_task = None
        if self._turn_cancel:
            self._turn_cancel.set()

    async def _resume(self, frame: AudioFrame) -> None:
        ws = self.workspace
        ws.resume()  # Invalidate before cancelling or awaiting any operation.
        self._stop_generation()
        # A continuation is a new generation of the same turn.  Tool cache
        # entries and visible tool states from the interrupted generation must
        # not be reused or reported as if they belonged to the new input.
        self.registry.cancel_turn(ws.turn_id)
        self.registry.begin_turn(ws.turn_id)
        ws.tools.clear()
        ws.paraformer_final = ""
        ws.analysis = {
            name: dict(details)
            for name, details in self.analysis_health.items()
            if isinstance(details, dict)
        }
        self._turn_cancel = asyncio.Event()
        self._active_input = True
        ws.timings = {k: v for k, v in ws.timings.items() if k in {"first_segment", "first_prediction", "asr_first_text"}}
        self.state.current.update(
            final_text="", assistant_text="", tts_text="", tts_status="IDLE",
            audio_buffer_ms=0, tts_returned_ms=0, tts_played_ms=0, tts_remaining_ms=0,
        )
        # New ASR cache for the resumed subsegment; earlier final callbacks still
        # update their own part and cannot overwrite the resumed part.
        seed = b"".join(f.pcm for f in self._recent if f.end_sample > (self._resume_started or frame.start_sample))
        self._new_sub(seed)
        self._start_scheduler()
        await self._detail("turn.resumed")
        await self._state_event(TerminalState.LISTENING, listening=True, turn_id=ws.turn_id)

    async def _endpoint(self, ws: TurnWorkspace, frame: AudioFrame) -> None:
        version, token = ws.speech_version, ws.token
        await self._detail("turn.endpoint_candidate", audio_end_sample=frame.end_sample)
        try:
            reply = await self.analysis.analyze("endpoint", ws.audio.tail(), *token, self.options.smart_timeout_ms)
            if not self._valid(token) or ws.speech_version != version or not self._active_input:
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
        if self.scheduler:
            self._track(self.scheduler.stop())
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
            result = reply["result"]
            if not result.get("complete") or not isinstance(result.get("text"), str):
                raise ValueError("incomplete refinement")
            if self._valid(token):
                ws.timings["sensevoice_refine_ms"] = reply["elapsed_ms"]
                ws.analysis["refine"] = self._analysis_meta("refine", status="READY")
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._valid(token):
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
            result = await refinement if refinement else None
            if not self._valid(token):
                return
            ws.paraformer_final = "".join(self._sub_parts.values()).strip()
            ws.final_text = result["text"] if result is not None else ws.paraformer_final
            if self.wake_enabled:
                ws.final_text = strip_wake_prefix(ws.final_text, self.wake_keyword)
            ws.audio_cues = result.get("segments", []) if result else []
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
            self.state.current.update(final_text=ws.final_text, partial_text="")
            await self._detail("transcript.refined", text=ws.final_text, source="sensevoice" if result is not None else "paraformer")
            await self._detail("speech.emotion", **speech_emotion)
            await self._detail("transcript.final", text=ws.final_text)
            await self._detail("turn.committed")
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
                await self._cancel_turn()
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
        if text:
            ws.timings.setdefault("asr_first_text", time.monotonic())
        ws.raw_partial = "".join(self._sub_parts.values())
        if message.get("type") == "asr.final":
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
        ws.input_revision += 1
        ws.segments = [{"text": s} for s in result["segments"]]
        ws.timings.setdefault("first_segment", time.monotonic())
        await self._detail("transcript.segment", **result)
        if self.options.predictions and self.scheduler:
            self.scheduler.submit({"turn_id": ws.turn_id, "generation_id": ws.generation_id,
                                   "input_revision": ws.input_revision, "text": text,
                                   "new_segment": result["new_segment"], "final": False,
                                   "tools": list(ws.tools)})

    async def _predict(self, snapshot: dict) -> None:
        ws = self.workspace
        token = (snapshot["turn_id"], snapshot["generation_id"])
        revision = snapshot["input_revision"]
        if ws is None or not self._prediction_valid(ws, token, revision):
            return
        request_started = time.monotonic()
        ws.timings.setdefault("first_prediction_request", request_started)
        await self._prediction_event(ws, "agent.prediction.requested", token, revision,
                                     text=snapshot.get("text", ""), new_segment=snapshot.get("new_segment", ""))
        ws.agent_phase = "PREDICTING"
        try:
            result = await self.agent.predict(list(self.history.messages), snapshot)
            returned_at = time.monotonic()
            ws.timings["prediction_returned"] = returned_at
            if not self._prediction_valid(ws, token, revision):
                self._clear_prediction_if_current(ws, revision)
                await self._prediction_event(ws, "agent.prediction.discarded", token, revision,
                                             reason=self._prediction_drop_reason(ws, token, revision))
                return
            ws.prediction = result
            ws.prediction_revision = revision
            ws.timings.setdefault("first_prediction", returned_at)
            ws.agent_phase = "IDLE"
            await self._detail("agent.prediction", **result)
            if result["decision"] == "ACT" and self.options.tools:
                for index, call in enumerate(result["tools"]):
                    if not self._prediction_valid(ws, token, revision):
                        self._clear_prediction_if_current(ws, revision)
                        await self._prediction_event(ws, "agent.prediction.discarded", token, revision,
                                                     reason=self._prediction_drop_reason(ws, token, revision), stage="tools", index=index)
                        return
                    try:
                        entry, reused = await self.registry.select(
                            ws.turn_id, call["name"], call["arguments"],
                            valid=lambda: self._prediction_valid(ws, token, revision),
                        )
                        # select() performs its final check immediately before
                        # creating a new task.  Check once more before emitting
                        # state; there is no await between this check and task
                        # creation below.
                        if not self._prediction_valid(ws, token, revision):
                            self._clear_prediction_if_current(ws, revision)
                            await self._prediction_event(ws, "agent.prediction.discarded", token, revision,
                                                         reason=self._prediction_drop_reason(ws, token, revision), stage="tool_selected", index=index)
                            return
                        ws.agent_phase = "PREFETCHING"
                        published = await self._tool_event(
                            ws, {"name": call["name"], "status": "RUNNING", "speculative": True, "reused": reused,
                                 "selected_at_ms": max(0, round((entry.started_at - ws.started_at) * 1000)),
                                 "execution_start_ms": (max(0, round((entry.execution_started_at - ws.started_at) * 1000))
                                                         if entry.execution_started_at is not None else None)},
                            token=token, revision=revision,
                        )
                        if not published or not self._prediction_valid(ws, token, revision):
                            self._clear_prediction_if_current(ws, revision)
                            if not self._prediction_valid(ws, token, revision):
                                await self._prediction_event(ws, "agent.prediction.discarded", token, revision,
                                                             reason=self._prediction_drop_reason(ws, token, revision), stage="tool_event", index=index)
                            return
                        self._track(asyncio.create_task(self._prefetched(ws, token, snapshot["input_revision"], call["name"], entry)))
                    except ToolSelectionInvalid:
                        self._clear_prediction_if_current(ws, revision)
                        await self._prediction_event(ws, "agent.prediction.discarded", token, revision,
                                                     reason=self._prediction_drop_reason(ws, token, revision), stage="tool_select", index=index)
                        return
                    except (ValueError, KeyError, OSError) as exc:
                        await self._tool_event(ws, {"status": "FAILED", "error": str(exc)}, token=token, revision=revision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._prediction_valid(ws, token, revision):
                ws.agent_phase = "IDLE"
                ws.prediction = {"status": "DEGRADED", "error": str(exc) or "timeout"}
                await self._detail("agent.prediction", **ws.prediction)

    def _prediction_drop_reason(self, ws: TurnWorkspace, token: tuple[str, int], revision: int) -> str:
        if self.workspace is not ws or not self.turns.is_current(token[0]):
            return "turn_ended"
        if ws.generation_id != token[1]:
            return "generation_expired"
        if ws.input_revision != revision:
            return "revision_expired"
        if not self._active_input:
            return "input_frozen"
        return "invalidated"

    @staticmethod
    def _clear_prediction_if_current(ws: TurnWorkspace, revision: int) -> None:
        if ws.prediction_revision == revision:
            ws.prediction = {}
            ws.prediction_revision = None

    async def _prediction_event(self, ws: TurnWorkspace, type_: str, token: tuple[str, int], revision: int, **payload) -> None:
        current = self.workspace is ws and ws.token == token and ws.input_revision == revision
        if current:
            self.state.current.update(ws.snapshot(), turn_id=ws.turn_id, v02=True)
        await self._publish(event(type_, ws.turn_id, input_revision=revision, generation_id=token[1],
                                  workspace=ws.snapshot() if current else None, **payload))

    async def _prefetched(self, ws, token, revision, name, entry):
        try:
            await asyncio.shield(entry.task)
            status = "DONE"
        except asyncio.CancelledError:
            return
        except Exception:
            status = "FAILED"
        if self._valid(token) and ws.input_revision == revision and self._active_input:
            finished = entry.finished_at or time.monotonic()
            execution_started = entry.execution_started_at or entry.started_at
            await self._tool_event(ws, {
                "name": name, "status": status, "speculative": True,
                "selected_at_ms": max(0, round((entry.started_at - ws.started_at) * 1000)),
                "execution_start_ms": max(0, round((execution_started - ws.started_at) * 1000)),
                "finished_at_ms": max(0, round((finished - ws.started_at) * 1000)),
                "execution_ms": max(0, round((finished - execution_started) * 1000)),
            }, token=token, revision=revision)

    async def _tool_event(self, ws, payload, *, token=None, revision=None):
        if self.workspace is not ws:
            return False
        if token is not None and ws.token != token:
            return False
        if revision is not None and ws.input_revision != revision:
            return False
        ws.tools = (ws.tools + [payload])[-32:]
        await self._detail("agent.tool", **payload)
        return True

    async def _answer(self, ws, token) -> str:
        bridge = TextBridge(**self.bridge_options)
        session = None
        reader = None
        sender_task = None
        progress_task = None
        parts = []
        tts_failed: Exception | None = None
        tts_disabled = False
        playback_key = ws.playback_key
        pacing_enabled = bool(self.options.tts_pacing and self.player is not None)
        lead_ms = max(1, self.options.tts_lead_ms)
        # The queue keeps the LLM consumer independent from the playback
        # clock, while still putting a hard bound on text waiting for TTS.
        pending_segments: deque[str] = deque()
        pending_signal = asyncio.Event()
        audio_signal = asyncio.Event()
        progress_stop = asyncio.Event()
        pending_limit = 32
        producer_done = False
        audio_returned_bytes = 0
        progress_lock = asyncio.Lock()
        last_progress: tuple[int, int, int, int] | None = None
        last_progress_at = 0.0
        audio_bytes_per_second = (
            self.player.sample_rate * self.player.channels * 2
            if self.player is not None else 32000 * 2
        )
        if self.player:
            self.player.allow(playback_key)

        def playback_progress() -> dict[str, int | bool | str | None]:
            returned_ms = int(audio_returned_bytes * 1000 / audio_bytes_per_second)
            if self.player is None:
                played_ms = 0
                queued_ms = 0
                started = False
            else:
                current = self.player.progress(playback_key)
                played_ms = int(current["played_ms"])
                queued_ms = int(current["buffered_ms"])
                started = bool(current["started"])
            return {
                "returned_ms": returned_ms,
                "played_ms": played_ms,
                "remaining_ms": max(0, returned_ms - played_ms),
                "queued_ms": queued_ms,
                "started": started,
            }

        async def emit_progress(*, status: str | None = None, waiting_text: str | None = None,
                                trigger: str | None = None, force: bool = False) -> None:
            nonlocal last_progress, last_progress_at
            if not self._valid(token) or not ws.committed:
                return
            async with progress_lock:
                snapshot = playback_progress()
                now = time.monotonic()
                signature = (
                    int(snapshot["returned_ms"]), int(snapshot["played_ms"]),
                    int(snapshot["remaining_ms"]), int(snapshot["queued_ms"]),
                )
                # Audio callbacks can arrive dozens of times per second.  A
                # half-second diagnostic cadence is enough for the console
                # and WebUI while keeping the event bus and log bounded.
                if not force and (now - last_progress_at < 0.5 or signature == last_progress):
                    return
                last_progress = signature
                last_progress_at = now
                current_status = status or self.state.current.get("tts_status") or "IDLE"
                if waiting_text is not None:
                    self.state.current["tts_text"] = waiting_text
                self.state.current.update(
                    tts_returned_ms=int(snapshot["returned_ms"]),
                    tts_played_ms=int(snapshot["played_ms"]),
                    tts_remaining_ms=int(snapshot["remaining_ms"]),
                    audio_buffer_ms=int(snapshot["queued_ms"]),
                )
                logger.info(
                    "controller.tts_progress turn_id=%s status=%s returned_ms=%d played_ms=%d remaining_ms=%d queued_ms=%d trigger=%s",
                    ws.turn_id,
                    current_status,
                    int(snapshot["returned_ms"]),
                    int(snapshot["played_ms"]),
                    int(snapshot["remaining_ms"]),
                    int(snapshot["queued_ms"]),
                    trigger or "monitor",
                )
                await self._detail(
                    "tts.progress",
                    provider="minimax",
                    status=current_status,
                    waiting_text=waiting_text if waiting_text is not None else self.state.current.get("tts_text", ""),
                    audio_buffer_ms=int(snapshot["queued_ms"]),
                    audio_returned_ms=int(snapshot["returned_ms"]),
                    audio_played_ms=int(snapshot["played_ms"]),
                    audio_remaining_ms=int(snapshot["remaining_ms"]),
                    pacing_enabled=pacing_enabled,
                    lead_ms=lead_ms,
                    trigger=trigger,
                )

        async def read_audio(active_session):
            nonlocal audio_returned_bytes

            try:
                async for pcm in active_session.audio_chunks():
                    if not self._valid(token) or not ws.committed:
                        return
                    audio_returned_bytes += len(pcm)
                    audio_signal.set()
                    ws.timings.setdefault("first_audio", time.monotonic())
                    if self.player:
                        # Keep the native player bounded.  Audio held by the
                        # provider queue is still included in
                        # ``audio_remaining_ms`` through audio_returned_bytes.
                        while self.player.buffered_ms > 2000:
                            await asyncio.sleep(.02)
                            if not self._valid(token):
                                return
                        self.player.enqueue(playback_key, pcm)
                    self.state.current["tts_status"] = "STREAMING"
                    await emit_progress(status="STREAMING", trigger="audio_received")
                    await self._latency(ws)
            finally:
                # Wake a sender waiting for the first PCM even when the
                # provider ends or fails without returning audio.
                audio_signal.set()

        async def monitor_progress() -> None:
            try:
                while True:
                    try:
                        await asyncio.wait_for(progress_stop.wait(), .2)
                    except asyncio.TimeoutError:
                        pass
                    if progress_stop.is_set() or not self._valid(token) or not ws.committed:
                        return
                    await emit_progress()
            except asyncio.CancelledError:
                raise

        async def reader_exception() -> Exception | None:
            if reader is None or not reader.done() or reader.cancelled():
                return None
            return reader.exception()

        async def wait_for_audio(before_bytes: int) -> None:
            if not pacing_enabled:
                return
            deadline = asyncio.get_running_loop().time() + 15.0
            while audio_returned_bytes <= before_bytes:
                if not self._valid(token) or tts_disabled:
                    return
                receiver_error = await reader_exception()
                if receiver_error is not None:
                    raise receiver_error
                audio_signal.clear()
                if audio_returned_bytes > before_bytes:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("TTS 文本发送后未收到音频")
                try:
                    await asyncio.wait_for(audio_signal.wait(), remaining)
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("TTS 文本发送后未收到音频") from exc
            await emit_progress(status="STREAMING", trigger="audio_available")

        async def wait_for_playback_window() -> None:
            if not pacing_enabled:
                return
            deadline = asyncio.get_running_loop().time() + 600.0
            while self._valid(token) and not tts_disabled:
                snapshot = playback_progress()
                if int(snapshot["remaining_ms"]) <= lead_ms:
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("TTS 播放进度长时间没有下降")
                await emit_progress(status="WAITING_PLAYBACK", trigger="lead_window")
                await asyncio.sleep(.05)

        async def next_segment() -> str | None:
            while not pending_segments and not producer_done and not tts_disabled and self._valid(token):
                pending_signal.clear()
                if pending_segments or producer_done or tts_disabled:
                    continue
                await pending_signal.wait()
            if not pending_segments:
                return None
            segment = pending_segments.popleft()
            # Wake a producer waiting on the bounded queue.
            pending_signal.set()
            return segment

        async def tts_sender() -> None:
            nonlocal session, reader, progress_task, tts_failed, tts_disabled
            sent_any = False
            try:
                while True:
                    segment = await next_segment()
                    if segment is None:
                        return
                    if not self._valid(token):
                        return
                    if session is None:
                        session = self.tts.session()
                        await session.start()
                        reader = asyncio.create_task(read_audio(session), name=f"tts-audio-{ws.turn_id}")
                        progress_task = asyncio.create_task(monitor_progress(), name=f"tts-progress-{ws.turn_id}")
                    if sent_any:
                        await wait_for_playback_window()
                    receiver_error = await reader_exception()
                    if receiver_error is not None:
                        raise receiver_error
                    before_bytes = audio_returned_bytes
                    ws.timings.setdefault("first_tts_text", time.monotonic())
                    self.state.current["tts_text"] = segment
                    await session.send_text(segment)
                    await emit_progress(
                        status="QUEUED",
                        waiting_text=segment,
                        trigger="first_segment" if not sent_any else "remaining_below_lead",
                        force=True,
                    )
                    sent_any = True
                    # Do not send every ready segment while the first one is
                    # still being synthesized.  One audio return reopens the
                    # playback-window check and prevents a fast LLM from
                    # flooding task_continue frames.
                    await wait_for_audio(before_bytes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tts_failed = exc
                tts_disabled = True
                pending_segments.clear()
                pending_signal.set()
                progress_stop.set()
                logger.warning("controller.tts_degraded turn_id=%s error=%s", ws.turn_id, exc)

        async def send(segments):
            nonlocal sender_task
            if not segments or self.tts is None or tts_disabled or not self._valid(token):
                return
            if sender_task is None:
                sender_task = asyncio.create_task(tts_sender(), name=f"tts-sender-{ws.turn_id}")
            for segment in segments:
                if not segment or not segment.strip():
                    continue
                while len(pending_segments) >= pending_limit and not tts_disabled and self._valid(token):
                    pending_signal.clear()
                    if len(pending_segments) < pending_limit:
                        continue
                    await pending_signal.wait()
                if tts_disabled or not self._valid(token):
                    return
                pending_segments.append(segment)
                pending_signal.set()

        async def finish_send_queue() -> None:
            nonlocal producer_done
            producer_done = True
            pending_signal.set()

        async def tool_event(payload):
            if self._valid(token):
                await self._tool_event(ws, payload, token=token)

        try:
            ws.timings["llm_request"] = time.monotonic()
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
            package = {"final_text": ws.final_text, "provisional_transcript": ws.paraformer_final,
                       "audio_cues": ws.audio_cues, "speech_emotion": speech_emotion, "final": True}
            request_messages = self.agent.answer_messages(list(self.history.messages), package)
            await self._detail(
                "llm.context",
                messages=request_messages,
                message_count=len(request_messages),
            )
            async for delta in self.agent.answer(list(self.history.messages), package, ws.turn_id, tool_event):
                if not self._valid(token) or not ws.committed:
                    return ""
                if delta is None:
                    await send(bridge.finish())
                    continue
                ws.timings.setdefault("first_token", time.monotonic())
                parts.append(delta)
                self.state.current["assistant_text"] = "".join(parts)
                await self._detail("assistant.delta", text_delta=delta)
                await send(bridge.feed(delta))
                await self._latency(ws)
            ws.staged_response = "".join(parts).strip()
            await send(bridge.finish())
            await finish_send_queue()
            if sender_task is not None:
                await sender_task
            if session:
                try:
                    await session.finish()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tts_failed = tts_failed or exc
                    tts_disabled = True
                    logger.warning("controller.tts_finish_degraded turn_id=%s error=%s", ws.turn_id, exc)
                if reader is not None:
                    # MiniMax documents a 120-second receive idle window.  A
                    # fixed 60-second answer deadline could cut a legitimate
                    # long response; scale the wait with the text while
                    # retaining a finite upper bound for cleanup.
                    answer_timeout = min(600.0, max(120.0, 30.0 + len(ws.staged_response) * 0.25))
                    # Once the sender/receiver has reported a transport
                    # failure there is no reason to wait through the full
                    # provider idle window.  Give already queued PCM a short
                    # grace period, then finish with a visible DEGRADED
                    # status.  A healthy session still gets the full dynamic
                    # deadline above so long answers are not clipped.
                    reader_timeout = min(answer_timeout, 15.0) if tts_failed is not None else answer_timeout
                    try:
                        await asyncio.wait_for(reader, reader_timeout)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        tts_failed = tts_failed or exc
                        tts_disabled = True
                        logger.warning("controller.tts_receive_degraded turn_id=%s error=%s", ws.turn_id, exc)
            if self.player:
                playback_timeout = min(600.0, max(120.0, 30.0 + len(ws.staged_response) * 0.25 + self.player.buffered_ms / 1000))
                try:
                    await self.player.wait_until_empty(timeout_seconds=playback_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tts_failed = tts_failed or exc
                    tts_disabled = True
                    logger.warning("controller.playback_degraded turn_id=%s error=%s", ws.turn_id, exc)
            if session:
                if tts_failed is not None and self._valid(token):
                    self.state.current["tts_status"] = "DEGRADED"
                elif self._valid(token):
                    self.state.current["tts_status"] = "FINISHED"
                await emit_progress(
                    status=self.state.current.get("tts_status"),
                    trigger="playback_finished",
                    force=True,
                )
            if tts_failed is not None and self._valid(token):
                self.state.current["tts_status"] = "DEGRADED"
                await self._detail(
                    "tts.status",
                    provider="minimax",
                    status="DEGRADED",
                    waiting_text="",
                    audio_buffer_ms=self.player.buffered_ms if self.player else 0,
                    error=str(tts_failed),
                )
            return ws.staged_response
        finally:
            progress_stop.set()
            if progress_task and not progress_task.done():
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            if sender_task and not sender_task.done():
                sender_task.cancel()
                await asyncio.gather(sender_task, return_exceptions=True)
            if reader and not reader.done():
                reader.cancel()
                self._track(reader)
            if session:
                self._track(asyncio.create_task(session.close()))

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
            "llm_first_token_ms": delta("first_token", "llm_request"),
            "llm_request_to_first_tts_text_ms": delta("first_tts_text", "llm_request"),
            "tts_text_to_first_audio_ms": delta("first_audio", "first_tts_text"),
            "first_audio_to_playback_ms": delta("playback", "first_audio"),
            "speech_end_to_playback_ms": delta("playback", "last_speech"), "turn_total_ms": delta("completed", "start"),
            "first_segment_ms": delta("first_segment", "start"),
            "first_prediction_request_ms": delta("first_prediction_request", "start"),
            "first_prediction_ms": delta("first_prediction", "start"),
            "smart_turn_ms": t.get("smart_turn_ms"), "sensevoice_refine_ms": t.get("sensevoice_refine_ms"),
            "commit_to_final_llm_ms": delta("llm_request", "commit"),
        }
        self.state.latency = {"complete": complete, "values": values}
        await self._detail("latency.snapshot", complete=complete, values=values)

    async def _finish_v2(self, ws, response):
        if self.workspace is not ws:
            return
        if response:
            self.history.append_exchange(ws.final_text, response)
        ws.timings["completed"] = time.monotonic()
        await self._latency(ws, complete=True)
        latency = self.state.latency["values"]
        self.history.append_record(turn_id=ws.turn_id, transcript=ws.final_text, response=response, latency=latency)
        # Preserve a provider/playback degradation that was reported while
        # the textual answer was still completed.  Replacing it with
        # FINISHED would make the WebUI hide the reason the tail was not
        # spoken, even though the turn itself was committed successfully.
        if self.state.current.get("tts_status") == "DEGRADED":
            self.state.current["audio_buffer_ms"] = 0
        else:
            self.state.current.update(tts_status="FINISHED", audio_buffer_ms=0)
        await self._detail("turn.completed", transcript=ws.final_text, response=response, completed_at=utc_now_iso(), latency=latency)
        await self._cancel_turn()
        await self._state_event(TerminalState.IDLE, listening=self.state.listening)

    async def _cancel_turn(self) -> None:
        ws, self.workspace = self.workspace, None
        self.turns.invalidate()
        self._active_input = False
        self._stop_generation()
        if self.player:
            self.player.block()
        if ws:
            self.registry.cancel_turn(ws.turn_id)
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

    async def stop_listening(self) -> None:
        await super().stop_listening()
        if self._asr_sender:
            self._asr_sender.cancel()
            await asyncio.gather(self._asr_sender, return_exceptions=True)
            self._asr_sender = None
        while not self._asr_queue.empty():
            self._asr_queue.get_nowait()
            self._asr_queue.task_done()
        self._recent.clear()
        self._last_frame = None

    async def close(self) -> None:
        await super().close()
        self.registry.close()
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.wait(self._background, timeout=1)

from __future__ import annotations

import asyncio
from collections import deque
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


class RealtimeController(TurnController):
    """v0.2 orchestration; v0.1 stays available through the feature switch."""

    def __init__(self, *, options: V02Options, analysis=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.options = options
        self.analysis = analysis or AnalysisClient(options.analysis_url)
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
                status = "READY" if all(health.get(k, {}).get("status") == "READY" for k, enabled in
                                        (("endpoint", self.options.smart_turn), ("refine", self.options.sensevoice)) if enabled) else "DEGRADED"
            except Exception:
                status = "DEGRADED"
        else:
            status = "DISABLED"
        self.state.services["analysis"] = status
        await self._state_event(TerminalState.IDLE, listening=False)

    async def start_listening(self) -> None:
        # Analysis is optional and never becomes a mandatory start prerequisite.
        status = self.state.services.pop("analysis", "DISABLED")
        try:
            await super().start_listening()
        finally:
            self.state.services["analysis"] = status
        if self.state.listening and (self._asr_sender is None or self._asr_sender.done()):
            self._asr_sender = asyncio.create_task(self._send_asr_loop())

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
                await self._error("ASR_SEND_FAILED", str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=self.state.listening)
            finally:
                self._asr_queue.task_done()

    async def _listen_loop(self) -> None:
        while self.state.listening and not self._closed:
            frame = await self.capture.read_frame()
            try:
                await self.handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._cancel_turn()
                await self._error("AUDIO_PIPELINE_FAILED", str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=self.state.listening)

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
        self._turn_cancel = asyncio.Event()
        self._active_input = True
        ws.timings = {k: v for k, v in ws.timings.items() if k in {"first_segment", "first_prediction", "asr_first_text"}}
        self.state.current.update(final_text="", assistant_text="", tts_text="", tts_status="IDLE", audio_buffer_ms=0)
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
                ws.analysis["refine"] = {"status": "READY"}
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._valid(token):
                ws.analysis["refine"] = {"status": "DEGRADED", "error": str(exc) or "timeout", "fallback": "paraformer"}
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
            valid_samples = self._speech_samples + (max(0, self._last_frame.end_sample - self._speech_start_sample) if self._speech_start_sample is not None else 0)
            if valid_samples < self.vad_min_speech_ms * 16:
                ws.final_text = ""
            ws.committed = True
            ws.timings["commit"] = time.monotonic()
            self.state.current.update(final_text=ws.final_text, partial_text="")
            await self._detail("transcript.refined", text=ws.final_text, source="sensevoice" if result is not None else "paraformer")
            await self._detail("speech.emotion", segments=ws.audio_cues)
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
                await self._error("ASR_WORKER_ERROR", str(message.get("message")), recoverable=True)
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
        parts = []
        playback_key = ws.playback_key
        if self.player:
            self.player.allow(playback_key)

        async def read_audio():
            async for pcm in session.audio_chunks():
                if not self._valid(token) or not ws.committed:
                    return
                ws.timings.setdefault("first_audio", time.monotonic())
                if self.player:
                    while self.player.buffered_ms > 2000:
                        await asyncio.sleep(.02)
                        if not self._valid(token):
                            return
                    self.player.enqueue(playback_key, pcm)
                    self.state.current["audio_buffer_ms"] = self.player.buffered_ms
                self.state.current["tts_status"] = "STREAMING"
                await self._detail("tts.status", provider="minimax", status="STREAMING", waiting_text=self.state.current["tts_text"], audio_buffer_ms=self.state.current["audio_buffer_ms"])
                await self._latency(ws)

        async def send(segments):
            nonlocal session, reader
            if not segments or self.tts is None or not self._valid(token):
                return
            if session is None:
                session = self.tts.session()
                await session.start()
                reader = asyncio.create_task(read_audio())
            for segment in segments:
                if not self._valid(token):
                    return
                ws.timings.setdefault("first_tts_text", time.monotonic())
                self.state.current["tts_text"] = segment
                await session.send_text(segment)

        async def tool_event(payload):
            if self._valid(token):
                await self._tool_event(ws, payload, token=token)

        try:
            ws.timings["llm_request"] = time.monotonic()
            package = {"final_text": ws.final_text, "provisional_transcript": ws.paraformer_final,
                       "audio_cues": ws.audio_cues, "final": True}
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
            if session:
                await session.finish()
                await asyncio.wait_for(reader, 60)
            if self.player:
                await self.player.wait_until_empty(timeout_seconds=60)
            return ws.staged_response
        finally:
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

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from .speech_pipeline import SpeechPipeline
from .turn_workspace import TurnWorkspace
from ..core.state import RuntimeState
from ..tts.alignment import DeliveryResolver


class SpeechHandle:
    def __init__(self, owner, identity, turn_id, generation):
        self.owner, self.identity, self.turn_id = owner, identity, turn_id
        self.speech_id = uuid4().hex
        self.queue = asyncio.Queue(maxsize=32)
        self.workspace = TurnWorkspace(turn_id, generation_id=generation, committed=True)
        self.reason = None
        self.snapshot = None
        self.text = ""
        self.task = None
        self.finished = False
        self.overlap_start_ms = None

    async def _put(self, value):
        if self.reason or self.task.done():
            raise ValueError("speech output closed")
        put = asyncio.create_task(self.queue.put(value))
        try:
            await asyncio.wait({put, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if not put.done() or self.reason:
                raise ValueError("speech output closed")
            await put
        finally:
            if not put.done():
                put.cancel()
            await asyncio.gather(put, return_exceptions=True)

    async def feed(self, text):
        self.text += text
        await self._put(text)

    async def finish(self):
        await self._put(None)
        self.finished = True

    async def stream(self):
        while True:
            value = await self.queue.get()
            if value is None:
                return
            yield value


class EpisodeSpeechOutput(SpeechPipeline):
    """Independent output transaction sharing the existing audio pipeline."""
    def __init__(self, controller):
        self.controller = controller
        self.tts, self.player = controller.tts, controller.player
        self.options, self.bridge_options = controller.options, controller.bridge_options
        self.state = RuntimeState()
        self._delivery_resolvers = {}
        self._tts_session = None
        self.handle = None
        self.generation = 0
        self.tasks = set()
        self.runtime = None
        self.loop = None

    @property
    def active(self):
        return self.handle is not None and not self.handle.task.done() and self.handle.reason is None

    @property
    def status(self):
        if not self.active:
            return "IDLE"
        return "SPEAKING" if self.handle.workspace.playback_started else "SPEAK_REQUESTED"

    def _valid(self, token):
        h, r = self.handle, self.runtime
        return bool(h and h.workspace.token == token and not h.reason and r and not r.closed
                    and r.episode_id == h.identity[0] and r.agent_step_id == h.identity[1]
                    and r.context_version == h.identity[2]
                    and r.user_speaking is False)

    async def _await_tts_cancel_barrier(self):
        # begin() has already joined cleanup of the preceding transaction.
        return

    async def _detail(self, kind, **payload):
        h = self.handle
        if h:
            await self.runtime.emit("speech.progress", {
                "speech_id": h.speech_id, "episode_id": h.identity[0], "agent_step_id": h.identity[1],
                "context_version": h.identity[2], "turn_id": h.turn_id,
                "generation_id": h.workspace.generation_id, "event": kind, **payload})

    async def _latency(self, ws):
        return

    def playback_started(self, key):
        h = self.handle
        if h and key == h.workspace.playback_key:
            h.workspace.playback_started = True
            h.workspace.timings.setdefault("playback", time.monotonic())
            if self.loop and not self.loop.is_closed():
                async def publish():
                    if self.handle is h and not h.reason:
                        await self.runtime.log("TTS_STARTED", identity=h.identity, turn_id=h.turn_id,
                                               speech_id=h.speech_id)
                self.loop.call_soon_threadsafe(lambda: self.controller._track(asyncio.create_task(publish())))

    async def begin(self, identity, turn_id):
        if self.active:
            raise ValueError("speech output busy")
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.runtime.gate(identity):
            raise ValueError("speech authorization expired during cleanup")
        self.generation += 1
        self.loop = asyncio.get_running_loop()
        h = SpeechHandle(self, identity, turn_id, self.generation)
        self.handle = h
        self.state.clear_current()
        self._delivery_resolvers = {turn_id: DeliveryResolver()}
        h.task = asyncio.create_task(self._run(h), name=f"speech-{h.speech_id}")
        self.tasks.add(h.task)
        h.task.add_done_callback(self.tasks.discard)
        # Ensure the transaction's finally block is installed before exposing
        # a cancellable handle to observations on another task.
        await asyncio.sleep(0)
        return h

    def invalidate(self, reason):
        h = self.handle
        if not h or h.task.done() or h.reason:
            return
        h.reason = reason
        if self.player:
            h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
        h.task.cancel()

    async def _run(self, h):
        # Record completion even if cancellation occurs before the first delta.
        try:
            if not self._valid(h.workspace.token):
                h.reason = h.reason or "stale"
                return
            h.workspace.timings["llm_request"] = self.runtime.request_started_at or time.monotonic()
            await self._play_stream(h.workspace, h.workspace.token, h.stream())
            if self.state.current.get("tts_status") == "DEGRADED" or self.tts is None or self.player is None:
                h.reason = "failed"
        except asyncio.CancelledError:
            h.reason = h.reason or "stopped"
        except Exception as exc:
            h.reason = "failed"
            await self.runtime.log("SPEECH_ERROR", identity=h.identity, turn_id=h.turn_id,
                                   speech_id=h.speech_id, error=str(exc)[:300])
        finally:
            session = self._tts_session
            if session is not None and h.reason:
                try:
                    cancel = getattr(session, "cancel", None)
                    if cancel:
                        await asyncio.wait_for(cancel(timeout_seconds=2.0), 2.5)
                except Exception:
                    await session.close()
                finally:
                    self._tts_session = None
            if h.snapshot is None and self.player:
                h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
            delivery = self._delivery_resolvers[h.turn_id].resolve(
                h.snapshot or {"started": False}, h.text, overlap_start_ms=h.overlap_start_ms)
            delivery["termination"] = h.reason or "completed"
            if h.snapshot:
                self.controller._add_playback_delivery_fields(delivery, h.snapshot)
            timings = h.workspace.timings
            input_finished_at = self.runtime.input_finished_at.get(h.turn_id)
            input_to_playback = None
            if input_finished_at is not None and "playback" in timings:
                input_to_playback = max(0, round((timings["playback"] - input_finished_at) * 1000))
            def elapsed(a, b):
                return round((timings[b] - timings[a]) * 1000) if a in timings and b in timings else None
            record = {"speech_id": h.speech_id, "episode_id": h.identity[0], "agent_step_id": h.identity[1],
                      "context_version": h.identity[2], "turn_id": h.turn_id,
                      "generation_id": h.workspace.generation_id, "text": h.text,
                      "status": h.reason or "completed", "delivery": delivery,
                      "latency": {"llm_first_token_ms": elapsed("llm_request", "first_token"),
                                  "llm_request_to_first_tts_text_ms": elapsed("llm_request", "first_tts_text"),
                                  "tts_text_to_first_audio_ms": elapsed("first_tts_text", "first_audio"),
                                  "first_audio_to_playback_ms": elapsed("first_audio", "playback"),
                                  "input_end_to_playback_ms": input_to_playback}}
            if self.handle is h:
                self.handle = None
            await self.runtime.output_completed(record)

    async def close(self):
        self.invalidate("stopped")
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self._tts_session:
            await self._tts_session.close()
            self._tts_session = None

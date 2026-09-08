from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from uuid import uuid4

from .speech_pipeline import SpeechPipeline
from .turn_workspace import TurnWorkspace
from ..core.state import RuntimeState
from ..tts.alignment import DeliveryResolver


logger = logging.getLogger(__name__)


class SpeechHandle:
    def __init__(self, owner, identity, turn_id, _queue_order, emotion, *, kind="formal"):
        self.owner, self.identity, self.turn_id = owner, identity, turn_id
        self.speech_id = uuid4().hex
        self.emotion_requested = emotion
        self.emotion_applied = emotion
        self.kind = kind
        self.queue = asyncio.Queue(maxsize=32)
        self.workspace = TurnWorkspace(turn_id, epoch=identity[2], phase="FINALIZING", committed=True)
        self.reason = None
        self.snapshot = None
        self.text = ""
        self.task = None
        self.finished = False
        self.overlap_start_ms = None
        self.queued_at = time.monotonic()
        self.started = asyncio.Event()

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
        if self.reason or self.task.done():
            raise ValueError("speech output closed")
        self.text += text
        await self.owner._text_delta(self, text)
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
    """A serial speech queue sharing the existing audio pipeline.

    A handle is created as soon as the Agent authorises speech, while a single
    worker starts the actual TTS/player transaction in FIFO order.  This keeps
    Agent intent durable during a prefill without allowing concurrent player
    access.
    """
    def __init__(self, controller):
        self.controller = controller
        self.tts, self.player = controller.tts, controller.player
        self.options, self.bridge_options = controller.options, controller.bridge_options
        self.state = RuntimeState()
        self._delivery_resolvers = {}
        self._tts_session = None
        self.handle = None
        self._pending = deque()
        self._worker = None
        self.generation = 0
        self.tasks = set()
        self.runtime = None
        self.loop = None
        self.last_replacement = {}

    @property
    def active(self):
        return self.handle is not None and not self.handle.task.done() and self.handle.reason is None

    @property
    def status(self):
        if not self.active:
            return "IDLE"
        return "SPEAKING" if self.handle.workspace.playback_started else "SPEAK_REQUESTED"

    @property
    def active_snapshot(self):
        h = self.handle
        if not h or h.task.done():
            return None
        return {"speech_id": h.speech_id, "conversation_id": h.identity[0], "trace_id": h.identity[1],
                "turn_id": h.turn_id, "epoch": h.workspace.epoch, "text": h.text,
                "status": self.status, "kind": h.kind, "emotion_requested": h.emotion_requested,
                "emotion_applied": h.emotion_applied}

    @property
    def queue_snapshot(self):
        return [{"speech_id": item.speech_id, "kind": item.kind, "text": item.text,
                 "conversation_id": item.identity[0], "trace_id": item.identity[1],
                 "turn_id": item.turn_id, "epoch": item.workspace.epoch,
                 "status": "QUEUED"}
                for item in self._pending if not item.reason and not item.task.done()]

    def _valid(self, token):
        h, r = self.handle, self.runtime
        return bool(h and h.workspace.token == token and not h.reason and r and not r.closed
                    and r.valid(h.identity) and r.user_speaking is False)

    async def _await_tts_cancel_barrier(self):
        # begin() has already joined cleanup of the preceding transaction.
        return

    async def _detail(self, kind, **payload):
        h = self.handle
        if h:
            await self.runtime.emit("speech.progress", {
                "speech_id": h.speech_id, "conversation_id": h.identity[0], "turn_id": h.turn_id,
                "epoch": h.workspace.epoch, "event": kind, **payload})

    async def _text_delta(self, h, text):
        if not text:
            return
        await self.runtime.emit("speech.text_delta", {
            "speech_id": h.speech_id, "conversation_id": h.identity[0], "turn_id": h.turn_id,
            "epoch": h.workspace.epoch, "text_delta": text,
            "emotion_requested": h.emotion_requested, "emotion_applied": h.emotion_applied,
        })

    async def _latency(self, ws):
        return

    def playback_started(self, key):
        h = self.handle
        if h and key == h.workspace.playback_key:
            h.workspace.playback_started = True
            started_at = time.monotonic()
            h.workspace.timings.setdefault("playback", started_at)
            # PcmPlayer invokes this callback from the first callback that
            # consumes the new generation.  Keep the explicit name for the
            # end-to-end latency metric while retaining ``playback`` for the
            # existing delivery contract.
            h.workspace.timings.setdefault("first_pcm_submitted", started_at)
            source_timings = getattr(self.controller, "_turn_timings", {}).get(h.turn_id)
            if isinstance(source_timings, dict):
                source_timings.setdefault("playback", started_at)
                source_timings.setdefault("first_pcm_submitted", started_at)
            if self.player is not None:
                self.controller.state.current["playback_diagnostics"] = self.player.diagnostics
            if self.loop and not self.loop.is_closed():
                async def publish():
                    if self.handle is h and not h.reason:
                        await self.runtime.log("TTS_STARTED", identity=h.identity, turn_id=h.turn_id,
                                               speech_id=h.speech_id)
                        started = getattr(self.runtime, "output_started", None)
                        if h.kind == "prefill" and callable(started):
                            await started({"speech_id": h.speech_id, "turn_id": h.turn_id,
                                           "kind": h.kind, "status": "playing"})
                self.loop.call_soon_threadsafe(lambda: self.controller._track(asyncio.create_task(publish())))

    async def begin(self, identity, turn_id, *, emotion="calm", kind="formal"):
        if self.runtime.gate(identity):
            raise ValueError("speech authorization expired during cleanup")
        self.generation += 1
        self.loop = asyncio.get_running_loop()
        h = SpeechHandle(self, identity, turn_id, self.generation, emotion, kind=kind)
        source_timings = getattr(self.controller, "_turn_timings", {}).get(turn_id)
        if isinstance(source_timings, dict):
            source_timings.setdefault("llm_request", getattr(self.runtime, "request_started_at", None) or time.monotonic())
            source_timings["speak_requested"] = time.monotonic()
            h.workspace.timings.update(source_timings)
        h.task = asyncio.create_task(self._wait_then_run(h), name=f"speech-{h.speech_id}")
        self.tasks.add(h.task)
        h.task.add_done_callback(self.tasks.discard)
        self._pending.append(h)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain(), name="speech-queue")
        await self.runtime.emit("speech.queued", {"speech_id": h.speech_id, "conversation_id": identity[0],
                                 "turn_id": turn_id, "epoch": identity[2], "kind": kind, "text": h.text, "status": "QUEUED"})
        await asyncio.sleep(0)
        return h

    async def replace_current(self, identity, turn_id, *, emotion="calm"):
        """Replace all output based on the current decision in one tool call.

        The caller validates the new text incrementally before invoking this
        method.  Stopping the old generation first closes the player gate and
        cancels queued output, so no stale PCM can overtake the replacement.
        """

        if self.runtime.gate(identity):
            raise ValueError("speech authorization expired during replacement")
        old = self.handle
        old_candidate = (
            old if old and not old.task.done() and not old.reason else
            next((item for item in self._pending if not item.task.done() and not item.reason), None)
        )
        old_speech_id = old_candidate.speech_id if old_candidate else None
        self.last_replacement = {
            "old_speech_id": old_speech_id,
            "new_speech_id": None,
        }
        source_timings = getattr(self.controller, "_turn_timings", {}).get(turn_id)
        if old_candidate is not None and isinstance(source_timings, dict):
            source_timings["replacement_count"] = int(source_timings.get("replacement_count", 0) or 0) + 1
        self.invalidate("replaced")
        if old_speech_id:
            await self.runtime.emit("speech.replaced", {
                "speech_id": old_speech_id,
                "conversation_id": identity[0],
                "turn_id": turn_id,
                "epoch": identity[2],
                "event": "speech.replaced",
                "status": "replaced",
            })
        handle = await self.begin(identity, turn_id, emotion=emotion, kind="formal")
        self.last_replacement["new_speech_id"] = handle.speech_id
        return handle

    async def _drain(self):
        while self._pending:
            h = self._pending.popleft()
            if h.reason or h.task.done():
                continue
            self.handle = h
            self.state.clear_current()
            prepare = getattr(self.tts, "prepare_emotion", None)
            if prepare is not None:
                h.emotion_applied = await prepare(h.emotion_requested)
            self._delivery_resolvers[h.speech_id] = DeliveryResolver()
            h.started.set()
            await asyncio.gather(h.task, return_exceptions=True)
            if self.handle is h:
                self.handle = None

    async def _wait_then_run(self, h):
        await h.started.wait()
        await self._run(h)

    async def enqueue_text(self, identity, turn_id, text, *, emotion="calm", kind="prefill"):
        h = await self.begin(identity, turn_id, emotion=emotion, kind=kind)
        await h.feed(text)
        await h.finish()
        return h

    def invalidate(self, reason):
        items = ([self.handle] if self.handle else []) + list(self._pending)
        for h in items:
            if not h or h.task.done() or h.reason:
                continue
            h.reason = reason
            if h is not self.handle:
                h.task.cancel()
                continue
            logger.info(
            "episode.speech_invalidated speech_id=%s episode_id=%s step=%s context_version=%s reason=%s playback_started=%s",
            h.speech_id, h.identity[0], h.identity[1], h.identity[2], reason,
            h.workspace.playback_started,
            )
            if self.player:
                h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
            h.task.cancel()

    def cancel(self, speech_id, reason="cancelled"):
        """Cancel exactly one queued or active speech item.

        ``clear_prefill`` uses this after a candidate has already become a
        SpeechItem.  Cancelling by ID prevents an explicit prefill withdrawal
        from invalidating a later formal response in the same FIFO queue.
        """
        if not isinstance(speech_id, str) or not speech_id:
            return False
        items = ([self.handle] if self.handle else []) + list(self._pending)
        for h in items:
            if h is None or h.speech_id != speech_id or h.task.done() or h.reason:
                continue
            h.reason = reason
            if h is self.handle and self.player:
                h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
            h.task.cancel()
            return True
        return False

    async def _run(self, h):
        # Record completion even if cancellation occurs before the first delta.
        try:
            if not self._valid(h.workspace.token):
                h.reason = h.reason or "stale"
                return
            source_timings = getattr(self.controller, "_turn_timings", {}).get(h.turn_id)
            if isinstance(source_timings, dict):
                h.workspace.timings.update(source_timings)
            h.workspace.timings["llm_request"] = self.runtime.request_started_at or time.monotonic()
            await self._play_stream(h.workspace, h.workspace.token, h.stream())
            # A zero-PCM Bidi transaction cannot have delivered any text, so
            # one replay is safe and avoids making the user repeat a request
            # after a cancelled/non-reusable MiniMax session.  Never retry
            # once any PCM was returned: that could duplicate audible speech.
            if (
                h.text
                and h.workspace.timings.get("tts_text_sent")
                and h.workspace.timings.get("tts_audio_bytes", 0) == 0
                and self._valid(h.workspace.token)
            ):
                await self.runtime.log(
                    "TTS_EMPTY_AUDIO",
                    identity=h.identity,
                    turn_id=h.turn_id,
                    speech_id=h.speech_id,
                    attempt=1,
                )
                logger.warning(
                    "episode.speech_retry_empty_audio speech_id=%s turn_id=%s",
                    h.speech_id,
                    h.turn_id,
                )
                self._delivery_resolvers[h.speech_id] = DeliveryResolver()
                self.state.clear_current()

                async def replay():
                    yield h.text

                await self._play_stream(h.workspace, h.workspace.token, replay())
                if h.workspace.timings.get("tts_text_sent") and h.workspace.timings.get("tts_audio_bytes", 0) == 0:
                    await self.runtime.log(
                        "TTS_EMPTY_AUDIO_RETRY_FAILED",
                        identity=h.identity,
                        turn_id=h.turn_id,
                        speech_id=h.speech_id,
                        attempt=2,
                    )
            if getattr(self.tts, "emotion_status", None) == "unsupported":
                h.emotion_applied = getattr(self.tts, "applied_emotion", "calm")
                await self.runtime.log("TTS_EMOTION_UNSUPPORTED", identity=h.identity, turn_id=h.turn_id,
                                       speech_id=h.speech_id,
                                       error=getattr(self.tts, "emotion_error", None))
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
                    # A successfully cancelled MiniMax Bidi generation is
                    # intentionally not reusable.  Closing it here prevents
                    # a detached socket from being retained by the client.
                    if not getattr(session, "reusable", False):
                        await session.close()
                except Exception:
                    await session.close()
                finally:
                    self._tts_session = None
            if h.snapshot is None and self.player:
                h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
            delivery = self._delivery_resolvers[h.speech_id].resolve(
                h.snapshot or {"started": False}, h.text, overlap_start_ms=h.overlap_start_ms)
            delivery["termination"] = h.reason or "completed"
            if h.snapshot:
                self.controller._add_playback_delivery_fields(delivery, h.snapshot)
            source_timings = getattr(self.controller, "_turn_timings", {}).get(h.turn_id)
            if isinstance(source_timings, dict):
                h.workspace.timings.update(source_timings)
            timings = h.workspace.timings
            # Output completion is the final boundary for a speech-bearing
            # turn.  The Episode controller may have recorded an earlier
            # provisional completion when it committed the input, so update
            # the shared timestamp before emitting the settled record.
            completed_at = time.monotonic()
            timings["completed"] = completed_at
            if isinstance(source_timings, dict):
                source_timings["completed"] = completed_at
            input_finished_at = self.runtime.input_finished_at.get(h.turn_id)
            input_to_playback = None
            if input_finished_at is not None and "playback" in timings:
                input_to_playback = max(0, round((timings["playback"] - input_finished_at) * 1000))
            def elapsed(a, b):
                return round((timings[b] - timings[a]) * 1000) if a in timings and b in timings else None
            def since_turn(key):
                return round((timings[key] - timings["turn_started"]) * 1000) \
                    if key in timings and "turn_started" in timings else None
            turn_latency = {
                "turn_id": h.turn_id,
                "input_id": h.turn_id,
                "speech_id": h.speech_id,
                "asr_first_text_ms": since_turn("asr_first_text"),
                "speech_end_confirmation_ms": elapsed("last_speech", "endpoint_accepted"),
                "asr_final_ms": since_turn("asr_final"),
                "finalized_ms": since_turn("commit"),
                "llm_first_token_ms": since_turn("first_token"),
                "speak_requested_ms": since_turn("speak_requested"),
                "tts_first_audio_ms": since_turn("first_audio"),
                "player_first_pcm_submitted_ms": since_turn("first_pcm_submitted"),
                "refinement_ms": timings.get("sensevoice_refine_ms"),
                "prewarm_status": getattr(self.controller.workspace, "tts_prewarm_status", None)
                if getattr(self.controller, "workspace", None) is not None else timings.get("tts_prewarm_status"),
                "prewarm_used": bool(timings.get("tts_prewarm_used")),
                "replacement_count": int(timings.get("replacement_count", 0) or 0),
            }
            # The input workspace may already be retired when playback ends.
            # Start from the controller's finalized-turn snapshot so the
            # output-owned record still contains one complete latency view.
            settled_latency = dict(
                getattr(getattr(self.controller, "state", None), "latency", {}).get("values") or {}
            )
            settled_latency.update({
                "llm_first_token_ms": elapsed("llm_request", "first_token"),
                "llm_request_to_first_tts_text_ms": elapsed("llm_request", "first_tts_text"),
                "tts_text_to_first_audio_ms": elapsed("first_tts_text", "first_audio"),
                "first_audio_to_playback_ms": elapsed("first_audio", "playback"),
                "input_end_to_playback_ms": input_to_playback,
                "asr_final_to_commit_ms": elapsed("asr_final", "commit"),
                "speech_end_confirmation_ms": elapsed("last_speech", "endpoint_accepted"),
                "refine_to_redecision_ms": elapsed("refinement_completed", "refinement_redecision_started"),
                "tts_prewarm_requested_ms": since_turn("tts_prewarm_requested"),
                "speech_end_to_first_pcm_ms": elapsed("last_speech", "first_pcm_submitted"),
                "first_pcm_to_player_submit_ms": elapsed("first_audio", "first_pcm_submitted"),
                "agent_request_to_speak_ms": elapsed("llm_request", "speak_requested"),
                "refine_duration_ms": timings.get("sensevoice_refine_ms"),
                "tts_prewarm_ready_ms": elapsed("tts_prewarm_requested", "tts_prewarm_ready"),
                "tts_checkout_ms": elapsed("tts_prewarm_requested", "tts_prewarm_checkout"),
                "tts_cold_start_ms": elapsed("tts_session_start", "tts_session_ready")
                if not timings.get("tts_prewarm_used") else None,
                "replacement_count": int(timings.get("replacement_count", 0) or 0),
                "turn_total_ms": since_turn("completed"),
            })
            record = {"speech_id": h.speech_id, "conversation_id": h.identity[0], "trace_id": h.identity[1],
                       # Keep the semantic names used by AgentEpisodeRuntime
                       # alongside the public compatibility aliases above.
                       "episode_id": h.identity[0], "agent_step_id": h.identity[1],
                       "context_version": h.identity[2], "input_id": h.turn_id,
                       "turn_id": h.turn_id, "epoch": h.workspace.epoch, "text": h.text,
                       "kind": h.kind, "queued_at": h.queued_at,
                      "emotion_requested": h.emotion_requested, "emotion_applied": h.emotion_applied,
                      "emotion_transport": getattr(self.tts, "emotion_transport", "voice_setting"),
                      "emotion_status": getattr(self.tts, "emotion_status", "applied"),
                       "status": h.reason or "completed", "delivery": delivery,
                       "turn_latency": turn_latency,
                        "latency": {**settled_latency,
                                     "queue_to_playback_ms": round((timings["playback"] - h.queued_at) * 1000)
                                     if "playback" in timings else None}}
            logger.info(
                "episode.speech_final speech_id=%s episode_id=%s step=%s context_version=%s turn_id=%s status=%s delivery=%s tts_text_sent=%s tts_audio_bytes=%s playback_started=%s",
                h.speech_id, h.identity[0], h.identity[1], h.identity[2], h.turn_id,
                record["status"], delivery.get("status"), timings.get("tts_text_sent"),
                timings.get("tts_audio_bytes"), h.workspace.playback_started,
            )
            if self.handle is h:
                self.handle = None
            source_timings = getattr(self.controller, "_turn_timings", {}).get(h.turn_id)
            if isinstance(source_timings, dict):
                source_timings.update(h.workspace.timings)
            await self.runtime.output_completed(record)

    async def close(self):
        self.invalidate("stopped")
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self._tts_session:
            await self._tts_session.close()
            self._tts_session = None

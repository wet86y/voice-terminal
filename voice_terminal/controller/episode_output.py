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


def _identity_pair(identity):
    """Return stable correlation fields without trusting diagnostic input.

    Older live runtimes occasionally supplied ``(conversation_id,)`` while
    winding down a request.  Correlation must be best-effort: a missing trace
    ID may reduce diagnostics, but must never make barge-in fail inside the
    audio-frame loop.
    """
    if isinstance(identity, (tuple, list)):
        return (
            identity[0] if len(identity) > 0 else None,
            identity[1] if len(identity) > 1 else None,
        )
    return identity, None


class SpeechHandle:
    def __init__(self, owner, identity, turn_id, _queue_order, emotion, *, kind="formal"):
        self.owner, self.identity, self.turn_id = owner, _identity_pair(identity), turn_id
        self.speech_id = uuid4().hex
        self.emotion_requested = emotion
        self.emotion_applied = emotion
        self.kind = kind
        self.queue = asyncio.Queue(maxsize=32)
        self.output_generation = owner.generation
        self.workspace = TurnWorkspace(turn_id, generation=self.output_generation,
                                       phase="FINALIZING", committed=True)
        self.reason = None
        self.snapshot = None
        self.text = ""
        self.task = None
        self.finished = False
        # A replacement must never borrow the socket that is still flushing
        # the superseded transaction.  The pipeline binds the concrete
        # session to this handle, not to the output singleton.
        self.tts_session = None
        self.force_fresh_tts_session = False
        self.overlap_start_ms = None
        self.queued_at = time.monotonic()
        self.started = asyncio.Event()
        self.playback_ready = asyncio.Event()
        self.state = "CREATING"

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
    """Generation-owned output transactions sharing the audio pipeline.

    A superseded transaction is locally stopped synchronously, while its
    provider cancellation is allowed to finish in its own task.  This makes a
    new LLM decision eligible for TTS immediately instead of serialising it
    behind the previous MiniMax cancel/close barrier.
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
        self._handles_by_token = {}
        self.generation = 0
        self.tasks = set()
        self.runtime = None
        self.loop = None
        self._playback_hold = False
        self._playback_release = asyncio.Event()
        self._playback_release.set()
        self.last_replacement = {}
        self.last_delivery = None
        # The most recently settled output is diagnostic/reconciliation state,
        # never an active continuation target.  Agent requests may use a full
        # delivery from the preceding turn as a last-resort duplicate guard,
        # but it must not leak back into ``speak_snapshot`` as current speech.
        self.last_completed_speech = None

    @property
    def active(self):
        return self.handle is not None and not self.handle.task.done() and self.handle.reason is None

    @property
    def status(self):
        if not self.active:
            return "IDLE"
        if self.handle and self.handle.state == "SUSPENDED":
            return "SUSPENDED"
        if self.handle and self.handle.state == "HELD":
            return "HELD"
        return "SPEAKING" if self.handle.workspace.playback_started else "SPEAK_REQUESTED"

    @property
    def playback_hold_active(self) -> bool:
        """Whether pre-playback output is currently held by user speech."""

        return self._playback_hold

    @property
    def active_snapshot(self):
        h = self.handle
        if not h or h.task.done():
            return None
        return {"speech_id": h.speech_id, "conversation_id": h.identity[0], "trace_id": h.identity[1],
                "turn_id": h.turn_id, "output_generation": h.workspace.generation, "text": h.text,
                "status": self.status, "kind": h.kind, "speech_state": h.state, "emotion_requested": h.emotion_requested,
                "emotion_applied": h.emotion_applied}

    @property
    def queue_snapshot(self):
        return [{"speech_id": item.speech_id, "kind": item.kind, "text": item.text,
                 "conversation_id": item.identity[0], "trace_id": item.identity[1],
                 "turn_id": item.turn_id, "output_generation": item.workspace.generation,
                 "status": "QUEUED"}
                for item in self._pending if not item.reason and not item.task.done()]

    def _valid(self, token):
        h, r = self.handle, self.runtime
        return bool(h and h.workspace.token == token and not h.reason and r and not r.closed)

    async def _wait_for_playback_permission(self, ws, token):
        """Wait while user speech owns the pre-playback output boundary."""
        while self._valid(token):
            h = self._handle_for_workspace(ws)
            if h is None or h.workspace.playback_started or not self._playback_hold:
                return
            await self._playback_release.wait()

    def _unplayed_candidate(self):
        candidates = ([self.handle] if self.handle else []) + list(self._pending)
        for h in candidates:
            task = getattr(h, "task", None) if h else None
            task_done = task.done() if task is not None else False
            if (h and h.state != "SUSPENDED" and not h.reason and not task_done
                    and not h.workspace.playback_started):
                return h
        return None

    def _schedule_output_event(self, h, trace_kind, event, **payload):
        """Publish hold diagnostics without blocking the audio frame path."""
        loop = self.loop
        if loop is None or loop.is_closed() or self.runtime is None:
            return

        async def publish():
            log = getattr(self.runtime, "log", None)
            if callable(log):
                await log(trace_kind, identity=h.identity, turn_id=h.turn_id,
                          speech_id=h.speech_id, **payload)
            await self._emit_handle_progress(h, event, **payload)

        task = loop.create_task(publish(), name=f"speech-{event}-{h.speech_id}")
        track = getattr(self.controller, "_track", None)
        if callable(track):
            track(task)

    def hold_unplayed(self) -> str | None:
        """Hold all output that has not crossed the real playback boundary."""
        self._playback_hold = True
        self._playback_release.clear()
        h = self._unplayed_candidate()
        if h is None:
            return None

        if self.player is not None:
            hold = getattr(self.player, "hold", None)
            if callable(hold) and not hold(h.workspace.playback_key):
                has_started = getattr(self.player, "has_started", None)
                if callable(has_started) and has_started(h.workspace.playback_key):
                    # The player won the race and has already crossed the
                    # playback boundary. The caller will use the confirmed
                    # barge-in path on the next activity frame.
                    self._playback_hold = False
                    self._playback_release.set()
                    return None

        h.state = "HELD"
        h.playback_ready.clear()
        if "playback_hold_started" not in h.workspace.timings:
            started_at = time.monotonic()
            h.workspace.timings["playback_hold_started"] = started_at
            self._schedule_output_event(
                h, "TTS_HELD", "tts_hold_started",
                reason="user_speaking_pre_playback",
                hold_started_at=started_at,
            )
        return h.speech_id

    async def _await_tts_cancel_barrier(self):
        # Provider cancellation belongs to the old handle's background
        # cleanup.  A new generation is deliberately never gated on it.
        return

    def _handle_for_workspace(self, ws):
        return self._handles_by_token.get(ws.token)

    def _new_tts_session(self, ws, *, fresh=False):
        """Create and bind a provider session to this output transaction."""
        h = self._handle_for_workspace(ws)
        fresh = bool(fresh or (h and h.force_fresh_tts_session))
        try:
            session = self.tts.session(fresh=fresh)
        except TypeError:  # Non-MiniMax/test providers retain the old API.
            session = self.tts.session()
        if h is not None:
            h.tts_session = session
        if h is self.handle:
            self._tts_session = session  # compatibility for controller prewarm state
        return session

    def _bind_tts_session(self, ws, session):
        h = self._handle_for_workspace(ws)
        if h is not None:
            h.tts_session = session
        if h is self.handle:
            self._tts_session = session

    def _release_tts_session(self, ws, session, *, retain):
        # A stale generation must never clear or retain the new generation's
        # session pointer after its reader finally exits.
        h = self._handle_for_workspace(ws)
        if h is self.handle and self._tts_session is session:
            self._tts_session = session if retain else None

    async def _emit_handle_progress(self, h, event, **payload):
        if self.runtime is None:
            return
        await self.runtime.emit("speech.progress", {
            "speech_id": h.speech_id,
            "conversation_id": h.identity[0],
            "turn_id": h.turn_id,
            "output_generation": h.workspace.generation,
            "event": event,
            **payload,
        })

    async def _detail(self, kind, **payload):
        h = self.handle
        if h:
            await self.runtime.emit("speech.progress", {
                "speech_id": h.speech_id, "conversation_id": h.identity[0], "turn_id": h.turn_id,
                "output_generation": h.workspace.generation, "event": kind, **payload})

    async def _text_delta(self, h, text):
        if not text:
            return
        await self.runtime.emit("speech.text_delta", {
            "speech_id": h.speech_id, "conversation_id": h.identity[0], "turn_id": h.turn_id,
            "output_generation": h.workspace.generation, "text_delta": text,
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
                self.loop.call_soon_threadsafe(lambda: self.controller._track(asyncio.create_task(publish())))

    async def begin(self, identity, turn_id, *, emotion="calm", kind="formal",
                    _generation_advanced=False, _fresh_session=False, _defer_activation=False):
        if self.runtime.gate(identity):
            raise ValueError("speech authorization expired during cleanup")
        # Keep at most one latest text candidate behind the active playback.
        # The queue stores text handles only; it never owns provider PCM.
        for pending in list(self._pending):
            if pending is not self.handle and not pending.reason and not pending.task.done():
                pending.reason = "superseded"
                pending.task.cancel()
        if not _generation_advanced:
            self.generation += 1
        self.loop = asyncio.get_running_loop()
        h = SpeechHandle(self, identity, turn_id, self.generation, emotion, kind=kind)
        h.force_fresh_tts_session = _fresh_session
        self._handles_by_token[h.workspace.token] = h
        if (getattr(self.runtime, "user_speaking", False) is False
                and not self._playback_hold and not _defer_activation):
            h.playback_ready.set()
            h.state = "READY"
        else:
            h.state = "HELD"
        source_timings = getattr(self.controller, "_turn_timings", {}).get(turn_id)
        if isinstance(source_timings, dict):
            source_timings.setdefault("llm_request", getattr(self.runtime, "request_started_at", None) or time.monotonic())
            source_timings["speak_requested"] = time.monotonic()
            h.workspace.timings.update(source_timings)
        h.task = asyncio.create_task(self._wait_then_run(h), name=f"speech-{h.speech_id}")
        self.tasks.add(h.task)
        def forget_finished(task, handle=h):
            self.tasks.discard(task)
            # A held/superseded task can be cancelled before it ever enters
            # _run() (and therefore before _run's final cleanup executes).
            if self.handle is not handle:
                self._handles_by_token.pop(handle.workspace.token, None)
        h.task.add_done_callback(forget_finished)
        # Register the candidate before awaiting emotion preparation.  User
        # speech can arrive during that await; keeping the handle visible lets
        # ``hold_unplayed`` mark it HELD instead of only setting the global
        # gate and leaving the handle READY.
        self._pending.append(h)
        if h.playback_ready.is_set():
            await self._activate(h)
        await self.runtime.emit("speech.queued", {"speech_id": h.speech_id, "conversation_id": h.identity[0],
                                 "turn_id": turn_id, "output_generation": h.workspace.generation,
                                 "kind": kind, "text": h.text, "status": h.state})
        await asyncio.sleep(0)
        return h

    async def append_successor(self, identity, turn_id, *, baseline_speech_id, baseline_generation,
                               suffix, emotion="calm"):
        """Queue a verified suffix without withdrawing the audible speech.

        A completed MiniMax Bidi generation cannot safely accept another
        ``task_continue`` through the old worker.  Keep the public operation
        at the output boundary so callers never need to know that detail: the
        successor waits behind the active playback, synthesises only ``suffix``
        and therefore never repeats the already accepted prefix.  A future
        same-session continuation can replace this implementation without
        changing the agent reconciliation contract.
        """
        if not suffix:
            return None
        active = self.handle
        same_active = bool(
            active and not active.reason and not active.task.done()
            and active.speech_id == baseline_speech_id
            and active.workspace.generation == baseline_generation
        )
        handle = await self.begin(
            identity, turn_id, emotion=(active.emotion_requested if same_active else emotion),
            kind="append", _defer_activation=same_active,
        )
        await handle.feed(suffix)
        event = "speech.appended" if same_active else "speech.append_successor"
        await self.runtime.emit(event, {
            "speech_id": active.speech_id if same_active else handle.speech_id,
            "successor_speech_id": handle.speech_id,
            "conversation_id": handle.identity[0], "turn_id": turn_id,
            "output_generation": handle.workspace.generation,
            "baseline_speech_id": baseline_speech_id,
            "baseline_generation": baseline_generation,
            "baseline_chars": len(active.text) if same_active else 0, "appended_chars": len(suffix),
            "cumulative_chars": (len(active.text) + len(suffix)) if same_active else len(suffix),
            "provider_generation": handle.workspace.generation,
        })
        return handle

    async def replace_current(self, identity, turn_id, *, emotion="calm"):
        """Replace all output based on the current decision in one tool call.

        The caller validates the new text incrementally before invoking this
        method.  Stopping the old generation first closes the player gate and
        cancels queued output, so no stale PCM can overtake the replacement.
        """

        if self.runtime.gate(identity):
            raise ValueError("speech authorization expired during replacement")
        # Advance the transaction generation before closing the old PCM gate.
        # Late audio/progress from the old token will fail ``_valid`` even if
        # its socket has not finished its provider-side cancellation.
        self.generation += 1
        old = self.handle
        old_candidate = (
            old if old and not old.task.done() and not old.reason else
            next((item for item in self._pending if not item.task.done() and not item.reason), None)
        )
        old_speech_id = old_candidate.speech_id if old_candidate else None
        self.last_replacement = {
            "old_speech_id": old_speech_id,
            "new_speech_id": None,
            "old_generation": old_candidate.workspace.generation if old_candidate else None,
            "new_generation": self.generation,
            "reason": "replaced",
        }
        source_timings = getattr(self.controller, "_turn_timings", {}).get(turn_id)
        if old_candidate is not None and isinstance(source_timings, dict):
            source_timings["replacement_count"] = int(source_timings.get("replacement_count", 0) or 0) + 1
        if old_candidate is not None:
            old_candidate.workspace.timings["replacement_detected"] = time.monotonic()
            await self._emit_handle_progress(old_candidate, "replacement_detected",
                                             reason="replaced", new_generation=self.generation)
        self.cancel_all("replaced")
        handle = await self.begin(identity, turn_id, emotion=emotion, kind="formal",
                                  _generation_advanced=True, _fresh_session=True)
        self.last_replacement["new_speech_id"] = handle.speech_id
        handle.workspace.timings["replacement_detected"] = time.monotonic()
        if old_candidate is not None:
            await self.runtime.emit("speech.replaced", {
                # Keep the old ``speech_id`` correlation field compatible;
                # consumers that understand replacement can use both IDs.
                "speech_id": old_speech_id,
                "old_speech_id": old_speech_id,
                "new_speech_id": handle.speech_id,
                "conversation_id": handle.identity[0],
                "turn_id": turn_id,
                "old_generation": old_candidate.workspace.generation,
                "new_generation": handle.workspace.generation,
                "output_generation": handle.workspace.generation,
                "reason": "replaced",
                "event": "speech.replaced",
                "status": "replaced",
            })
        return handle

    async def _activate(self, h):
        """Give one generation immediate ownership of TTS/player state."""
        if h.reason or h.task.done():
            return
        try:
            self._pending.remove(h)
        except ValueError:
            pass
        self.handle = h
        self.state.clear_current()
        prepare = getattr(self.tts, "prepare_emotion", None)
        if prepare is not None:
            try:
                h.emotion_applied = await prepare(
                    h.emotion_requested,
                    detach_active=h.force_fresh_tts_session,
                )
            except TypeError:  # third-party/test providers use the old hook
                h.emotion_applied = await prepare(h.emotion_requested)
        # A barge-in/reset may have cancelled this handle while an emotion
        # provider yielded.  Do not revive its TTS permission afterwards.
        if h.reason or h.task.done():
            was_active = self.handle is h
            if was_active:
                self.handle = None
            self._handles_by_token.pop(h.workspace.token, None)
            return
        self._delivery_resolvers[h.speech_id] = DeliveryResolver()
        h.started.set()

    async def _wait_then_run(self, h):
        await h.started.wait()
        # The pending speak queue owns text only.  Do not start TTS or retain
        # PCM while the user is still speaking; endpointing grants permission
        # to start the normal streaming TTS transaction.
        await h.playback_ready.wait()
        await self._run(h)

    def release_held(self):
        """Grant playback to the latest candidate after local endpointing."""
        was_held = self._playback_hold
        self._playback_hold = False
        self._playback_release.set()
        candidates = ([self.handle] if self.handle else []) + list(self._pending)
        for h in candidates:
            if h and h.state == "SUSPENDED":
                continue
            if (h and not h.reason and not h.task.done()
                    and not h.workspace.playback_started):
                resume = getattr(self.player, "resume", None) if self.player is not None else None
                if callable(resume) and (was_held or "playback_hold_started" in h.workspace.timings):
                    resume(h.workspace.playback_key)
                hold_started = h.workspace.timings.get("playback_hold_started")
                if hold_started is not None and "playback_hold_released" not in h.workspace.timings:
                    released_at = time.monotonic()
                    h.workspace.timings.setdefault("playback_hold_released", released_at)
                    self._schedule_output_event(
                        h, "TTS_RELEASED", "tts_hold_released",
                        reason="user_speech_ended",
                        hold_ms=round((released_at - hold_started) * 1000),
                    )
                h.state = "READY"
                h.playback_ready.set()
                if h is not self.handle:
                    # Held candidates have no provider state yet; activation
                    # is immediate and does not wait for a retired task.
                    self.loop.create_task(self._activate(h), name=f"speech-activate-{h.speech_id}")
                return h.speech_id
        return None

    def suspend_pending(self):
        """Keep the latest unplayed text but remove its TTS permission."""
        candidates = ([self.handle] if self.handle else []) + list(self._pending)
        for h in candidates:
            if h and not h.reason and not h.task.done() and not h.workspace.playback_started:
                h.state = "SUSPENDED"
                h.playback_ready.clear()
                return h.speech_id
        return None

    @property
    def speak_snapshot(self):
        h = (self.handle if self.handle and not self.handle.reason and not self.handle.task.done() else None)
        h = h or next((item for item in self._pending if not item.reason and not item.task.done()), None)
        if h is None:
            return {"queue_status": "empty", "text_only_queue": True}
        if h.workspace.playback_started:
            status = "speaking"
        elif h.state == "SUSPENDED":
            status = "suspended"
        else:
            status = "queued"
        value = {"queue_status": status, "text_only_queue": True, "text": h.text,
                 "speech_id": h.speech_id, "output_generation": h.workspace.generation,
                 "conversation_id": h.identity[0], "turn_id": h.turn_id,
                 "emotion": h.emotion_requested}
        return value

    async def enqueue_text(self, identity, turn_id, text, *, emotion="calm", kind="formal"):
        h = await self.begin(identity, turn_id, emotion=emotion, kind=kind)
        await h.feed(text)
        await h.finish()
        return h

    def cancel_all(self, reason):
        """Explicitly cancel the current output scope.

        This is deliberately not a semantic invalidation hook: transcript
        revisions and tool results never call it.  Only stop/barged-in/
        replacement/transport cleanup paths may cancel output.
        """
        # A transport/lifecycle failure must not leave the permission event
        # closed after all handles have been cancelled. ``replaced`` and
        # confirmed ``interrupted`` deliberately retain the hold while the
        # user may still be speaking, so a successor remains deferred until
        # the normal speech-ended callback releases it.
        lifecycle_cancel = reason in {
            "input_unavailable", "stopped", "reset", "failed", "fatal_error", "scope_closed",
        }
        if lifecycle_cancel:
            self._playback_hold = False
            self._playback_release.set()
        items = ([self.handle] if self.handle else []) + list(self._pending)
        if lifecycle_cancel:
            # ``hold_unplayed`` may have armed a pending handle before it was
            # ever activated.  Such a handle is not the active player owner,
            # so the normal active snapshot branch below cannot clear its
            # player hold.  Resume those keys explicitly during teardown.
            resume = getattr(self.player, "resume", None) if self.player is not None else None
            if callable(resume):
                for h in items:
                    if h is not None:
                        resume(h.workspace.playback_key)
        for h in items:
            if not h or h.task.done() or h.reason:
                continue
            h.reason = reason
            h.workspace.timings.setdefault("preemption_requested", time.monotonic())
            if h is not self.handle:
                h.task.cancel()
                continue
            logger.info(
                "episode.speech_cancelled speech_id=%s episode_id=%s step=%s request_seq=%s reason=%s playback_started=%s",
                h.speech_id, h.identity[0], h.identity[1], h.workspace.generation, reason,
            h.workspace.playback_started,
            )
            if self.player:
                h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
            h.workspace.timings["local_playback_stopped"] = time.monotonic()
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(
                    lambda h=h, reason=reason: self.controller._track(asyncio.create_task(
                        self._emit_handle_progress(h, "local_playback_stopped", reason=reason)
                    ))
                )
            h.task.cancel()

    def cancel(self, speech_id, reason="cancelled"):
        """Cancel exactly one queued or active speech item.

        Cancelling by ID prevents withdrawal of an old queued candidate from
        invalidating a later response in the same FIFO queue.
        """
        if not isinstance(speech_id, str) or not speech_id:
            return False
        items = ([self.handle] if self.handle else []) + list(self._pending)
        for h in items:
            if h is None or h.speech_id != speech_id or h.task.done() or h.reason:
                continue
            h.reason = reason
            if h is self.handle:
                if self.player:
                    h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
                h.workspace.timings["local_playback_stopped"] = time.monotonic()
                if self.loop and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(
                        lambda h=h, reason=reason: self.controller._track(asyncio.create_task(
                            self._emit_handle_progress(h, "local_playback_stopped", reason=reason)
                        ))
                    )
            h.task.cancel()
            return True
        return False

    async def _run(self, h):
        # Record completion even if cancellation occurs before the first delta.
        try:
            if not self._valid(h.workspace.token):
                h.reason = h.reason or "scope_closed"
                return
            source_timings = getattr(self.controller, "_turn_timings", {}).get(h.turn_id)
            if isinstance(source_timings, dict):
                h.workspace.timings.update(source_timings)
            h.workspace.timings["llm_request"] = self.runtime.request_started_at or time.monotonic()
            await self._play_stream(h.workspace, h.workspace.token, h.stream(), speech_id=h.speech_id)
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

                await self._play_stream(h.workspace, h.workspace.token, replay(), speech_id=h.speech_id)
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
            # Preserve ownership before settlement clears ``self.handle``.
            # A same-prefix successor may be released only when this handle
            # was still the active generation at the cleanup boundary.
            was_active = self.handle is h
            # The provider generation and local PCM are now drained, but a
            # refined ASR decision often arrives immediately afterwards.
            # Retain the logical speech briefly so a verified suffix can wait
            # behind this handle instead of becoming a replacement.  This is
            # internal bookkeeping only: the listener never waits for silence
            # before hearing already available audio.
            if not h.reason and self.handle is h:
                opened_at = time.monotonic()
                h.workspace.timings["append_window_opened"] = opened_at
                await self._emit_handle_progress(h, "append_window_opened", wait_ms=1000)
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    h.reason = h.reason or "stopped"
                finally:
                    h.workspace.timings["append_window_wait_ms"] = round(
                        (time.monotonic() - opened_at) * 1000
                    )
                    await self._emit_handle_progress(h, "append_window_expired",
                                                     wait_ms=h.workspace.timings["append_window_wait_ms"])
            # Session ownership is transaction-scoped.  At this point
            # ``self._tts_session`` may already belong to a replacement.
            session = h.tts_session
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
                    h.workspace.timings["old_provider_cancelled"] = time.monotonic()
                    await self._emit_handle_progress(h, "old_provider_cancelled", reason=h.reason)
                    if self.handle is h and self._tts_session is session:
                        self._tts_session = None
            if h.snapshot is None and self.player:
                h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
            delivery = self._delivery_resolvers[h.speech_id].resolve(
                h.snapshot or {"started": False}, h.text, overlap_start_ms=h.overlap_start_ms)
            delivery["termination"] = h.reason or "completed"
            self.last_delivery = dict(delivery)
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
                "llm_request_to_first_speech_text_ms": timings.get("llm_request_to_first_speech_text_ms"),
                "first_speech_text_to_json_finish_ms": timings.get("first_speech_text_to_json_finish_ms"),
                "speech_started_before_envelope_commit": bool(timings.get("speech_started_before_envelope_commit")),
                "partial_speech_protocol_error": bool(timings.get("partial_speech_protocol_error")),
                "partial_speech_tool_recovery": bool(timings.get("partial_speech_tool_recovery")),
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
                "llm_request_to_first_speech_text_ms": timings.get("llm_request_to_first_speech_text_ms"),
                "llm_request_to_speak_queued_ms": (
                    elapsed("llm_request", "speak_queued") if "speak_queued" in timings else None
                ),
                "first_speech_text_to_json_finish_ms": timings.get("first_speech_text_to_json_finish_ms"),
                "speech_started_before_envelope_commit": bool(timings.get("speech_started_before_envelope_commit")),
                "partial_speech_protocol_error": bool(timings.get("partial_speech_protocol_error")),
                "partial_speech_tool_recovery": bool(timings.get("partial_speech_tool_recovery")),
                "turn_total_ms": since_turn("completed"),
            })
            record = {"speech_id": h.speech_id, "conversation_id": h.identity[0],
                       "request_id": h.identity[1], "trace_id": h.identity[1],
                       # Compatibility aliases are correlation-only; no
                       # semantic version participates in delivery.
                       "episode_id": h.identity[0], "agent_step_id": h.identity[1],
                       "input_id": h.turn_id,
                       "turn_id": h.turn_id, "output_generation": h.workspace.generation, "text": h.text,
                       "kind": h.kind, "queued_at": h.queued_at,
                      "emotion_requested": h.emotion_requested, "emotion_applied": h.emotion_applied,
                      "emotion_transport": getattr(self.tts, "emotion_transport", "voice_setting"),
                      "emotion_status": getattr(self.tts, "emotion_status", "applied"),
                       "status": h.reason or "completed", "delivery": delivery,
                       "turn_latency": turn_latency,
                        "latency": {**settled_latency,
                                     "queue_to_playback_ms": round((timings["playback"] - h.queued_at) * 1000)
                                     if "playback" in timings else None}}
            self.last_completed_speech = {
                "speech_id": h.speech_id,
                "conversation_id": h.identity[0],
                "turn_id": h.turn_id,
                "output_generation": h.workspace.generation,
                "text": h.text,
                "heard_text": str(delivery.get("heard_text") or ""),
                "delivery_status": delivery.get("status"),
                "termination": delivery.get("termination"),
                "status": record["status"],
            }
            logger.info(
                "episode.speech_final speech_id=%s conversation_id=%s request_id=%s turn_id=%s status=%s delivery=%s tts_text_sent=%s tts_audio_bytes=%s playback_started=%s",
                h.speech_id, h.identity[0], h.identity[1], h.turn_id,
                record["status"], delivery.get("status"), timings.get("tts_text_sent"),
                timings.get("tts_audio_bytes"), h.workspace.playback_started,
            )
            if self.handle is h:
                self.handle = None
            self._handles_by_token.pop(h.workspace.token, None)
            source_timings = getattr(self.controller, "_turn_timings", {}).get(h.turn_id)
            if isinstance(source_timings, dict):
                source_timings.update(h.workspace.timings)
            await self.runtime.output_completed(record)
            # A verified same-prefix extension is intentionally held while
            # this generation drains.  Activate it only after the old player
            # key has been settled, so its normal ``allow`` call cannot clear
            # audible PCM from the preceding response.
            if was_active and not h.reason and not self._playback_hold:
                self.release_held()

    async def close(self):
        self._playback_hold = False
        self._playback_release.set()
        self.cancel_all("stopped")
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self._tts_session:
            await self._tts_session.close()
            self._tts_session = None

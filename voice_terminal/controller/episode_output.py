from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from uuid import uuid4

from .speech_pipeline import SpeechPipeline
from .speech_frontier import SpeechFrontierMixin
from .speech_inference import SpeechInferenceBoundary
from .speech_continuation import SpeechContinuationMixin
from .turn_workspace import TurnWorkspace
from ..core.state import RuntimeState
from ..tts.alignment import DeliveryResolver
from ..quiet_hours import PassiveQuietSuppressed


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
        self.delivered_prefix = None  # delivered_prefix（接播段已结算的固定前缀；普通替换沿用动态前沿）。
        self.task = None
        self.finished = False
        self.envelope_validated = False  # envelope_validated（完整决策已通过协议校验）。
        self.run_started = False  # run_started（是否进入播放及结算路径）。
        # A replacement must never borrow the socket that is still flushing
        # the superseded transaction.  The pipeline binds the concrete
        # session to this handle, not to the output singleton.
        self.tts_session = None
        self.tts_session_prepared = False  # 会话是否在回答受理时预建，仅领取一次。
        self.tts_voice_snapshot = None  # 回答固定的音色快照。
        self.tts_inference_steps = None  # 回答固定的推理步数。
        self.tts_voice_release = None  # 整个回答持有的素材租约释放函数。
        self.tts_backend = "minimax"
        self.tts_client = None
        self.force_fresh_tts_session = False
        self.overlap_start_ms = None
        self.queued_at = time.monotonic()
        self.started = asyncio.Event()
        self.production_ready = asyncio.Event()  # production_ready（生产许可）：显式暂停关闭。
        self.production_ready.set()
        self.playback_ready = asyncio.Event()
        self.player_held = False  # player_held（该事务是否实际暂停了播放器）。
        self.state = "CREATING"
        # Bound by ``_play_stream`` for the duration of this transaction.  It
        # owns the boundary between text the provider already received and
        # text this transaction can still replace.
        self.admission = None
        self.continuation_tail = ""  # continuation_tail（发送结束后等待接播的新尾巴）。
        self.continuation_identity = None  # continuation_identity（最近一次有效修订请求）。
        self.continuation_base = ""  # continuation_base（本次窗口已实际播放的完整前缀）。
        self.continuation_deadline = None  # continuation_deadline（实际播放完成后的窗口截止时间）。
        self.continuation_tickets = set()  # continuation_tickets（已获得续写权限的推理票据）。
        self.continuation_signal = asyncio.Event()  # continuation_signal（尾巴或推理状态变化通知）。
        self.resources_released = False  # resources_released（音色与模型租约已释放）。

    @property
    def sent_chars(self):
        """Length of the irreversible prefix already handed to the provider.

        尚未进入管线的文字没有提交，不得成为锁定正文。
        """

        state = self.admission
        return state.sent_chars if state is not None else 0

    @property
    def rewritable(self):
        """Whether a later decision may still replace this tail in place."""

        state = self.admission
        return bool(state is not None and not getattr(state, "sending", False)
                    and getattr(state, "producer_done", False) and self.finished
                    and (state.rewritable or self.owner._can_continue(self)))

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
        if self.reason == "passive_quiet":
            self.text += text
            return
        if self.reason or self.task.done():
            raise ValueError("speech output closed")
        self.text += text
        await self.owner._text_delta(self, text)
        await self._put(text)

    async def finish(self):
        if self.reason == "passive_quiet":
            self.finished = True
            return
        await self._put(None)
        self.finished = True

    async def stream(self):
        while True:
            value = await self.queue.get()
            if value is None:
                return
            yield value


class EpisodeSpeechOutput(SpeechContinuationMixin, SpeechFrontierMixin, SpeechPipeline):
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
        self._settling_speech_ids: set[str] = set()
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
        # Root cause of the duplicate-playback defect: ``speak_snapshot`` used
        # to expose only the queue head, so once an appended successor became
        # active the text already delivered by its predecessor vanished from
        # the model-visible state (a 71-char frontier collapsed to 13).  Keep
        # one running "already delivered in this turn" frontier and expose it
        # as the snapshot's accepted text, so a continuation baseline can never
        # shrink behind what the listener already has.
        self.delivered = {"turn_id": None, "text": ""}
        self.inference_boundary = SpeechInferenceBoundary(self)  # inference_boundary（模型推理提交边界）。
        self._init_continuation()

    @property
    def active(self):
        return self.handle is not None and not self.handle.task.done() and self.handle.reason is None

    @property
    def busy(self):
        return bool(
            self.active
            or self._settling_speech_ids
            or any(not item.reason and not item.task.done() for item in self._pending)
        )

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
                "text_revision": getattr(h, "text_revision", 0),
                "status": self.status, "kind": h.kind, "speech_state": h.state, "emotion_requested": h.emotion_requested,
                "emotion_applied": h.emotion_applied}

    @property
    def queue_snapshot(self):
        return [{"speech_id": item.speech_id, "kind": item.kind, "text": item.text,
                 "conversation_id": item.identity[0], "trace_id": item.identity[1],
                 "turn_id": item.turn_id, "output_generation": item.workspace.generation,
                 "text_revision": getattr(item, "text_revision", 0), "status": "QUEUED"}
                for item in self._pending if not item.reason and not item.task.done()]

    def record_delivery(self, record):
        """Advance the cross-transaction delivery frontier for one settlement.

        The frontier is the authoritative answer to "what has this turn
        already delivered", accumulated over every transaction of the turn
        rather than recomputed from whichever handle happens to be the queue
        head.  It is what keeps a continuation baseline monotonic.
        """

        if not isinstance(record, dict):
            return
        turn_id = record.get("turn_id")
        delivery = record.get("delivery")
        delivery = delivery if isinstance(delivery, dict) else {}
        # 已送达只采用播放器确认的文字，提前合成状态不能替代听到的事实。
        heard = str(delivery.get("heard_text") or "")
        text = heard  # 只有实际听到的正文可进入跨事务送达前沿。
        self.last_delivery = {
            "turn_id": turn_id,
            "text": text,
            "heard_text": heard,
            "status": delivery.get("status"),
            "termination": delivery.get("termination"),
        }
        if not isinstance(turn_id, str) or not turn_id or not text:
            return
        frontier = self.delivered if isinstance(self.delivered, dict) else {}
        if frontier.get("turn_id") == turn_id:
            self.delivered = {"turn_id": turn_id, "text": str(frontier.get("text") or "") + text}
        else:
            self.delivered = {"turn_id": turn_id, "text": text}

    def _valid(self, token):
        h, r = self.handle, self.runtime
        return bool(h and h.workspace.token == token and not h.reason and r and not r.closed)

    def deferred_remainder(self, handle, full_text):
        """只扣除本事务已确认播放的前缀，尚不确定的部分仍作为未播放材料。"""
        resolver = self._delivery_resolvers.get(handle.speech_id)
        delivery = (resolver.resolve(handle.snapshot or {"started": False}, full_text,
                    overlap_start_ms=handle.overlap_start_ms) if resolver is not None else {})
        heard = str(delivery.get("heard_text") or "")
        return full_text[len(heard):], heard

    async def _wait_for_playback_permission(self, ws, token):
        """Wait while user speech owns the pre-playback output boundary."""
        wait_wake = getattr(self.controller, "_wait_for_wake_output", None)
        if callable(wait_wake):
            await wait_wake(ws.turn_id)
        h = self._handle_for_workspace(ws)
        if h is not None and getattr(h, "playback_ready", None) is not None and not h.workspace.playback_started and not h.playback_ready.is_set():
            await h.playback_ready.wait()
        if getattr(self.runtime, "should_defer_speech", lambda: False)():
            raise PassiveQuietSuppressed()
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
            held = hold(h.workspace.playback_key) if callable(hold) else False
            h.player_held = bool(held)
            if callable(hold) and not held:
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
        if h is not None and getattr(h, "tts_session_prepared", False) and h.tts_session is not None:
            h.tts_session_prepared = False
            self._tts_session = h.tts_session
            return h.tts_session
        fresh = bool(fresh or (h and h.force_fresh_tts_session))
        client = h.tts_client if h is not None else self.tts
        if client is None and not hasattr(self.tts, "bind"):
            client = self.tts
        if client is None:
            raise RuntimeError("所选 TTS provider 不可用。")
        # turn_id / speech_id（轮次与语音事务标识）随会话进入 provider，供日志串联。
        correlation = {"turn_id": ws.turn_id, "speech_id": getattr(h, "speech_id", None)}
        try:
            if h is not None and getattr(h, "tts_voice_snapshot", None) is not None:
                session = client.session(voice_snapshot=h.tts_voice_snapshot,
                                         inference_steps=h.tts_inference_steps, **correlation)
            else:
                session = client.session(fresh=fresh, emotion=h.emotion_applied if h else "calm",
                                         **correlation)
        except TypeError:  # Non-MiniMax/test providers retain the old API.
            try:
                session = client.session(fresh=fresh, turn_id=ws.turn_id)
            except TypeError:
                try:
                    session = client.session(fresh=fresh)
                except TypeError:
                    session = client.session()
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
            "provider": h.tts_backend,
            **payload,
        })

    async def _detail(self, kind, **payload):
        h = self.handle
        if h:
            await self.runtime.emit("speech.progress", {
                "speech_id": h.speech_id, "conversation_id": h.identity[0], "turn_id": h.turn_id,
                "output_generation": h.workspace.generation, "event": kind,
                "provider": h.tts_backend, **payload})

    async def _text_delta(self, h, text):
        if not text:
            return
        await self.runtime.emit("speech.text_delta", {
            "speech_id": h.speech_id, "conversation_id": h.identity[0], "turn_id": h.turn_id,
            "output_generation": h.workspace.generation, "text_delta": text,
            "text_revision": getattr(h, "text_revision", 0),
            "emotion_requested": h.emotion_requested, "emotion_applied": h.emotion_applied,
            "provider": h.tts_backend,
        })

    async def _latency(self, ws):
        return

    def playback_started(self, key):
        h = self.handle
        if h and key == h.workspace.playback_key and not h.workspace.playback_started:
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
                        # Preserve the existing latency trace even if the
                        # additional observer event cannot be delivered.
                        await self.runtime.log("TTS_STARTED", identity=h.identity, turn_id=h.turn_id,
                                               speech_id=h.speech_id)
                        await self.runtime.emit("speech.started", {
                            "speech_id": h.speech_id,
                            "conversation_id": h.identity[0],
                            "request_id": h.identity[1],
                            "trace_id": h.identity[1],
                            "turn_id": h.turn_id,
                            "output_generation": h.workspace.generation,
                            "text": h.text,
                            "status": "playing",
                            "kind": h.kind,
                            "emotion_requested": h.emotion_requested,
                            "emotion_applied": h.emotion_applied,
                            "provider": h.tts_backend,
                        })
                self.loop.call_soon_threadsafe(lambda: self.controller._track(asyncio.create_task(publish())))

    async def begin(self, identity, turn_id, *, emotion="calm", kind="formal",
                    _generation_advanced=False, _fresh_session=False, _continuation_parent=None):
        if self.runtime.gate(identity):
            raise ValueError("speech authorization expired during cleanup")
        # speak（新播报）只有空闲时可创建；显式 cancel（取消）替换由专用入口授权。
        if not _generation_advanced and self.busy:
            return None
        if _continuation_parent is None:
            self._invalidate_revision_window()
        elif not self._continuation_current(_continuation_parent):
            return None
        on_model_speech = getattr(self.controller, "_on_model_speech_accepted", None)
        if callable(on_model_speech) and not getattr(self.runtime, "should_defer_speech", lambda: False)():
            await on_model_speech()
        # 异步准备后再次确认；检查与注册之间没有让出事件循环。
        if self.runtime.gate(identity):
            raise ValueError("speech authorization expired during preparation")
        if _continuation_parent is not None and not self._continuation_current(_continuation_parent):
            return None
        if not _generation_advanced and self.busy:
            return None
        if not _generation_advanced:
            self.generation += 1
        self.loop = asyncio.get_running_loop()
        if _continuation_parent is not None:
            identity = _continuation_parent.continuation_identity or identity
        h = SpeechHandle(self, identity, turn_id, self.generation, emotion, kind=kind)
        if _continuation_parent is not None:
            # 接播严格晚于旧段结算；普通替换的旧段可能还在异步结算，不能提前冻结。
            h.delivered_prefix = _continuation_parent.continuation_base
        h.envelope_validated = getattr(self.runtime, "_validated_envelope_identity", None) == identity
        if hasattr(self.tts, "bind"):
            if _continuation_parent is None:
                h.tts_backend, h.tts_client = self.tts.bind()
            else:
                h.tts_backend, h.tts_client = self.tts.bind(backend=_continuation_parent.tts_backend)
        else:
            h.tts_backend, h.tts_client = "minimax", self.tts
        if _continuation_parent is not None:
            # 后继已注册才移交窗口；异步准备期间保留旧锚，取消仍能撤销接播。
            self._continuation_handle = None
        h.force_fresh_tts_session = _fresh_session
        self._handles_by_token[h.workspace.token] = h
        register = getattr(self.runtime, "_register_speech_transaction", None)
        if callable(register):
            register(h)
        if (getattr(self.runtime, "user_speaking", False) is False
                and not self._playback_hold):
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
            if not self._continuation_alive(handle):
                self._release_speech_resources(handle)
            if not handle.run_started and handle.tts_session is not None:
                # 未进入执行管线的会话也必须释放音色素材租约。
                cleanup = asyncio.create_task(handle.tts_session.close(), name=f"speech-unused-{handle.speech_id}")
                self.tasks.add(cleanup)
                cleanup.add_done_callback(self.tasks.discard)
            # A held/superseded task can be cancelled before it ever enters
            # _run() (and therefore before _run's final cleanup executes).
            if not handle.run_started:
                if self.handle is handle:
                    self.handle = None
                try:
                    self._pending.remove(handle)
                except ValueError:
                    pass
            if self.handle is not handle:
                self._handles_by_token.pop(handle.workspace.token, None)
        h.task.add_done_callback(forget_finished)
        # Register the candidate before awaiting emotion preparation.  User
        # speech can arrive during that await; keeping the handle visible lets
        # ``hold_unplayed`` mark it HELD instead of only setting the global
        # gate and leaving the handle READY.
        self.inference_boundary.authorize_new(h)
        if getattr(h.tts_client, "resolve_voice", None) is not None:
            try:
                if _continuation_parent is not None and _continuation_parent.tts_voice_snapshot is not None:
                    h.tts_session = h.tts_client.session(voice_snapshot=_continuation_parent.tts_voice_snapshot,
                                                        inference_steps=_continuation_parent.tts_inference_steps)
                else:
                    h.tts_session = h.tts_client.session()
                h.tts_session_prepared = True
                h.tts_voice_snapshot = getattr(h.tts_session, "voice_snapshot", None)
                h.tts_inference_steps = getattr(h.tts_session, "_inference_steps", None)
                library = getattr(h.tts_client, "voice_library", None)
                if h.tts_voice_snapshot is not None and callable(getattr(library, "pin", None)):
                    h.tts_voice_release = library.pin(h.tts_voice_snapshot)
            except Exception:
                h.reason = "failed"
                h.task.cancel()  # 冻结音色失败时同样释放路由租约与待启动任务。
                raise
        self._pending.append(h)
        try:
            await self._activate(h)  # 先合成，设备输出仍等待播放许可。
            await self.runtime.emit("speech.queued", {"speech_id": h.speech_id, "conversation_id": h.identity[0],
                                     "turn_id": turn_id, "output_generation": h.workspace.generation,
                                     "kind": kind, "text": h.text, "status": h.state,
                                     "provider": h.tts_backend})
            await asyncio.sleep(0)
        except BaseException:
            # 音色准备或事件发布时取消，已登记的后继也必须撤销，不能留下空事务。
            self.cancel(h.speech_id)
            raise
        return h

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
                "provider": handle.tts_backend,
                "old_provider": old_candidate.tts_backend,
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
        prepare = getattr(h.tts_client, "prepare_emotion", None)
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
        # production_ready（生产许可）独立于端点控制的播放许可。
        await h.production_ready.wait()
        await self._run(h)

    def release_held(self):
        """Grant playback to the latest candidate after local endpointing."""
        self._playback_hold = False
        self._playback_release.set()
        candidates = ([self.handle] if self.handle else []) + list(self._pending)
        for h in candidates:
            if h and h.state == "SUSPENDED":
                continue
            if (h and not h.reason and not h.task.done()
                    and not h.workspace.playback_started):
                resume = getattr(self.player, "resume", None) if self.player is not None else None
                if callable(resume) and getattr(h, "player_held", False):
                    if resume(h.workspace.playback_key) is False:
                        # playback_resume_failed（播放器恢复失败）：不能发布成功释放，
                        # 也不能重新授权已失效的代次，让该事务按取消路径结算。
                        h.player_held = False
                        self._schedule_output_event(
                            h, "TTS_RESUME_FAILED", "tts_resume_failed",
                            reason="playback_resume_failed",
                        )
                        self.cancel(h.speech_id, "playback_resume_failed")
                        return None
                    h.player_held = False
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
        """暂停新正文提交；已锁定音频可结束，尚未起播时关闭设备许可。"""
        candidates = ([self.handle] if self.handle else []) + list(self._pending)
        for h in candidates:
            if h and not h.reason and not h.task.done():
                h.state = "SUSPENDED"
                h.production_ready.clear()
                if not h.workspace.playback_started:
                    h.playback_ready.clear()
                    hold = getattr(self.player, "hold", None) if self.player is not None else None
                    if callable(hold):
                        h.player_held = hold(h.workspace.playback_key) is not False
                return h.speech_id
        return None

    def resume_pending(self):
        """resume_pending（恢复生产）：显式暂停恢复后仍遵守端点播放许可。"""
        for h in ([self.handle] if self.handle else []) + list(self._pending):
            if h and not h.reason and h.state == "SUSPENDED":
                h.state = "READY" if h.workspace.playback_started else "HELD"
                h.production_ready.set()
                if not self._playback_hold:
                    self.release_held()
                return h.speech_id
        return None

    async def enqueue_text(self, identity, turn_id, text, *, emotion="calm", kind="formal"):
        h = await self.begin(identity, turn_id, emotion=emotion, kind=kind)
        if h is None:
            return None
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
        self._invalidate_revision_window()
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
            h.continuation_tail = ""
            h.continuation_tickets.clear()
            h.continuation_signal.set()
            if reason == "passive_quiet" and h.finished and h.envelope_validated and not h.run_started:
                self.runtime.defer_speech(h.speech_id, h.text)
            h.reason = reason
            h.production_ready.set()
            h.playback_ready.set()
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
        window = self._continuation_handle
        if window is not None and window.speech_id == speech_id:
            self._invalidate_revision_window()
            if window.task.done():
                return True
        items = ([self.handle] if self.handle else []) + list(self._pending)
        for h in items:
            if h is None or h.speech_id != speech_id or h.task.done() or h.reason:
                continue
            h.continuation_tail = ""
            h.continuation_tickets.clear()
            h.continuation_signal.set()
            h.reason = reason
            h.production_ready.set()
            h.playback_ready.set()
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

    @staticmethod
    def _playback_completed_fact(h, snapshot):
        """窗口、调度通知和最终送达共用同一设备输出完成事实。"""
        read = snapshot.get if isinstance(snapshot, dict) else lambda key: getattr(snapshot, key, None)
        return bool(
            not h.reason
            and h.text
            and h.workspace.timings.get("tts_text_sent")
            and int(h.workspace.timings.get("tts_audio_bytes") or 0) > 0
            and h.workspace.timings.get("playback_output_complete") is True
            and bool(read("started"))
            and int(read("buffered_ms") or 0) == 0
            and int(read("enqueued_ms") or 0) > 0
            and int(read("output_estimated_ms") or 0) >= int(read("enqueued_ms") or 0)
        )

    async def _notify_playback_completed(self, h):
        """在播放器输出完成时通知 Agent（代理），先于历史结算。"""

        if h.reason or h.workspace.timings.get("playback_completion_notified"):
            return
        snapshot = h.snapshot
        progress = getattr(self.player, "progress", None)
        if snapshot is None and callable(progress):
            snapshot = progress(h.workspace.playback_key)
        if not self._playback_completed_fact(h, snapshot):
            return
        timings = h.workspace.timings
        completed_at = timings.get("playback_output_completed_at")
        if not isinstance(completed_at, (int, float)) or completed_at <= 0:
            completed_at = time.monotonic()
            timings["playback_output_completed_at"] = completed_at
        timings["playback_completion_notified"] = completed_at
        self._open_revision_window(h, completed_at)
        callback = getattr(self.runtime, "playback_completed", None)
        if not callable(callback):
            return
        try:
            result = callback(
                turn_id=h.turn_id,
                speech_id=h.speech_id,
                conversation_id=h.identity[0],
                request_id=h.identity[1],
                output_generation=h.workspace.generation,
                playback_completed_at=completed_at,
                wall_time=time.time(),
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            # The scheduling diagnostic must never turn a successfully played
            # response into a failed speech transaction.
            logger.exception(
                "episode.playback_completion_callback_failed speech_id=%s turn_id=%s",
                h.speech_id, h.turn_id,
            )

    async def _run(self, h):
        h.run_started = True
        # Record completion even if cancellation occurs before the first delta.
        try:
            if not self._valid(h.workspace.token):
                h.reason = h.reason or "scope_closed"
                return
            source_timings = getattr(self.controller, "_turn_timings", {}).get(h.turn_id)
            if isinstance(source_timings, dict):
                h.workspace.timings.update(source_timings)
            h.workspace.timings["llm_request"] = self.runtime.request_started_at or time.monotonic()
            await self._play_stream(h.workspace, h.workspace.token, h.stream(),
                                    speech_id=h.speech_id, handle=h)
            await self._notify_playback_completed(h)
            # A zero-PCM Bidi transaction cannot have delivered any text, so
            # one replay is safe and avoids making the user repeat a request
            # after a cancelled/non-reusable MiniMax session.  Never retry
            # once any PCM was returned: that could duplicate audible speech.
            if (
                h.tts_backend == "minimax"
                and h.text
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

                await self._play_stream(h.workspace, h.workspace.token, replay(),
                                        speech_id=h.speech_id, handle=h)
                await self._notify_playback_completed(h)
                if h.workspace.timings.get("tts_text_sent") and h.workspace.timings.get("tts_audio_bytes", 0) == 0:
                    await self.runtime.log(
                        "TTS_EMPTY_AUDIO_RETRY_FAILED",
                        identity=h.identity,
                        turn_id=h.turn_id,
                        speech_id=h.speech_id,
                        attempt=2,
                    )
            if getattr(h.tts_client, "emotion_status", None) == "unsupported":
                h.emotion_applied = getattr(h.tts_client, "applied_emotion", "calm")
                await self.runtime.log("TTS_EMOTION_UNSUPPORTED", identity=h.identity, turn_id=h.turn_id,
                                       speech_id=h.speech_id,
                                       error=getattr(h.tts_client, "emotion_error", None))
            provider_missing = h.tts_client is None and (self.tts is None or hasattr(self.tts, "bind"))
            if self.state.current.get("tts_status") == "DEGRADED" or provider_missing or self.player is None:
                h.reason = "failed"
            # PCM（原始音频采样）队列及设备尾部已经输出完毕。
            # 在交接前冻结播放快照，防止下一代输出覆盖上一句的送达事实。
            if not h.reason and h.snapshot is None and self.player is not None:
                h.snapshot = self.player.stop_and_snapshot(h.workspace.playback_key)
                h.workspace.timings["playback_snapshot_frozen"] = time.monotonic()
        except asyncio.CancelledError:
            h.reason = h.reason or "stopped"
        except PassiveQuietSuppressed:
            h.reason = "passive_quiet"
        except Exception as exc:
            h.reason = "failed"
            await self.runtime.log("SPEECH_ERROR", identity=h.identity, turn_id=h.turn_id,
                                   speech_id=h.speech_id, error=str(exc)[:300])
        finally:
            # Preserve ownership before settlement clears ``self.handle``.
            # A same-prefix successor may be released only when this handle
            # was still the active generation at the cleanup boundary.
            was_active = self.handle is h
            # 播放完成后立即结算，已排队的续句无需等待固定宽限期。
            # delivered（已送达文字边界）与 last_completed_speech（最近完成语音）
            # 会在释放旧句柄前保存，迟到的续句仍可剥离已播放前缀。
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
            if h.snapshot:
                self.controller._add_playback_delivery_fields(delivery, h.snapshot)
            snapshot = h.snapshot
            playback_complete = self._playback_completed_fact(h, snapshot)
            if playback_complete:
                provider_status = str(delivery.get("status") or "none")
                delivery.update({
                    "status": "full",
                    "termination": "completed",
                    "heard_text": h.text,
                    "uncertain_text": "",
                    "unheard_text": "",
                    "precision": "playback_complete",
                    "reason": "completed_playback_authoritative",
                })
                if provider_status != "full":
                    diagnostic = self._delivery_resolvers[h.speech_id].diagnostics
                    logger.info(
                        "delivery.reconciled speech_id=%s provider_status=%s final_status=full "
                        "reason=completed_playback_authoritative played_ms=%s enqueued_ms=%s "
                        "provider_span_count=%s provider_min_ms=%s provider_max_ms=%s",
                        h.speech_id, provider_status,
                        getattr(snapshot, "output_estimated_ms", None),
                        getattr(snapshot, "enqueued_ms", None),
                        diagnostic["provider_span_count"], diagnostic["provider_min_ms"],
                        diagnostic["provider_max_ms"],
                    )
                    await self.runtime.log(
                        "DELIVERY_RECONCILED", identity=h.identity, turn_id=h.turn_id,
                        speech_id=h.speech_id, provider_status=provider_status,
                        final_status="full", reason="completed_playback_authoritative",
                        played_ms=getattr(snapshot, "output_estimated_ms", None),
                        enqueued_ms=getattr(snapshot, "enqueued_ms", None), **diagnostic,
                    )
            self.last_delivery = dict(delivery)
            if h.reason == "passive_quiet" and h.finished and h.envelope_validated:
                heard = str(delivery.get("heard_text") or "")
                self.runtime.defer_speech(h.speech_id, h.text[len(heard):], heard_text=heard)
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
                "wake_to_cue_first_pcm_ms": elapsed("wake_detected", "wake_cue_first_pcm"),  # 命中到提示音首帧。
                "wake_to_llm_request_ms": elapsed("wake_detected", "llm_request"),  # 命中到模型请求。
                "wake_to_asr_ready_ms": elapsed("wake_detected", "wake_asr_ready"),  # 命中到识别服务就绪。
                "wake_to_buffer_drained_ms": elapsed("wake_detected", "wake_buffer_drained"),  # 命中到缓存回放完成。
                "wake_to_tts_prewarm_ms": elapsed("wake_detected", "tts_prewarm_requested"),  # 命中到合成预热请求。
                "wake_to_confirmation_first_pcm_ms": elapsed("wake_detected", "first_pcm_submitted"),  # 命中到确认语首帧。
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
                       "envelope_validated": h.envelope_validated,
                      "emotion_requested": h.emotion_requested, "emotion_applied": h.emotion_applied,
                      "emotion_transport": getattr(h.tts_client, "emotion_transport", "voice_setting"),
                      "emotion_status": getattr(h.tts_client, "emotion_status", "applied"),
                      "provider": h.tts_backend,
                       "status": h.reason or "completed", "delivery": delivery,
                       # How much of ``text`` the provider actually received.
                       # A replacement or a barge-in can stop a transaction
                       # before the whole answer was synthesised, and the
                       # delivery frontier must never claim that audio.
                       "tts_text_chars": int(getattr(h, "sent_chars", len(h.text)) or 0),
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
            self.record_delivery(record)
            logger.info(
                "episode.speech_final speech_id=%s conversation_id=%s request_id=%s turn_id=%s "
                "status=%s delivery=%s tts_text_sent=%s tts_audio_bytes=%s playback_started=%s "
                "text_chars=%s heard_chars=%s uncertain_chars=%s unheard_chars=%s termination=%s precision=%s",
                h.speech_id, h.identity[0], h.identity[1], h.turn_id,
                record["status"], delivery.get("status"), timings.get("tts_text_sent"),
                timings.get("tts_audio_bytes"), h.workspace.playback_started,
                len(h.text), len(str(delivery.get("heard_text") or "")),
                len(str(delivery.get("uncertain_text") or "")), len(str(delivery.get("unheard_text") or "")),
                delivery.get("termination"), delivery.get("precision"),
            )
            self._settling_speech_ids.add(h.speech_id)
            logger.info(
                "speech.settling_started speech_id=%s turn_id=%s",
                h.speech_id, h.turn_id,
            )
            await self.runtime.log(
                "SPEECH_SETTLING_STARTED", identity=h.identity, turn_id=h.turn_id,
                speech_id=h.speech_id,
            )
            if self.handle is h:
                self.handle = None
            self._handles_by_token.pop(h.workspace.token, None)
            source_timings = getattr(self.controller, "_turn_timings", {}).get(h.turn_id)
            if isinstance(source_timings, dict):
                source_timings.update(h.workspace.timings)
            try:
                await self.runtime.output_completed(record)
            finally:
                self._settling_speech_ids.discard(h.speech_id)
                settled = getattr(self.runtime, "output_transaction_settled", None)
                if callable(settled):
                    try:
                        result = settled(record)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception(
                            "episode.output_settlement_callback_failed speech_id=%s turn_id=%s",
                            h.speech_id, h.turn_id,
                        )
                logger.info(
                    "speech.settling_finished speech_id=%s turn_id=%s",
                    h.speech_id, h.turn_id,
                )
                await self.runtime.log(
                    "SPEECH_SETTLING_FINISHED", identity=h.identity, turn_id=h.turn_id,
                    speech_id=h.speech_id,
                )
            # A verified same-prefix extension is intentionally held while
            # this generation drains.  Activate it only after the old player
            # key has been settled, so its normal ``allow`` call cannot clear
            # audible PCM from the preceding response.
            if was_active and not h.reason and not self._playback_hold:
                self.release_held()
            self._schedule_continuation(h)

    async def close(self):
        self._playback_hold = False
        self._playback_release.set()
        self.cancel_all("stopped")
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self._tts_session:
            await self._tts_session.close()
            self._tts_session = None

"""SpeechInferenceBoundary（推理语音边界）：请求前补块，推理期间冻结提交。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import logging

logger = logging.getLogger(__name__)
INFERENCE_BUFFER_MS = 3000  # INFERENCE_BUFFER_MS（有效请求前的剩余音频目标）。
PREFILL_TIMEOUT_SECONDS = 2.0  # PREFILL_TIMEOUT_SECONDS（补块调度最长等待时间）。


class SpeechInferenceBoundary:
    """关闸并结算已有提交；仅请求准备阶段允许旧事务补交一块文本。

    发送与字符结算使用同一互斥锁。推理期间只阻止新的文本提交，音频
    接收和播放继续进行。结果落地、失败和取消都仅释放本次请求票据。
    """

    def __init__(self, output, *, buffer_ms=INFERENCE_BUFFER_MS,
                 prefill_timeout=PREFILL_TIMEOUT_SECONDS):
        self.output = output  # output（唯一语音事务所有者）。
        self.buffer_ms = buffer_ms  # buffer_ms（请求前剩余音频目标，毫秒）。
        self.prefill_timeout = prefill_timeout  # prefill_timeout（补块等待上限，秒）。
        self._new_speech = {}  # 请求票据绑定明确受理的新播报标识。
        self._requests = set()  # _requests（尚未释放的独立请求票据）。
        self._mutex = asyncio.Lock()  # _mutex（发送及字符结算互斥锁）。
        self._changed = asyncio.Event()  # _changed（闸门状态变化通知）。
        self._prefill = None  # _prefill（票据、获准补块事务、完成通知）。
        self._ticket = ContextVar("speech_inference_ticket", default=None)  # _ticket（当前请求独立票据）。

    @property
    def current_ticket(self):
        """current_ticket（当前异步请求票据）：不同请求不能借用对方的续写资格。"""
        return self._ticket.get()

    @property
    def frozen(self):
        """frozen（是否有请求正在保护提交边界）。"""
        return bool(self._requests)

    def prefilling(self, handle):
        """prefilling（该事务是否获准在请求前补交一块）。"""
        return (self._prefill is not None and self._prefill[1] is handle
                and len(self._requests) == 1)

    def authorize_new(self, handle):
        """仅本次请求新建的播报拥有生产例外，旧事务保持冻结。"""
        for ticket, (identity, ids) in self._new_speech.items():
            if identity == handle.identity:
                ids.add(handle.speech_id)
        self._changed.set()

    def _allowed(self, handle):
        own = bool(handle and self._requests and all(
            handle.speech_id in self._new_speech.get(ticket, (None, set()))[1]
            for ticket in self._requests))
        return not self.frozen or self.prefilling(handle) or own

    async def wait_ready(self, handle, valid):
        """wait_ready（等待提交许可）：取消事务无需等模型完成。"""
        while valid():
            if self._allowed(handle):
                return True
            self._changed.clear()
            if self._allowed(handle):
                continue
            try:
                await asyncio.wait_for(self._changed.wait(), 0.1)
            except asyncio.TimeoutError:
                pass
        return False

    @asynccontextmanager
    async def submission(self, handle, valid):
        """submission（发送临界区）：发送成功与字符推进不可被快照拆开。"""
        while await self.wait_ready(handle, valid):
            await self._mutex.acquire()
            try:
                if self.prefilling(handle) and self._remaining_ms(handle) >= self.buffer_ms:
                    # 上一块音频在准备等待期间已返回，缓冲够用就撤销额外补锁。
                    self._finish_prefill(False)
                if not self._allowed(handle):
                    continue
                yield valid()
                return
            finally:
                self._mutex.release()
        yield False

    def committed(self, handle):
        """committed（补块已接受）：立即撤销单次补块许可。"""
        if self.prefilling(handle):
            self._finish_prefill(True)

    def _finish_prefill(self, submitted):
        """_finish_prefill（结算准备许可）：补交成功或音频已足够。"""
        _, _, completed = self._prefill
        self._prefill = None
        if not completed.done():
            completed.set_result(submitted)
        self._changed.set()

    def _remaining_ms(self, handle):
        if handle is None or self.output.player is None:
            return 0
        player = self.output.player
        received = int(getattr(handle.tts_session, "received_bytes", 0) or 0)
        bytes_per_second = player.sample_rate * player.channels * 2
        progress = player.progress(handle.workspace.playback_key)
        output_ms = int(progress.get("output_estimated_ms", progress.get("played_ms", 0)) or 0)
        returned_ms = int(received * 1000 / bytes_per_second) if bytes_per_second else 0
        return max(0, returned_ms - output_ms)

    def _can_prefill(self, handle):
        """仅可改的旧事务可补块；不唤醒被用户讲话挂起的输出。"""
        if (handle is None or handle.reason or handle.task.done()
                or handle.tts_session is None or not handle.finished
                or getattr(self.output.runtime, "user_speaking", False)
                or getattr(self.output, "playback_hold_active", False)):
            return False
        state = handle.admission
        if (state is None or not state.rewritable or state.sent_chars >= len(handle.text)
                or not handle.production_ready.is_set()):
            return False
        if state.sentence_window and self.output.player is not None:
            player = self.output.player
            progress = player.progress(handle.workspace.playback_key)
            active = state.active_clauses(
                output_estimated_ms=int(progress.get("output_estimated_ms", progress.get("played_ms", 0)) or 0),
                bytes_per_second=player.sample_rate * player.channels * 2,
            )
            # 保留 current_clause（当前块）+ prefetched_clause（预取块）的契约。
            if len(active) >= 2:
                return False
        return True

    @asynccontextmanager
    async def request(self, identity):
        """request（有效推理保护期）：任何退出路径均配对释放。"""
        ticket = object()  # ticket（本次请求专用票据，重试也不复用）。
        completed = None  # completed（补块已被语音服务接受的通知）。
        before_ms = 0  # before_ms（准备开始时的剩余音频）。
        prefill_status = "not_needed"  # prefill_status（补块结果）。
        retained = None  # retained（本次推理获得的续写窗口锚）。
        ticket_context = self._ticket.set(ticket)  # ticket_context（进入请求前的上下文令牌）。
        self._requests.add(ticket)
        self._new_speech[ticket] = (identity, set())
        self._changed.set()
        try:
            # 先关闸，再等待已经开始的 send_text（文本发送）结算。
            async with self._mutex:
                handle = self.output.handle
                before_ms = self._remaining_ms(handle)
                if before_ms < self.buffer_ms:
                    prefill_status = "unavailable"
                    if len(self._requests) == 1 and self._can_prefill(handle):
                        completed = asyncio.get_running_loop().create_future()
                        self._prefill = (ticket, handle, completed)
                        prefill_status = "pending"
                        self._changed.set()
            if completed is not None:
                try:
                    submitted = await asyncio.wait_for(asyncio.shield(completed), self.prefill_timeout)
                    prefill_status = "submitted" if submitted else "buffer_ready"
                except asyncio.TimeoutError:
                    prefill_status = "timeout"
                finally:
                    if self._prefill is not None and self._prefill[0] is ticket:
                        self._prefill = None
                    self._changed.set()
            # 超时也必须等已在发送的补块结算，才能构建真实且稳定的模型上下文。
            async with self._mutex:
                handle = self.output.handle
                sent_chars = handle.sent_chars if handle is not None else 0
                speech_id = handle.speech_id if handle is not None else None
                retain = getattr(self.output, "_retain_revision_window", None)
                if callable(retain):
                    retained = retain(ticket)
            logger.info(
                "speech.inference_frozen conversation_id=%s request_id=%s speech_id=%s "
                "sent_chars=%s remaining_ms=%d target_ms=%d prefill=%s",
                identity[0], identity[1], speech_id, sent_chars, before_ms,
                self.buffer_ms, prefill_status,
            )
            yield
        finally:
            release = getattr(self.output, "_release_revision_window", None)
            if callable(release):
                release(ticket, retained)
            if self._prefill is not None and self._prefill[0] is ticket:
                self._prefill = None
            if completed is not None and not completed.done():
                completed.cancel()
            self._requests.discard(ticket)
            self._new_speech.pop(ticket, None)
            self._changed.set()
            self._ticket.reset(ticket_context)
            logger.info("speech.inference_released conversation_id=%s request_id=%s held=%s",
                        identity[0], identity[1], self.frozen)

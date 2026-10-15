"""SpeechContinuationMixin（回应续写窗口）：保留修订资格，有序接播新增尾巴。"""
from __future__ import annotations

import asyncio
import logging
import time


logger = logging.getLogger(__name__)


class SpeechContinuationMixin:
    continuation_window_seconds = 1.0  # continuation_window_seconds（实际播放完成后的续写秒数）。

    def _init_continuation(self):
        self._continuation_handle = None  # _continuation_handle（已播放回应的续写锚）。
        self._continuation_task = None  # _continuation_task（窗口收尾及后续接播任务）。

    @property
    def continuation_active(self):
        """continuation_active（续写仍持有旧后端）：整体替换后端前须等待退出。"""
        h = self._continuation_handle
        return h is not None and self._continuation_alive(h)

    def _continuation_current(self, h):
        if h is None or h.reason or h.workspace.generation != self.generation:
            return False
        runtime = self.runtime
        if getattr(runtime, "closed", False) or runtime.gate(h.identity):
            return False
        return getattr(runtime, "last_turn_id", h.turn_id) in {None, h.turn_id}

    def _continuation_alive(self, h):
        """已受理工作或在途权限仍有待结算时保留锚；实际播放结束才开始计时。"""
        if not self._continuation_current(h):
            return False
        if h is not self.handle and h is not self._continuation_handle:
            return False
        if h.task.done() and h is not self._continuation_handle:
            return False
        deadline = h.continuation_deadline
        # 已受理尾巴属于待播工作，不能因慢结算或推理票据释放而过期丢失。
        return (deadline is None or time.monotonic() < deadline
                or bool(h.continuation_tickets) or bool(h.continuation_tail))

    def _can_continue(self, h):
        """当前请求能否修订：截止后只保留该请求在窗口内已获得的权限。"""
        if not self._continuation_alive(h):
            return False
        deadline = h.continuation_deadline
        return (deadline is None or time.monotonic() < deadline
                or self.inference_boundary.current_ticket in h.continuation_tickets)

    def _retain_revision_window(self, ticket):
        """ticket（推理票据）：窗口内获得的修订权限保留到本次请求退出。"""
        h = self.handle or self._continuation_handle
        if h is None or not h.rewritable or not self._can_continue(h):
            return None
        h.continuation_tickets.add(ticket)
        return h

    def _release_revision_window(self, ticket, h):
        if h is not None:
            h.continuation_tickets.discard(ticket)
            h.continuation_signal.set()

    def _open_revision_window(self, h, completed_at):
        if not self._continuation_current(h):
            return
        previous = self._continuation_handle
        if previous is not None and previous is not h:
            self._invalidate_revision_window()
        h.continuation_base = self._accumulated_text(h)[0]
        h.continuation_deadline = completed_at + self.continuation_window_seconds
        self._continuation_handle = h

    def _continuation_snapshot(self):
        h = getattr(self, "_continuation_handle", None)
        if h is None or not self._continuation_alive(h):
            return None
        base, tail = h.continuation_base, h.continuation_tail
        value = {
            "queue_status": "continuation",  # queue_status（播放结束后仍可续写）。
            "text_only_queue": True, "text": base + tail,  # text（已播放前缀与待接播尾巴）。
            "conversation_id": h.identity[0], "turn_id": h.turn_id,
            "speech_id": h.speech_id, "output_generation": h.workspace.generation,
            "text_revision": getattr(h, "text_revision", 0),
            "sent_chars": len(base), "rewritable": self._can_continue(h),
            "_model_text_parts": {
                "committed_prefix": base, "current_clause": "", "prefetched_clause": "",
                "revisable_tail": tail,
            },
            "_model_text_parts_valid": True,
        }
        delivery = self._delivery_fact()
        if delivery is not None:
            value["delivery"] = {**delivery, "speech_id": h.speech_id}
        return value

    def _release_speech_resources(self, h):
        """物理播放和续写窗口均退出后，释放音色及模型租约，且只释放一次。"""
        if h.resources_released:
            return
        h.resources_released = True
        if h.tts_voice_release is not None:
            h.tts_voice_release()
            h.tts_voice_release = None
        if hasattr(self.tts, "release"):
            self.tts.release(h.tts_backend)

    def _invalidate_revision_window(self):
        h, self._continuation_handle = self._continuation_handle, None
        task, self._continuation_task = self._continuation_task, None
        if h is not None:
            h.continuation_tail = ""
            h.continuation_deadline = 0.0
            h.continuation_tickets.clear()
            h.continuation_signal.set()
            if h.task.done():
                self._release_speech_resources(h)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _schedule_continuation(self, h):
        if self._continuation_handle is not h:
            return
        if self._continuation_task is None or self._continuation_task.done():
            task = asyncio.create_task(self._wait_for_continuation(h), name=f"speech-continuation-{h.speech_id}")
            self._continuation_task = task
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _wait_for_continuation(self, h):
        """当前音频及历史结算结束后立即接播；空窗口到期才收尾，不固定延迟播放。"""
        try:
            if not h.task.done():
                await asyncio.shield(h.task)
            while self._continuation_handle is h and self._continuation_alive(h):
                h.continuation_signal.clear()
                if h.continuation_tail:
                    identity = h.continuation_identity or h.identity
                    successor = await self.begin(identity, h.turn_id, emotion=h.emotion_requested,
                                                 kind=h.kind, _continuation_parent=h)
                    if successor is not None:
                        # 创建前的异步准备允许再次修订，接播必须消费最终版本。
                        text, h.continuation_tail = h.continuation_tail, ""
                        await successor.feed(text)
                        await successor.finish()
                    return
                remaining = h.continuation_deadline - time.monotonic()
                if remaining <= 0:
                    if not h.continuation_tickets:
                        break
                    await h.continuation_signal.wait()
                else:
                    try:
                        await asyncio.wait_for(h.continuation_signal.wait(), remaining)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("speech.continuation_failed speech_id=%s", h.speech_id)
        finally:
            if self._continuation_handle is h:
                self._continuation_handle = None
            if self._continuation_task is asyncio.current_task():
                self._continuation_task = None
            self._release_speech_resources(h)

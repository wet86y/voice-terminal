"""ScreenInputMixin（屏幕输入调度）：复用唯一心跳、请求恢复及聊天顺序。"""
from __future__ import annotations

import asyncio


class ScreenInputMixin:
    def invalidate_screen_capture(self):
        service = getattr(self.ledger, "screen_capture", None)
        if service is not None:
            service.invalidate()
        packet = self._screen_pending
        if packet is not None:
            self.ledger.update_screen_status(packet, "未发送")
        self._screen_pending = None
        active = getattr(self, "active_request", None) is not None
        for packet in self._screen_requests.values():
            if not active or packet is not self._screen_context_packet:
                self.ledger.update_screen_status(packet, "未发送")
        # 已冻结正文继续由请求收尾；清除派发槽不删除稳定历史中的图片。
        self._screen_requests.clear()
        if not active:
            self._screen_context_packet = None
        self.pending.discard("SCREEN_CAPTURE_READY")
        self.pending.discard("SCREEN_CAPTURE_FAILED")

    async def _capture_screen_action(self, identity, turn_id):
        service = getattr(self.ledger, "screen_capture", None)
        if service is None or not service.available or self.input_scope_closed or self.closed:
            return
        generation = service.generation
        try:
            packet = await service.grab()
            if self.closed or self.input_scope_closed or generation != service.generation or identity[0] != self.episode_id:
                return
            if self._screen_pending is not None:
                self.ledger.update_screen_status(self._screen_pending, "已被新截图替代")
            packet["activation_generation"] = generation  # activation_generation（捕获作用域代次）。
            self._screen_pending = self.ledger.store_screen_capture(packet, identity[0], turn_id or "")
            self._wake("SCREEN_CAPTURE_READY")
        except (Exception, asyncio.TimeoutError) as exc:
            if self.closed or self.input_scope_closed or generation != service.generation:
                return
            self._screen_error = "屏幕捕获失败：" + ("超过3秒" if isinstance(exc, asyncio.TimeoutError) else str(exc)[:200])
            await self.emit("error", {"code": "SCREEN_CAPTURE_FAILED", "message": self._screen_error, "recoverable": True})
            self._wake("SCREEN_CAPTURE_FAILED")

    def _prepare_screen_context(self, retry_key, kinds, recovery):
        if not self.ledger.screen_capture.config.enabled or self.input_scope_closed:
            self._screen_requests.clear()
            self._screen_context_packet = None
            return
        # 请求槽只追踪本次派发状态；既有图片始终由台账历史按原位置投影。
        self._screen_context_packet = self._screen_requests.get(retry_key) if recovery or "LLM_RETRY" in kinds else None
        if self._screen_context_packet is None and self._screen_pending is not None:
            service = self.ledger.screen_capture
            if service.config.enabled and not self.input_scope_closed:
                self._screen_context_packet = self._screen_pending
        if self._screen_context_packet is not None:
            # 冻结时原子提交到有序历史，后到文本不会越过这条观察消息。
            self.ledger.commit_screen_capture(self._screen_context_packet)

    def _claim_screen_context(self, retry_key):
        packet = self._screen_context_packet
        if packet is not None:
            self._screen_requests[retry_key] = packet
            if self._screen_pending is packet:
                self._screen_pending = None
                self.pending.discard("SCREEN_CAPTURE_READY")

    def _screen_accepted(self):
        packet = self._screen_context_packet
        if packet is not None:
            self.ledger.update_screen_status(packet, "已发送")

    def _settle_screen_context(self, retry_key, outcome):
        packet = self._screen_requests.pop(retry_key, None)
        accepted = packet is not None and any(row.get("speech_id") == packet["capture_id"] and row.get("status") == "已发送"
                                               for row in self.ledger.conversation.entries)
        if packet is not None and outcome != "completed" and not accepted:
            self.ledger.update_screen_status(packet, "发送失败")
        self._screen_context_packet = None

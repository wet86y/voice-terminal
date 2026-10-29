"""SpeechControlMixin（情绪兼容层生命周期）：会话参数、队列快照和请求投影。"""
from __future__ import annotations

import logging

from ..tts.controls import DEFAULT_CONTROLS, SpeechControls, SpeechControlError, merge_controls, profile_for


logger = logging.getLogger(__name__)


class SpeechControlMixin:
    def _init_speech_controls(self) -> None:
        self.speech_control_epoch = 0  # speech_control_epoch（参数刷新代次），只留执行层。
        self._session_speech_controls = DEFAULT_CONTROLS  # _session_speech_controls（本会话保持值）。

    @property
    def speech_control_profile(self):
        return profile_for(self.tts, enabled=getattr(self.tts, "speech_control_enabled", True))

    @property
    def session_speech_controls(self) -> SpeechControls:
        """按当前实际能力读取会话保持值，不从已结束的播放句柄恢复。"""
        return self.speech_control_profile.normalize(self._session_speech_controls)

    def refresh_speech_controls(self, reason: str, *, reset: bool = True) -> None:
        """reset（清空保持值）与旧请求失效分离；监听边界仅推进代次。"""
        self.speech_control_epoch += 1
        if reset:
            self._session_speech_controls = DEFAULT_CONTROLS
        seen = set()
        for h in (self.handle, getattr(self, "_continuation_handle", None), *self._pending):
            if h is None or id(h) in seen:
                continue
            seen.add(id(h))
            if reset:
                h.speech_controls = DEFAULT_CONTROLS
            h.speech_control_epoch = self.speech_control_epoch
        client = self.tts.current_client() if callable(getattr(self.tts, "current_client", None)) else self.tts
        invalidate = getattr(client, "invalidate_control_prewarm", None)
        if callable(invalidate):
            invalidate()
        logger.info("speech.controls_refreshed reason=%s epoch=%d reset=%s", reason, self.speech_control_epoch, reset)

    def _control_values(self, client, backend, patch, *, base=DEFAULT_CONTROLS,
                        expected_epoch=None, expected_profile=None):
        enabled = getattr(self.tts, "speech_control_enabled", True)
        profile = profile_for(client, backend=backend, enabled=enabled)
        if (expected_epoch is not None and expected_epoch != self.speech_control_epoch
                or expected_profile is not None and expected_profile != profile.identity):
            return profile.normalize(base), False
        if patch is None:
            return profile.normalize(base), False
        try:
            return merge_controls(profile, base, patch), bool(patch)
        except SpeechControlError as exc:
            feedback = getattr(self.runtime, "minor_protocol_feedback", None)
            if isinstance(feedback, list):
                feedback.append({"field": "speak.controls", "applied": "retained_previous",
                                 "hint": str(exc) + " 参数未应用，正文仍按语音队列规则处理。"})
            logger.info("speech.controls_rejected reason=%s", exc)
            return profile.normalize(base), False

    def _handle_controls(self, h) -> SpeechControls:
        if getattr(h, "speech_control_epoch", None) != self.speech_control_epoch:
            return DEFAULT_CONTROLS
        profile = profile_for(h.tts_client, backend=h.tts_backend,
                              enabled=getattr(self.tts, "speech_control_enabled", True))
        return profile.normalize(getattr(h, "speech_controls", DEFAULT_CONTROLS))

    def _set_handle_controls(self, h, patch, *, base=None, expected_epoch=None, expected_profile=None) -> None:
        """在输出受理边界同步提交；无效或空补丁不能用旧队列覆盖会话保持值。"""
        h.speech_controls, updated = self._control_values(
            h.tts_client, h.tts_backend, patch,
            base=base if base is not None else self._handle_controls(h),
            expected_epoch=expected_epoch, expected_profile=expected_profile,
        )
        h.speech_control_epoch = self.speech_control_epoch
        if updated:
            self._commit_handle_controls(h, expected_epoch=self.speech_control_epoch)

    def _commit_handle_controls(self, h, *, expected_epoch: int) -> None:
        """只登记当前输出代次及实际能力下的已受理参数。"""
        if (expected_epoch != self.speech_control_epoch or h.reason
                or h.workspace.generation != self.generation or self.runtime.gate(h.identity)):
            return
        profile = profile_for(h.tts_client, backend=h.tts_backend,
                              enabled=getattr(self.tts, "speech_control_enabled", True))
        if profile.identity == self.speech_control_profile.identity:
            self._session_speech_controls = self._handle_controls(h)

    def speech_control_context(self) -> str:
        return self.speech_control_profile.protocol()

    def speech_control_snapshot(self) -> dict[str, str | float]:
        """speech_control_snapshot（会话参数快照）：空闲也可读取；默认项不发送。"""
        return self.session_speech_controls.non_defaults()

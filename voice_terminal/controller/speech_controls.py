"""SpeechControlMixin（情绪兼容层生命周期）：队列参数、刷新和上下文投影。"""
from __future__ import annotations

import logging

from ..tts.controls import DEFAULT_CONTROLS, SpeechControls, SpeechControlError, merge_controls, profile_for


logger = logging.getLogger(__name__)


class SpeechControlMixin:
    def _init_speech_controls(self) -> None:
        self.speech_control_epoch = 0  # speech_control_epoch（参数刷新代次），只留执行层。

    @property
    def speech_control_profile(self):
        return profile_for(self.tts, enabled=getattr(self.tts, "speech_control_enabled", True))

    def refresh_speech_controls(self, reason: str) -> None:
        """只刷新运行参数，保留用户的总开关；旧请求不能跨刷新提交补丁。"""
        self.speech_control_epoch += 1
        seen = set()
        for h in (self.handle, getattr(self, "_continuation_handle", None), *self._pending):
            if h is None or id(h) in seen:
                continue
            seen.add(id(h))
            h.speech_controls = DEFAULT_CONTROLS
            h.speech_control_epoch = self.speech_control_epoch
        client = self.tts.current_client() if callable(getattr(self.tts, "current_client", None)) else self.tts
        invalidate = getattr(client, "invalidate_control_prewarm", None)
        if callable(invalidate):
            invalidate()
        logger.info("speech.controls_refreshed reason=%s epoch=%d", reason, self.speech_control_epoch)

    def _control_values(self, client, backend, patch, *, base=DEFAULT_CONTROLS,
                        expected_epoch=None, expected_profile=None):
        enabled = getattr(self.tts, "speech_control_enabled", True)
        profile = profile_for(client, backend=backend, enabled=enabled)
        if (expected_epoch is not None and expected_epoch != self.speech_control_epoch
                or expected_profile is not None and expected_profile != profile.identity):
            return profile.normalize(base)
        if patch is None:
            return profile.normalize(base)
        try:
            return merge_controls(profile, base, patch)
        except SpeechControlError as exc:
            feedback = getattr(self.runtime, "minor_protocol_feedback", None)
            if isinstance(feedback, list):
                feedback.append({"field": "speak.controls", "applied": "retained_previous",
                                 "hint": str(exc) + " 参数未应用，正文仍按语音队列规则处理。"})
            logger.info("speech.controls_rejected reason=%s", exc)
            return profile.normalize(base)

    def _handle_controls(self, h) -> SpeechControls:
        if getattr(h, "speech_control_epoch", None) != self.speech_control_epoch:
            return DEFAULT_CONTROLS
        profile = profile_for(h.tts_client, backend=h.tts_backend,
                              enabled=getattr(self.tts, "speech_control_enabled", True))
        return profile.normalize(getattr(h, "speech_controls", DEFAULT_CONTROLS))

    def _set_handle_controls(self, h, patch, *, base=None, expected_epoch=None, expected_profile=None):
        h.speech_controls = self._control_values(
            h.tts_client, h.tts_backend, patch,
            base=base if base is not None else self._handle_controls(h),
            expected_epoch=expected_epoch, expected_profile=expected_profile,
        )
        h.speech_control_epoch = self.speech_control_epoch

    def speech_control_context(self) -> str:
        profile = self.speech_control_profile
        content = profile.protocol()
        h = self.handle or getattr(self, "_continuation_handle", None)
        if h is None or not profile.enabled:
            return content
        continuing = self._continuation_alive(h) and h is getattr(self, "_continuation_handle", None)
        playing = False
        if callable(getattr(self.player, "progress", None)):
            playing = bool(self.player.progress(h.workspace.playback_key).get("started")) and not h.reason
        if playing or continuing:
            summary = self._handle_controls(h).summary()
            if summary:
                content += "\n当前播报调节：" + summary
        return content

"""Product settings（产品设置）：保存后更新运行时，启动期间的修改自动补应用。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from typing import Any, Mapping

from .product import apply_settings_to_config
from .runtime_operations import runtime_operation

logger = logging.getLogger(__name__)


class ProductSettingsRuntimeMixin:
    async def switch_audio_enhancements(self, payload: Mapping[str, Any]) -> dict:
        """音频增强切换事务：立即停听、等待原生调用、核验并保存、恢复监听意图。"""
        from ..audio_enhancements import AudioEnhancements
        from ..analysis.client import AnalysisClient
        from ..application.resource_readiness import inspect_model_resources
        if not hasattr(self, "_enhancement_switch_lock"):
            self._enhancement_switch_lock = asyncio.Lock()
        async with self._enhancement_switch_lock:
            old = self.config.audio_enhancements
            new = AudioEnhancements.from_mapping({**old.snapshot(), **dict(payload)})
            if old == new:
                return new.snapshot()
            requirements = [resource for field, resource in (
                ("dual_pass", "paraformer-offline"), ("emotion", "emotion2vec-plus-base"),
                ("acoustic_events", "yamnet")) if getattr(new, field) and not getattr(old, field)]
            issues = await asyncio.to_thread(inspect_model_resources, self._project_root,
                                            self.config.model_dir, {"音频增强": requirements} if requirements else {})
            if issues:
                raise ValueError("；".join(issues.values()))
            controller = self.controller
            analysis = AnalysisClient(self.config.smart_turn.analysis_url)
            changed = {"emotion": new.emotion, "acoustic": new.acoustic_events}
            changed = {key: value for key, value in changed.items()
                       if value != (old.emotion if key == "emotion" else old.acoustic_events)}
            async def apply(settings):
                if new.dual_pass != old.dual_pass:
                    status = await self.asr.configure_enhancements(settings.dual_pass)
                    offline = status.get("offline", {})
                    if (offline.get("status") != ("READY" if settings.dual_pass else "DISABLED")
                            or settings.dual_pass and offline.get("device") != status.get("device")):
                        raise RuntimeError("双路精修未就绪或实际设备与主识别不一致")
                if changed:
                    selected = {key: settings.emotion if key == "emotion" else settings.acoustic_events for key in changed}
                    status = await analysis.configure(selected)
                    for key, enabled in selected.items():
                        if status.get(key, {}).get("status") != ("READY" if enabled else "DISABLED"):
                            raise RuntimeError(f"音频增强通道 {key} 未达到预期状态")
            self._enhancement_resume_requested = True
            labels = {"dual_pass": "双路精修", "emotion": "情绪背景", "acoustic_events": "声学事件"}
            label = "、".join(labels[key] for key in labels if getattr(new, key) != getattr(old, key))
            async with runtime_operation(self, "audio_enhancements", label, pause=True) as operation:
                self._enhancement_switching = True
                try:
                    await operation.update("loading")
                    await apply(new)
                    await operation.update("verifying")
                    if self.product_settings is not None:
                        await asyncio.to_thread(self.product_settings.save_audio_enhancements, new.snapshot())
                    self.config = replace(self.config, audio_enhancements=new)
                    if controller is not None:
                        controller.audio_enhancements = new
                        if not new.acoustic_events:
                            controller.perception["acoustic"].clear()
                except BaseException as failure:
                    await operation.update("rolling_back")
                    try:
                        await apply(old)
                    except Exception as rollback_error:
                        logger.exception("音频增强恢复失败，保持停止监听")
                        operation.prevent_resume()
                        if not isinstance(failure, asyncio.CancelledError):
                            raise RuntimeError(f"音频增强切换失败且恢复原模型失败，已停止监听：{rollback_error}") from failure
                    raise
                finally:
                    self._enhancement_switching = False
                    if not self._enhancement_resume_requested:
                        operation.prevent_resume()
            return new.snapshot()

    async def apply_product_settings(self, settings: Mapping[str, Any], *, label: str = "运行设置") -> bool:
        """apply_product_settings（即时应用）：保留当前输入、播放与会话历史。"""
        async with runtime_operation(self, "product_settings", label) as operation:
            applied = await self._apply_product_settings(settings)
            if not applied:
                operation.fail("运行设置应用失败")
            return applied

    async def _apply_product_settings(self, settings: Mapping[str, Any]) -> bool:
        async with self._session_lock:
            async with self._product_settings_lock:
                self.stage_product_settings(settings)
                if self.application_initializing:
                    return False
                applied = await self._apply_pending_product_settings()
                await self.bus.publish(self.snapshot())
                return applied

    async def _apply_pending_product_settings(self) -> bool:
        """启动汇合时或保存后应用；不再要求停止监听或开启一轮对话。"""
        pending = self._pending_product_settings
        if pending is None:
            return True
        previous = self.config
        # compute（模型运行设备）有独立的应用重启契约，不能被其他设置顺带加载。
        live_sections = {key: pending[key] for key in ("wake", "silence", "quiet_hours", "speaker", "screen_capture") if key in pending}
        next_config = replace(apply_settings_to_config(previous, live_sections), compute=previous.compute)
        wake_changed = next_config.wake != previous.wake
        old_worker = self.kws
        candidate = old_worker
        old_compute = dict(self._compute_models.get("kws", {}))
        try:
            # 旧工作线程在新模型成功加载前继续服务；原生构造在后台执行。
            if wake_changed:
                candidate = (await self._load_measured_model("kws", self._build_kws_worker, next_config)
                             if next_config.wake.enabled else None)
            if (wake_changed and not next_config.wake.enabled and self.controller is not None
                    and self.state.listening and self.state.input_owner == "kws"):
                if not await self.controller._ensure_asr_ready(preserve_listening=True):
                    raise ValueError("语音识别尚未就绪，无法从关键词待机切换为直接监听。")
            if next_config.speaker != previous.speaker:
                await self.set_speaker_settings(vars(next_config.speaker))
            self.history.screen_capture.configure(next_config.screen_capture)
            if not next_config.screen_capture.enabled and self.controller is not None:
                episode = getattr(self.controller, "episode", None)
                if episode is not None:
                    episode.invalidate_screen_capture()
            self.config = next_config
            self.quiet_policy.config = next_config.quiet_hours
            self.state.wake_always_on = bool(next_config.wake.enabled)
            self.kws = candidate
            if self.controller is not None:
                self.controller.set_agent_options(next_config.agent)
                if wake_changed:
                    await self.controller.set_wake_settings(candidate, next_config.wake)
            if wake_changed:
                self.state.set_service("kws", ("RUNNING" if self.state.listening and self.state.input_owner == "kws"
                                               else "READY") if candidate is not None else "DISABLED")
                if candidate is None:
                    self._compute_models["kws"] = {"requested_device": previous.compute.devices.get("kws", "cpu"),
                                                   "device": None, "service": "DISABLED", "load_error": None}
            if wake_changed and old_worker is not None:
                try:
                    await asyncio.to_thread(old_worker.close)
                except Exception:
                    logger.warning("product.old_kws_cleanup_failed", exc_info=True)
            if self._pending_product_settings is pending:
                self._pending_product_settings = None
            self.startup_errors = [item for item in self.startup_errors if not str(item).startswith("product_settings:")]
            return True
        except Exception as exc:
            logger.exception("product.settings_apply_failed")
            self.history.screen_capture.configure(previous.screen_capture)
            if candidate is not None and candidate is not old_worker:
                await asyncio.to_thread(candidate.close)
            self.config = previous
            self.quiet_policy.config = previous.quiet_hours
            self.kws = old_worker
            self._compute_models["kws"] = old_compute
            self.state.wake_always_on = bool(previous.wake.enabled)
            if self.controller is not None:
                self.controller.set_agent_options(previous.agent)
                if wake_changed:
                    await self.controller.set_wake_settings(old_worker, previous.wake)
            self.startup_errors = [item for item in self.startup_errors if not str(item).startswith("product_settings:")]
            self.startup_errors.append(f"product_settings: {str(exc)[:300]}")
            return False

    async def refresh_custom_instruction_settings(self) -> None:
        """refresh_custom_instruction_settings（刷新自定义指令）：资料文档仅在新会话载入。"""
        store = self.product_settings
        if store is None:
            return
        instructions = store.settings_only().get("custom_instructions", [])
        async with self._session_lock:
            self._instruction_snapshot = [dict(item) for item in instructions]
            # 保留当前会话已冻结的用户资料；人格文档由新会话从磁盘读取。
            self.shadow_context.set_product_context(self._shadow_user_profile, self._instruction_snapshot)
            episode = getattr(self.controller, "episode", None)
            if episode is not None:
                episode.refresh_custom_instructions(self._instruction_snapshot)
            self._persist_voice_identity()
            await self.bus.publish(self.snapshot())

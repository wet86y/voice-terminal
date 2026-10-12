"""Product settings（产品设置）：保存后更新运行时，启动期间的修改自动补应用。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from typing import Any, Mapping

from .product import apply_settings_to_config

logger = logging.getLogger(__name__)


class ProductSettingsRuntimeMixin:
    async def apply_product_settings(self, settings: Mapping[str, Any]) -> bool:
        """apply_product_settings（即时应用）：保留当前输入、播放与会话历史。"""
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
        live_sections = {key: pending[key] for key in ("wake", "silence", "quiet_hours", "speaker") if key in pending}
        next_config = replace(apply_settings_to_config(previous, live_sections), compute=previous.compute)
        wake_changed = next_config.wake != previous.wake
        old_worker = self.kws
        candidate = old_worker
        old_compute = dict(self._compute_models.get("kws", {}))
        try:
            # 旧工作线程在新模型成功加载前继续服务；原生构造在后台执行。
            if wake_changed:
                candidate = (await self._load_native(self._build_kws_worker, next_config)
                             if next_config.wake.enabled else None)
            if (wake_changed and not next_config.wake.enabled and self.controller is not None
                    and self.state.listening and self.state.input_owner == "kws"):
                if not await self.controller._ensure_asr_ready(preserve_listening=True):
                    raise ValueError("语音识别尚未就绪，无法从关键词待机切换为直接监听。")
            if next_config.speaker != previous.speaker:
                await self.set_speaker_settings(vars(next_config.speaker))
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

    async def refresh_prompt_settings(self) -> None:
        """refresh_prompt_settings（更新人格与指令）：后续模型请求直接读取新快照。"""
        store = self.product_settings
        if store is None:
            return
        documents = await asyncio.to_thread(store.read_persona)
        instructions = store.settings_only().get("custom_instructions", [])
        async with self._session_lock:
            self._instruction_snapshot = [dict(item) for item in instructions]
            self._shadow_user_profile = documents.get("user", "")
            self.shadow_context.set_product_context(self._shadow_user_profile, self._instruction_snapshot)
            episode = getattr(self.controller, "episode", None)
            if episode is not None:
                from ..agent.persona import PersonaDocuments
                episode.refresh_prompt_settings(
                    PersonaDocuments(personality=documents.get("personality", ""), user=self._shadow_user_profile),
                    self._instruction_snapshot,
                )
            self._persist_voice_identity()
            await self.bus.publish(self.snapshot())

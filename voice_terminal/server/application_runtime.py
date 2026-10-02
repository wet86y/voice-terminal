"""Application-managed operations（应用受管操作），保留会话账本与唯一调度器。"""
from __future__ import annotations

import asyncio


class ApplicationRuntimeMixin:
    def begin_application_shutdown(self) -> None:
        """Shutdown gate（退出门禁）立即阻止委托组件的新启动与自动重启。"""
        self._application_stopping = True
        self.dsh._closing = True
        task = self.dsh._start_task
        if task is not None and not task.done():
            task.cancel()

    async def application_delegated_status(self) -> dict:
        if not hasattr(self, "_application_dsh_lock"):
            self._application_dsh_lock = asyncio.Lock()
        async with self._application_dsh_lock:
            process = self.dsh._process
            if self.dsh.status().state == "ready" and process is not None and process.pid != getattr(self, "_application_bound_dsh_pid", None):
                self._application_bound_dsh_pid = process.pid
                self._application_dsh_error = ""
                try:
                    reconnect = getattr(self.shadow_session, "reconnect", None)
                    if reconnect:
                        await reconnect()
                except Exception as exc:
                    self._application_dsh_error = f"代理桥接重新绑定失败（{type(exc).__name__}），请重试组件。"
        return self.application_component_status()

    def application_component_status(self) -> dict:
        dsh = self.dsh.status()
        process = self.dsh._process
        mapping = {"disabled": "disabled", "starting": "starting", "ready": "ready", "unavailable": "failed", "exited": "failed"}
        dsh_state = mapping[dsh.state]
        enabled = self.config.dsh.enabled and self.config.dsh.auto_start
        if not enabled:
            dsh_state = "disabled"
        elif process is None and self.dsh._start_task is None and not dsh.message:
            dsh_state = "waiting"
        binding_error = getattr(self, "_application_dsh_error", "")
        if dsh_state == "ready" and binding_error:
            dsh_state = "failed"
        speaker_process = self._speaker_worker_process
        speaker_enabled = self.config.speaker.mode != "off" or self.config.speaker.owner_only_barge_in
        speaker_service = self.speaker_status()
        service = speaker_service.get("service", "DISABLED")
        speaker_state = {"READY": "ready", "STARTING": "starting", "DISABLED": "disabled"}.get(service, "failed")
        return {
            "dsh": {"state": dsh_state, "enabled": enabled, "phase": dsh.message or ("桥接就绪" if dsh_state == "ready" else "等待启动"),
                "error": binding_error or dsh.message if dsh_state == "failed" else "", "pid": process.pid if process and process.returncode is None else None},
            "speaker": {"state": speaker_state, "enabled": speaker_enabled or speaker_process is not None,
                "phase": "声纹连接已就绪" if speaker_state == "ready" else "按配置停用" if speaker_state == "disabled" else "声纹工作进程不可用",
                "error": str(speaker_service.get("last_error") or "") if speaker_state == "failed" else "",
                "pid": speaker_process.pid if speaker_process and speaker_process.returncode is None else None},
        }

    async def application_control(self, action: str) -> dict:
        if getattr(self, "_application_stopping", False):
            return {"ok": False, "message": "应用正在退出，不能启动或恢复组件。"}
        if action == "pause":
            await self.stop_listening()
            return {"ok": True}
        if action in {"dsh-start", "dsh-retry"}:
            if action == "dsh-retry":
                await self.dsh.close()
                self.dsh._restart_count = 0
                self.dsh._closing = False
                self._application_bound_dsh_pid = None
                self._application_dsh_error = ""
            self.dsh.begin_start()
            ready = await self.dsh.wait_ready()
            status = await self.application_delegated_status()
            ready = ready and status["dsh"]["state"] == "ready"
            return {"ok": ready, "message": status["dsh"]["error"]}
        if action == "speaker-retry":
            ready = await self.ensure_speaker_worker()
            return {"ok": ready, "message": "声纹工作进程恢复失败" if not ready else ""}
        if action == "reinitialize":
            async with self._session_lock:
                await self.close()
                self._prepare_for_reinitialize()
                self.startup_errors.clear()
                await self.initialize()
                await self.bus.publish(self.snapshot())
                health = self.health_payload()
                services = health["services"]
                ready = all(services.get(key) == "READY" for key in ("audio", "asr", "llm", "tts"))
                return {"ok": ready, "message": "语音业务未完整恢复，请查看核心组件状态" if not ready else ""}
        raise ValueError("未知受管操作")

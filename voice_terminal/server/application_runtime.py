"""Application-managed operations（应用受管操作），保留会话账本与唯一调度器。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from ..application.managed import report as application_report


class ApplicationRuntimeMixin:
    def begin_dsh_start(self) -> None:
        """begin_dsh_start（提前启动代理）：独立于三路模型预算，仅保留 95% 内存门槛。"""
        from ..application.dsh_startup import begin_dsh_start
        begin_dsh_start(self.dsh)

    @property
    def application_initializing(self) -> bool:
        """application_initializing（业务初始化中）：接口可用不代表可以开启语音会话。"""
        return self._application_phased_startup and not self._application_session_ready

    async def restore_startup_compute(self) -> None:
        """restore_startup_compute（恢复加载失败的设备选项）：在完整初始化后执行。"""
        if self.product_settings is None:
            return
        try:
            status = await self.compute_status()
            restored = await asyncio.to_thread(
                self.product_settings.revert_failed_compute_devices, status.get("models", {}))
            if restored:
                self.startup_errors.append("compute_device_restore（计算设备恢复）：加载失败后已恢复上次成功设备（"
                    + ", ".join(f"{name}={device}" for name, device in sorted(restored.items())) + "）。")
        except Exception as exc:
            self.startup_errors.append(f"compute_status_startup（启动计算状态）：{type(exc).__name__}: {str(exc)[:240]}")

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
            if (self.dsh.status().state == "ready" and process is not None
                    and not self.application_initializing
                    and process.pid != getattr(self, "_application_bound_dsh_pid", None)):
                self._application_bound_dsh_pid = process.pid
                self._application_dsh_error = ""
                try:
                    reconnect = getattr(self.shadow_session, "reconnect", None)
                    if reconnect:
                        await reconnect()
                    elif self.config.shadow_agent.enabled and self.config.shadow_agent.backend == "dsh":
                        raise RuntimeError("代理会话尚未建立")
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
        elif getattr(self.dsh, "_waiting_for_memory", False):
            dsh_state = "waiting"
        elif process is None and self.dsh._start_task is None and not dsh.message:
            dsh_state = "waiting"
        binding_error = getattr(self, "_application_dsh_error", "")
        binding_pending = enabled and dsh_state == "ready" and self.application_initializing
        if binding_pending:
            dsh_state = "waiting"
        if dsh_state == "ready" and binding_error:
            dsh_state = "failed"
        speaker_process = self._speaker_worker_process
        local_tts = getattr(self.tts, "local", None) if self.tts is not None else None
        local_tts_server = getattr(local_tts, "server", None)
        local_tts_process = getattr(local_tts_server, "_process", None)
        local_tts_pid = None
        if local_tts_process is not None:
            try:
                if local_tts_process.poll() is None:
                    local_tts_pid = local_tts_process.pid
            except (AttributeError, OSError):
                pass
        speaker_enabled = self.config.speaker.mode != "off" or self.config.speaker.owner_only_barge_in
        speaker_service = self.speaker_status()
        service = speaker_service.get("service", "DISABLED")
        speaker_state = {"READY": "ready", "STARTING": "starting", "DISABLED": "disabled"}.get(service, "failed")
        return {
            # startup（内部启动状态）：用于区分等待内存与会话初始化失败。
            "startup": {"local_ready": self._application_local_ready,  # local_ready（本地加载完成）。
                        "session_ready": self._application_session_ready},  # session_ready（会话建立完成）。
            "dsh": {"state": dsh_state, "enabled": enabled, "phase": "桥接已就绪，等待语音会话绑定" if binding_pending else dsh.message or ("桥接就绪" if dsh_state == "ready" else "等待启动"),
                "error": binding_error or dsh.message if dsh_state == "failed" else "", "pid": process.pid if process and process.returncode is None else None},
            "speaker": {"state": speaker_state, "enabled": speaker_enabled or speaker_process is not None,
                "phase": "声纹连接已就绪" if speaker_state == "ready" else "按配置停用" if speaker_state == "disabled" else "加载声纹工作进程" if speaker_state == "starting" else "声纹工作进程不可用",
                "error": str(speaker_service.get("last_error") or "") if speaker_state == "failed" else "",
                "pid": speaker_process.pid if speaker_process and speaker_process.returncode is None else None},
            "local_tts": {"pid": local_tts_pid},  # local_tts（本地语音合成子进程），供宿主独立统计资源。
            # capabilities（分能力行）：宿主与界面据此显示“启动中/不可用”并单独恢复。
            "capabilities": self.capability_rows(),
        }

    def _apply_managed_endpoints(self, endpoint_table: dict | None) -> None:
        """Apply the host's current endpoint table（宿主当前端点表）before reconnecting clients."""
        if not isinstance(endpoint_table, dict):
            return
        from ..application.ports import validate_endpoint_url
        asr_url = endpoint_table.get("asr")
        analysis_url = endpoint_table.get("analysis")
        speaker_url = endpoint_table.get("speaker")
        config = self.config
        if isinstance(asr_url, str):
            asr_url = validate_endpoint_url("asr", asr_url)["url"]
            config = replace(config, asr=replace(config.asr, worker_url=asr_url))
        if isinstance(analysis_url, str):
            analysis_url = validate_endpoint_url("analysis", analysis_url)["url"]
            config = replace(config, smart_turn=replace(config.smart_turn, analysis_url=analysis_url))
        if isinstance(speaker_url, str):
            speaker_url = validate_endpoint_url("speaker", speaker_url)["url"]
            config = replace(config, speaker_worker=replace(config.speaker_worker, worker_url=speaker_url))
        self.config = config

    async def application_control(self, action: str, endpoint_table: dict | None = None,
                                  capability: str | None = None) -> dict:
        if getattr(self, "_application_stopping", False):
            return {"ok": False, "message": "应用正在退出，不能启动或恢复组件。"}
        if action == "capability-retry":
            # 能力级恢复：只重建一个能力，不重启语音服务进程，也不中断其他能力。
            return await self.retry_capability(str(capability or ""))
        if action in {"initialize-local", "initialize-session"}:
            if not self._application_phased_startup:
                return {"ok": False, "message": "当前进程未启用分阶段启动。"}
            async with self._application_startup_lock:
                if getattr(self, "_application_stopping", False):
                    return {"ok": False, "message": "应用正在退出。"}
                if action == "initialize-local":
                    if not self._application_local_ready:
                        await self.initialize_local()
                    application_report("voice", "本地模型已加载，等待工作进程汇合")
                    return {"ok": True}
                if not self._application_local_ready:
                    return {"ok": False, "message": "本地模型尚未完成加载。"}
                if not self._application_session_ready:
                    self._apply_managed_endpoints(endpoint_table)
                    await self.initialize_session()
                    await self.restore_startup_compute()
                    await self.bus.publish(self.snapshot())
                services = self.health_payload()["services"]
                ready = all(services.get(key) == "READY" for key in ("audio", "asr", "llm", "tts"))
                return {"ok": ready, "message": "核心能力未完整就绪，请查看组件状态。" if not ready else ""}
        if self.application_initializing and action not in {"pause", "dsh-start", "dsh-wait"}:
            return {"ok": False, "message": "组件仍在加载，请等待本轮启动结束后恢复。"}
        if action == "pause":
            await self.stop_listening()
            return {"ok": True}
        if action in {"dsh-start", "dsh-retry", "dsh-wait"}:
            if action == "dsh-retry":
                await self.dsh.close()
                self.dsh._restart_count = 0
                self.dsh._closing = False
                self._application_bound_dsh_pid = None
                self._application_dsh_error = ""
            if action != "dsh-wait":
                self.begin_dsh_start()
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
                self._apply_managed_endpoints(endpoint_table)
                self.startup_errors.clear()
                await self.initialize()
                await self.bus.publish(self.snapshot())
                health = self.health_payload()
                services = health["services"]
                ready = all(services.get(key) == "READY" for key in ("audio", "asr", "llm", "tts"))
                # session_initialized（语音会话初始化流程已完成）与 ok（全部 Voice 核心能力就绪）分开报告，
                # asr_ready（语音服务中的识别会话实际健康就绪）确认本地工作进程重连，同时仍保留缺少 LLM/TTS（大语言模型/语音合成）凭据时的 Voice 不就绪状态。
                return {
                    "ok": ready,
                    "session_initialized": True,
                    "asr_ready": services.get("asr") == "READY",
                    "message": "语音业务未完整恢复，请查看核心组件状态" if not ready else "",
                }
        raise ValueError("未知受管操作")

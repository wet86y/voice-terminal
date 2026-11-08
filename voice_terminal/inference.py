"""主语音推理策略：共享凭据、按请求边界切换，不重建语音或后台会话。"""
from __future__ import annotations
import asyncio
import os
import json
from dataclasses import replace

# MODEL_CAPABILITIES（模型能力登记）：图像传输与思考策略共用一个来源。
MODEL_CAPABILITIES = {
    "deepseek-flash": {"images": "files", "forced_effort": None},
    "MiniMax-M3.1-Flash": {"images": "inline", "forced_effort": "low"},
}


def inference_config(config, backend: str):
    """backend（后端）：只有两种正式模型，旁路配置保持独立。"""
    if backend not in {"deepseek", "minimax"}:
        raise ValueError("推理后端无效")
    mini = backend == "minimax"
    return replace(config, provider=backend,
                   base_url="https://api.minimaxi.com/v1" if mini else "https://api.deepseek.com",
                   model="MiniMax-M3.1-Flash" if mini else "deepseek-flash",
                   api_key_env="MINIMAX_API_KEY" if mini else "DEEPSEEK_API_KEY",
                   transport="responses", thinking="enabled",
                   reasoning_effort="low", allowed_reasoning=("low",) if mini else ("off", "low"))


class InferenceRuntimeMixin:
    def inference_snapshot(self):
        model = getattr(getattr(getattr(self, "config", None), "llm", None), "model", "deepseek-flash")
        mini = model == "MiniMax-M3.1-Flash"
        return {"backend": "minimax" if mini else "deepseek", "model": model,
                "dsh_sync_pending": getattr(self, "_minimax_dsh_sync_pending", False),
                "pending_backend": getattr(self, "_pending_inference", None),
                "reasoning_mode_selectable": not mini,
                "reasoning_effort": "low" if mini or getattr(self, "reasoning_mode", "fast") == "deep" else "none"}

    async def switch_inference(self, backend: str, *, persist=True):
        from .providers.deepseek import DeepSeekClient
        if not hasattr(self, "_inference_lock"):
            self._inference_lock = asyncio.Lock()
        async with self._inference_lock:
            next_config = inference_config(self.config.llm, backend)
            key = self._api_key(next_config.api_key_env)
            if not key:
                raise ValueError("请先保存所选供应方的接口密钥。")
            episode = getattr(self.controller, "episode", None)
            self._pending_inference = backend
            if episode:
                episode.inference_switch_pending = True
            candidate = None
            try:
                await self.bus.publish(self.snapshot())
                # 已派发请求及恢复继续使用原客户端；新的普通请求暂缓。
                while episode and (getattr(episode, "_dispatch_starting", False) or getattr(episode, "_recovery_starting", False) or episode.active_request is not None or episode.recovery_queue
                                   or "LLM_RETRY" in episode.pending):
                    if getattr(episode, "closed", False):
                        raise ValueError("会话已结束，请重新选择推理后端。")
                    await asyncio.sleep(.025)
                async with self._session_lock:
                    key = self._api_key(next_config.api_key_env)  # 等待期间可能保存了新凭据，安装前重新读取。
                    candidate = DeepSeekClient(base_url=next_config.base_url, model=next_config.model,
                        api_key=key, provider=backend, api_key_env=next_config.api_key_env,
                        thinking="enabled", reasoning_effort="low", allowed_reasoning=next_config.allowed_reasoning,
                        transport="responses", max_output_tokens=next_config.max_output_tokens,
                        timeout_ms=next_config.timeout_ms, protocol_recovery_limit=next_config.protocol_recovery_limit,
                        transport_retry_limit=next_config.transport_retry_limit)
                    candidate.set_session_id(getattr(self.conversation, "conversation_id", None))
                    if persist:
                        self.product_settings.save_inference({"backend": backend})
                    old = self.llm
                    if self.config.llm.model != "MiniMax-M3.1-Flash":
                        self._deepseek_reasoning_mode = self.reasoning_mode
                    self.reasoning_mode = "deep" if backend == "minimax" else getattr(self, "_deepseek_reasoning_mode", self.reasoning_mode)
                    self.config = replace(self.config, llm=next_config)
                    self.llm = candidate
                    if self.controller:
                        self.controller.llm = candidate
                        self.controller.set_reasoning_mode(self.reasoning_mode)
                        self.controller.registry.supports_images = candidate.supports_images
                    if episode:
                        episode.provider = candidate
                        # 图片源保持中立；供应方文件引用缓存不可跨后端恢复。
                    candidate = None
                    self.state.set_service("llm", "READY")
                    self._clear_service_error("llm")
                if old:
                    try:
                        await old.close()
                    except Exception:
                        pass  # 旧网络连接回收失败不改变已保存的选择。
            finally:
                if candidate:
                    await candidate.close()
                self._pending_inference = None
                if episode:
                    episode.inference_switch_pending = False
                    if episode.queue.empty():
                        episode.queue.put_nowait(True)  # 只唤醒已有待处理输入，不创造模型请求。
                await self.bus.publish(self.snapshot())
            return self.inference_snapshot()

    async def refresh_provider_credentials(self, registry):
        """只更新后续网络请求；已开始的请求已持有自己的认证头。"""
        credentials = await registry.runtime_fixed_credentials()
        os.environ.update(credentials)
        self._credential_overrides.update(credentials)
        if self.llm:
            key = self._api_key(self.config.llm.api_key_env) or self.llm.api_key
            if self.llm.api_key != key:
                self.llm.api_key = key
        elif credentials.get(self.config.llm.api_key_env):
            await self.switch_inference("minimax" if self.config.llm.model == "MiniMax-M3.1-Flash" else "deepseek", persist=False)
        tts = getattr(self, "tts", None)
        for client in (tts, getattr(tts, "remote", None)):
            if client is not None and hasattr(client, "api_key") and credentials.get("MINIMAX_API_KEY"):
                update = getattr(client, "update_api_key", None)
                if update:
                    update(credentials["MINIMAX_API_KEY"])
                else:
                    client.api_key = credentials["MINIMAX_API_KEY"]
        await self.sync_minimax_dsh()

    async def sync_minimax_dsh(self):
        """DSH 不可用时保留待同步状态，启动完成后重试。"""
        key = self._api_key("MINIMAX_API_KEY")
        if not key:
            return
        if not hasattr(self, "_minimax_sync_lock"):
            self._minimax_sync_lock = asyncio.Lock()
        async with self._minimax_sync_lock:
            try:
                from .paths import atomic_json
                marker = self.dsh.paths.config / "dsh-minimax-bootstrap.json"
                try:
                    initialized = json.loads(marker.read_text(encoding="utf-8")).get("bootstrap_version", 0) >= 1
                except FileNotFoundError:
                    initialized = False
                await self.dsh.bridge.sync_minimax(key, initialize=not initialized)
                if not initialized:
                    atomic_json(marker, {"bootstrap_version": 1})
                self._minimax_dsh_sync_pending = False
            except Exception:
                self._minimax_dsh_sync_pending = True
                task = getattr(self.dsh, "_credential_sync_task", None)
                if (task is None or task.done()) and not self.dsh._closing:
                    self.dsh._credential_sync_task = asyncio.create_task(self._retry_minimax_sync(), name="minimax-credential-sync")

    async def _retry_minimax_sync(self):
        """有限退避重试：不重启或取消任何 DSH（后台执行宿主）任务。"""
        for delay in (1, 2, 5, 15, 30):
            await asyncio.sleep(delay)
            if self.dsh._closing or not getattr(self, "_minimax_dsh_sync_pending", False):
                return
            await self.sync_minimax_dsh()
            await self.bus.publish(self.snapshot())

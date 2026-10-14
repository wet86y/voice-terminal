"""TTS backend（语音合成后端）选择与事务绑定兼容层。"""

from __future__ import annotations

import asyncio
from typing import Any


class TtsBackendRouter:
    def __init__(self, *, remote: Any | None, local: Any | None = None,
                 backend: str = "minimax") -> None:
        if backend not in {"minimax", "voxcpm1"}:
            raise ValueError("tts.backend（语音运行后端）仅接受 minimax 或 voxcpm1。")
        if backend == "voxcpm1" and local is None:
            raise ValueError("本地 TTS 后端尚未接入。")
        self.remote = remote
        self.local = local
        self.backend = backend
        self._local_leases = 0
        self._local_ready = backend == "voxcpm1"
        self._switch_lock = asyncio.Lock()
        self._local_switching = False

    @property
    def transport(self) -> str:
        return getattr(self.current_client(), "transport", "websocket")

    @property
    def scheduling(self) -> str:
        return getattr(self.current_client(), "scheduling", "auto")

    @property
    def effective_scheduling(self) -> str:
        return getattr(self.current_client(), "effective_scheduling", self.scheduling)

    @property
    def capabilities(self):
        from .capabilities import capabilities_of
        return capabilities_of(self.current_client())

    @property
    def supports_prewarm(self) -> bool:
        client = self.current_client()
        return bool(client is not None and callable(getattr(client, "prewarm", None)))

    def current_client(self) -> Any | None:
        return self.local if self.backend == "voxcpm1" else self.remote

    @property
    def local_model(self) -> str | None:
        """local_model（当前本地模型选择值）。"""
        if self.local is None:
            return None
        return getattr(self.local, "local_model", None)

    @property
    def active_local_sessions(self) -> int:
        """active_local_sessions（当前占用本地 TTS 的会话数）。"""
        return self._local_leases

    def bind(self) -> tuple[str, Any | None]:
        backend = self.backend
        client = self.current_client()
        if backend == "voxcpm1":
            if self._local_switching:
                raise RuntimeError("本地 TTS 模型正在切换，请等待切换完成后再发起播报。")
            self._local_leases += 1
        return backend, client

    async def ensure_ready(self) -> None:
        client = self.current_client()
        ensure_ready = getattr(client, "ensure_ready", None)
        if callable(ensure_ready):
            await ensure_ready()
            if self.backend == "voxcpm1":
                self._local_ready = True

    def release(self, backend: str) -> None:
        if backend != "voxcpm1":
            return
        self._local_leases = max(0, self._local_leases - 1)
        if self._local_leases == 0 and self.backend != "voxcpm1":
            asyncio.create_task(self._stop_after_drain(), name="local-tts-drain-stop")

    async def _stop_after_drain(self) -> None:
        async with self._switch_lock:
            if (self._local_leases == 0 and self.backend != "voxcpm1"
                    and self.local is not None and self._local_ready):
                await self.local.close()
                self._local_ready = False

    async def switch(self, backend: str) -> None:
        if backend not in {"minimax", "voxcpm1"}:
            raise ValueError("tts.backend（语音运行后端）仅接受 minimax 或 voxcpm1。")
        async with self._switch_lock:
            if backend == "voxcpm1":
                if self.local is None:
                    raise ValueError("本地 TTS 后端尚未接入。")
                await self.local.ensure_ready()
                self._local_ready = True
            self.backend = backend
            if (backend == "minimax" and self.local is not None
                    and self._local_leases == 0 and self._local_ready):
                await self.local.close()
                self._local_ready = False

    async def prewarm(self, *, turn_id: str | None = None) -> dict[str, Any]:
        client = self.current_client()
        if client is None:
            return {"status": "DISABLED", "reason": "provider_unavailable"}
        local_backend = self.backend == "voxcpm1"
        if local_backend:
            if self._local_switching:
                return {"status": "DISABLED", "reason": "model_switching"}
            self._local_leases += 1
        try:
            if local_backend:
                ensure_ready = getattr(client, "ensure_ready", None)
                if callable(ensure_ready):
                    await ensure_ready()
                    self._local_ready = True
            prewarm = getattr(client, "prewarm", None)
            if not callable(prewarm):
                return {"status": "DISABLED", "reason": "unsupported"}
            return await prewarm(turn_id=turn_id)
        finally:
            if local_backend:
                self.release("voxcpm1")

    async def switch_local_model(self, local_model: str) -> None:
        """在没有本地 TTS 会话租约时切换模型。"""
        if self.backend != "voxcpm1":
            raise RuntimeError("当前 TTS 后端未启用本地 VoxCPM。")
        if self.local is None:
            raise RuntimeError("本地 TTS 后端尚未接入。")
        if local_model not in {"voxcpm1", "voxcpm2"}:
            raise ValueError("local_model（本地语音合成模型）必须为 voxcpm1 或 voxcpm2。")
        async with self._switch_lock:
            if self._local_leases:
                raise RuntimeError("当前仍有 TTS（语音合成）会话，请等播报事务结束后再切换模型。")
            if self._local_switching:
                raise RuntimeError("本地 TTS 模型正在切换，请稍候。")
            switch_model = getattr(self.local, "switch_model", None)
            if not callable(switch_model):
                raise RuntimeError("当前本地 TTS 客户端不支持模型热切换。")
            self._local_switching = True
            try:
                await switch_model(local_model)
                self._local_ready = True
            finally:
                self._local_switching = False

    async def discard_prewarm(self, *, turn_id: str | None = None) -> None:
        if self.remote is not None:
            await self.remote.discard_prewarm(turn_id=turn_id)

    async def close(self) -> None:
        if self.remote is not None:
            await self.remote.close()

    async def shutdown(self) -> None:
        try:
            await self.close()
        finally:
            async with self._switch_lock:
                if self.local is not None:
                    try:
                        await self.local.close()
                    finally:
                        self._local_ready = False

"""Transaction-bound TTS provider compatibility layer.

The product currently uses the remote provider directly. This adapter keeps
the transaction binding boundary available for a future local provider without
starting or importing any local synthesis runtime.
"""

from __future__ import annotations

import asyncio
from typing import Any


class TtsBackendRouter:
    def __init__(self, *, remote: Any | None, local: Any | None = None,
                 backend: str = "minimax") -> None:
        if backend not in {"minimax", "cosyvoice"}:
            raise ValueError("tts.backend 仅接受 minimax 或 cosyvoice。")
        if backend == "cosyvoice" and local is None:
            raise ValueError("本地 TTS 后端尚未接入。")
        self.remote = remote
        self.local = local
        self.backend = backend
        self._local_leases = 0
        self._local_ready = backend == "cosyvoice"
        self._switch_lock = asyncio.Lock()

    @property
    def transport(self) -> str:
        return getattr(self.current_client(), "transport", "websocket")

    def current_client(self) -> Any | None:
        return self.local if self.backend == "cosyvoice" else self.remote

    def bind(self) -> tuple[str, Any | None]:
        backend = self.backend
        client = self.current_client()
        if backend == "cosyvoice":
            self._local_leases += 1
        return backend, client

    def release(self, backend: str) -> None:
        if backend != "cosyvoice":
            return
        self._local_leases = max(0, self._local_leases - 1)
        if self._local_leases == 0 and self.backend != "cosyvoice":
            asyncio.create_task(self._stop_after_drain(), name="local-tts-drain-stop")

    async def _stop_after_drain(self) -> None:
        async with self._switch_lock:
            if (self._local_leases == 0 and self.backend != "cosyvoice"
                    and self.local is not None and self._local_ready):
                await self.local.close()
                self._local_ready = False

    async def switch(self, backend: str) -> None:
        if backend not in {"minimax", "cosyvoice"}:
            raise ValueError("tts.backend 仅接受 minimax 或 cosyvoice。")
        async with self._switch_lock:
            if backend == "cosyvoice":
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
        if self.backend == "minimax" and self.remote is not None:
            return await self.remote.prewarm(turn_id=turn_id)
        return {"status": "DISABLED", "reason": "local_provider"}

    async def discard_prewarm(self, *, turn_id: str | None = None) -> None:
        if self.remote is not None:
            await self.remote.discard_prewarm(turn_id=turn_id)

    async def close(self) -> None:
        if self.remote is not None:
            await self.remote.close()

    async def shutdown(self) -> None:
        await self.close()
        async with self._switch_lock:
            if self.local is not None and self._local_ready:
                await self.local.close()
                self._local_ready = False

"""Transaction-bound provider selection owned by the Voice Server."""

from __future__ import annotations

import asyncio
from typing import Any

from .cosyvoice_runtime import CosyVoiceTtsClient, CosyVoiceWorkerManager


class TtsBackendRouter:
    def __init__(self, *, remote: Any | None, worker: CosyVoiceWorkerManager, backend: str) -> None:
        self.remote = remote
        self.local = CosyVoiceTtsClient(worker)
        self.worker = worker
        self.backend = backend
        self._local_leases = 0
        self._switch_lock = asyncio.Lock()

    @property
    def transport(self) -> str:
        return getattr(self.current_client(), "transport", "worker")

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
            asyncio.create_task(self._stop_after_drain(), name="cosyvoice-drain-stop")

    async def _stop_after_drain(self) -> None:
        async with self._switch_lock:
            if self._local_leases == 0 and self.backend != "cosyvoice":
                await self.worker.stop()

    async def switch(self, backend: str) -> None:
        if backend not in {"minimax", "cosyvoice"}:
            raise ValueError("tts.backend 仅接受 minimax 或 cosyvoice。")
        async with self._switch_lock:
            if backend == "cosyvoice":
                await self.worker.ensure_ready()
                self.worker.state = "ready"
            self.backend = backend
            if backend == "minimax" and self._local_leases == 0:
                await self.worker.stop()
            elif backend == "minimax":
                self.worker.state = "draining"

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
        await self.worker.stop()

"""Official 2pass orchestration（官方双路编排）的共享离线模型通道。

参考 FunASR runtime/python/websocket/funasr_wss_server.py；声学边界由宿主提供。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

from ..resources import model_thread_budget, model_load_footprint


class OfflineParaformer:
    def __init__(self, model_dir: Path, device: str):
        from ..analysis.snapshot import verify_snapshot
        self.identity = verify_snapshot(model_dir, "paraformer-offline")
        from .model_loader import auto_model_class
        from ..compute import loaded_funasr_device
        self.requested_device = device
        self.device = device
        self.runtime = "PyTorch（推理框架）"
        self.model_file = str(model_dir / "model.pt")
        self.model = auto_model_class(model_dir)(
            model=str(model_dir), device=device, ncpu=model_thread_budget(),
            disable_update=True, disable_pbar=True,
        )
        actual = loaded_funasr_device(self.model, device)
        if str(actual).replace("cuda:0", "cuda") != str(device).replace("cuda:0", "cuda"):
            raise RuntimeError(f"双路必须使用相同设备：主路 {device}，离线路 {actual}")
        self.warmed = False

    def __call__(self, pcm: bytes) -> dict:
        from ..audio.allocation import pcm_samples
        result = self.model.generate(input=pcm_samples(pcm), batch_size=1)
        return {"text": "".join(str(row.get("text") or "") for row in result), "complete": True}

    def warmup(self):
        self(b"\0\0" * 16000)
        self.warmed = True


class DualPassLane:
    """DualPassLane（离线单通道）：模型实例跨句段复用，原生调用完成前不卸载。"""

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-offline")
        self.model = None
        self.error = None
        self.enabled = False
        self.epoch = 0
        self.resource_footprint = None

    async def configure(self, enabled: bool, device: str, *, strict: bool = False):
        self.epoch += 1
        self.enabled = enabled

        def change():
            close = getattr(self.model, "close", None)
            if callable(close):
                close()
            self.model = None
            self.resource_footprint = None
            self.error = None
            if enabled:
                try:
                    with model_load_footprint() as footprint:
                        from ..model_process import ModelProcess
                        model = ModelProcess("offline", self.model_dir, device=device)
                    self.model = model
                    self.resource_footprint = model.resource_footprint or footprint
                except Exception as exc:
                    self.error = str(exc)
                    if strict:
                        raise

        from .worker import AsrWorkerEngine
        future = asyncio.get_running_loop().run_in_executor(self.executor, change)
        await AsrWorkerEngine._await_native(future)

    def status(self):
        loaded = self.model is not None and getattr(self.model, "alive", True)
        return {"status": "DISABLED" if not self.enabled else "READY" if loaded else "DEGRADED",
                "model": "BiCifParaformer（离线精修）", "model_loaded": loaded,
                "identity": getattr(self.model, "identity", None),  # identity（固定模型身份）。
                "model_warmed": bool(loaded and self.model.warmed),
                "device": self.model.device if self.model else None,
                "runtime": "PyTorch（推理框架）", "error": self.error or getattr(self.model, "error", None),
                "resource_status": getattr(self.model, "resource_status", None) if loaded else None,
                "resource_footprint": self.resource_footprint if loaded else None}

    async def infer(self, pcm: bytes):
        epoch = self.epoch
        if not self.enabled or self.model is None:
            return None
        started = time.perf_counter()

        def invoke():
            if epoch != self.epoch or self.model is None:
                return None
            return self.model(pcm)

        from .worker import AsrWorkerEngine
        result = await AsrWorkerEngine._await_native(
            asyncio.get_running_loop().run_in_executor(self.executor, invoke))
        if epoch != self.epoch or result is None:
            return None
        return {**result, "elapsed_ms": (time.perf_counter() - started) * 1000}

    def close(self):
        self.epoch += 1
        self.executor.shutdown(wait=True, cancel_futures=True)
        close = getattr(self.model, "close", None)
        if callable(close):
            close()
        self.model = None
        self.resource_footprint = None

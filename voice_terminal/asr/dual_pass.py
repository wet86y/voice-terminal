"""独立的热词增强离线通道；宿主提供声学边界，不刷新主流式模型。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
import tempfile

from ..resources import model_thread_budget, model_load_footprint


class OfflineParaformer:
    def __init__(self, model_dir: Path, device: str):
        self.model_dir = model_dir
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

    def __call__(self, pcm: bytes, *, hotwords: list[str] | None = None, purpose: str = "conversation") -> dict:
        from ..audio.allocation import pcm_samples
        from .text import normalize_asr_text
        if purpose not in {"conversation", "reference"}:
            raise ValueError("purpose（调用用途）无效")
        targets = hotwords or [] if purpose == "conversation" else []
        if not isinstance(targets, list) or len(targets) > 5000 or any(
                not isinstance(word, str) or not word.strip() or len(word) > 96
                or any(char in word for char in "\r\n\x00") for word in targets):
            raise ValueError("hotwords（原生目标热词）无效")
        filename = None
        try:
            if targets:
                from ..local_layout import local_root
                directory = local_root() / "tmp" / "hotword-injection"
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", dir=directory, delete=False) as file:
                    file.write("\n".join(targets))
                    filename = file.name
            # AutoModel（框架入口）会保留 kwargs（调用参数）；每次必须显式清空旧热词。
            result = self.model.generate(input=pcm_samples(pcm), batch_size=1, hotword=filename)
            return {"text": normalize_asr_text("".join(str(row.get("text") or "") for row in result)).strip(),
                    "complete": True, "route": "offline"}
        finally:
            if filename:
                Path(filename).unlink(missing_ok=True)

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
        self._ready = asyncio.Event()  # ready（切换完成屏障；加载期间的合法闭段等待就绪）。
        self._ready.set()

    async def configure(self, enabled: bool, device: str, *, strict: bool = False,
                        ignore_host_fallback: bool = False):  # ignore_host_fallback（切换时忽略宿主强制中央处理器回退）。
        self.epoch += 1
        self.enabled = enabled
        self._ready.clear()

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
                        model = ModelProcess("offline", self.model_dir, device=device,
                                             require_cuda=str(device).startswith("cuda"),
                                             ignore_host_fallback=ignore_host_fallback)
                    self.model = model
                    self.resource_footprint = model.resource_footprint or footprint
                except Exception as exc:
                    self.error = str(exc)
                    if strict:
                        raise

        from .worker import AsrWorkerEngine
        future = asyncio.get_running_loop().run_in_executor(self.executor, change)
        try:
            await AsrWorkerEngine._await_native(future)
        finally:
            self._ready.set()

    def status(self):
        loaded = self.model is not None and getattr(self.model, "alive", True)
        return {"status": "DISABLED" if not self.enabled else "READY" if loaded else "DEGRADED",
                "model": "ContextualParaformer（热词增强离线精修）", "model_loaded": loaded,
                "identity": getattr(self.model, "identity", None),  # identity（固定模型身份）。
                "model_warmed": bool(loaded and self.model.warmed),
                "device": self.model.device if self.model else None,
                "runtime": "PyTorch（推理框架）", "error": self.error or getattr(self.model, "error", None),
                "resource_status": getattr(self.model, "resource_status", None) if loaded else None,
                "resource_footprint": self.resource_footprint if loaded else None}

    async def infer(self, pcm: bytes, *, hotwords: list[str] | None = None, purpose: str = "conversation"):
        epoch = self.epoch
        await self._ready.wait()
        if epoch != self.epoch:
            return None
        if not self.enabled or self.model is None:
            return None
        started = time.perf_counter()

        def invoke():
            if epoch != self.epoch or self.model is None:
                return None
            return self.model(pcm, hotwords=hotwords, purpose=purpose)

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

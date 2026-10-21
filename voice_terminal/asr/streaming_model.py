"""Streaming Paraformer（流式语音识别）生命周期，隔离于 ASR Worker（识别工作进程）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StreamingParaformer:
    """在可回收模型子进程中持有流式模型和各发言轮次缓存。"""

    def __init__(self, model_dir: Path, *, device: str, fallback_device: str = "cpu",
                 chunk_size: tuple[int, int, int] = (0, 8, 4),  # chunk_size（流式模型分块参数）。
                 sample_rate: int = 16000,  # sample_rate（音频采样率，赫兹）。
                 encoder_chunk_look_back: int = 4,  # encoder_chunk_look_back（编码器回看分块数）。
                 decoder_chunk_look_back: int = 1) -> None:  # decoder_chunk_look_back（解码器回看分块数）。
        self.model_dir = Path(model_dir)
        self.requested_device = device
        self.device = "cpu"
        self.fallback_device = fallback_device
        self.chunk_size = tuple(chunk_size)
        self.sample_rate = sample_rate
        self.encoder_chunk_look_back = encoder_chunk_look_back
        self.decoder_chunk_look_back = decoder_chunk_look_back
        self.runtime = "PyTorch（推理框架）"
        self.model_file = str(self.model_dir / "model.pt")
        self.warmed = False
        self.load_error: str | None = None
        self.model: Any = None
        self._np: Any = None
        self._cache_by_turn: dict[str, dict[str, Any]] = {}  # _cache_by_turn（按发言轮次隔离的流式缓存）。
        self._load()

    def _load(self) -> None:
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f"Paraformer（流式识别）模型目录不存在：{self.model_dir}")
        from ..compute import loaded_funasr_device, startup_device
        from ..tensor_runtime import activate_tensor_runtime

        initial = startup_device(self.requested_device, "paraformer")
        activate_tensor_runtime(initial)
        try:
            import numpy as np  # type: ignore
            from .model_loader import auto_model_class
            model_class = auto_model_class(self.model_dir)
            from ..resources import model_thread_budget
        except ImportError as exc:
            raise RuntimeError("缺少 NumPy（数值计算库）或 FunASR（语音框架），请先运行 scripts\\setup.ps1（环境安装脚本）。") from exc
        self._np = np
        fallback = self.fallback_device if self.fallback_device in {"cpu", "cuda"} else "cpu"
        candidates = [initial]
        if initial == "cuda":
            if fallback != initial:
                candidates.append(fallback)
            if "cpu" not in candidates:
                candidates.append("cpu")
        last_error: Exception | None = None
        for candidate in candidates:
            model = None
            try:
                model = model_class(
                    model=str(self.model_dir), device=candidate, disable_update=True,
                    disable_pbar=True, ncpu=model_thread_budget(),
                )
                actual = loaded_funasr_device(model, candidate)
                self.model = model
                self.device = "cuda" if str(actual).startswith("cuda") else "cpu"
                self._warmup()
                if last_error is not None:
                    self.load_error = f"CUDA（图形处理器）加载失败，已回退到 {self.device}：{type(last_error).__name__}: {last_error}"
                elif candidate == "cuda" and self.device == "cpu":
                    self.load_error = "FunASR（语音模型加载器）未使用请求的 CUDA（图形处理器），实际运行于 CPU（中央处理器）。"
                logger.info("asr.streaming_model.ready model_dir=%s device=%s", self.model_dir, self.device)
                return
            except Exception as exc:
                last_error = exc
                close = getattr(model, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                self.model = None
                self.warmed = False
                if candidate == "cuda":
                    try:
                        import torch  # type: ignore
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
        raise RuntimeError(str(last_error) if last_error else "Paraformer（流式识别）模型加载失败") from last_error

    def _infer(self, turn_id: str, pcm: bytes, is_final: bool) -> str:
        if self.model is None:
            raise RuntimeError("Paraformer（流式识别）模型尚未加载")
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn_id（发言轮次编号）必须是非空字符串")
        if len(pcm) % 2:
            pcm = pcm[:-1]
        audio = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        cache = self._cache_by_turn.setdefault(turn_id, {})
        result = self.model.generate(
            input=audio, cache=cache, is_final=is_final, chunk_size=list(self.chunk_size),
            encoder_chunk_look_back=self.encoder_chunk_look_back,
            decoder_chunk_look_back=self.decoder_chunk_look_back,
        )
        if is_final:
            self._cache_by_turn.pop(turn_id, None)
        if isinstance(result, list) and result and isinstance(result[0], dict):
            text = result[0].get("text")
            return text if isinstance(text, str) else ""
        return ""

    def _warmup(self) -> None:
        cache: dict[str, Any] = {}
        for final in (False, False, True):
            middle_chunks = self.chunk_size[1] if len(self.chunk_size) > 1 else 8
            audio = self._np.zeros(0 if final else int(self.sample_rate * middle_chunks * 60 / 1000),
                                   dtype=self._np.float32)
            self.model.generate(
                input=audio, cache=cache, is_final=final, chunk_size=list(self.chunk_size),
                encoder_chunk_look_back=self.encoder_chunk_look_back,
                decoder_chunk_look_back=self.decoder_chunk_look_back,
            )
        self.warmed = True

    def warmup(self) -> None:
        # ModelProcess（模型子进程） calls warmup after loading within the
        # measured footprint scope. _load performs the required real inference.
        return None

    def infer(self, turn_id: str, pcm: bytes, is_final: bool) -> str:
        return self._infer(turn_id, pcm, is_final)

    def clear_turn(self, turn_id: str) -> None:
        self._cache_by_turn.pop(turn_id, None)

    def close(self) -> None:
        self._cache_by_turn.clear()
        model, self.model = self.model, None
        self.warmed = False
        close = getattr(model, "close", None)
        if callable(close):
            close()

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class VadError(RuntimeError):
    pass


@dataclass(frozen=True)
class VadActivity:
    is_speech: bool
    speech_started: bool
    speech_ended: bool
    speech_end_sample: int | None = None
    speech_start_sample: int | None = None


class FsmnVadDetector:
    """Thin adapter around FunASR's streaming FSMN-VAD model."""

    def __init__(self, *, model_dir: Path, device: str = "cpu", fallback_device: str = "cpu",
                 sample_rate: int = 16000, end_silence_ms: int | None = None) -> None:
        self.model_dir = model_dir
        self.requested_device = device
        self.fallback_device = fallback_device
        self.device = "cpu"
        self.load_error: str | None = None
        self.sample_rate = sample_rate
        self.end_silence_ms = end_silence_ms
        self._cache: dict[str, Any] = {}
        self._active = False
        # AudioCapture emits 20 ms blocks, while the FSMN-VAD frontend needs
        # a larger streaming window to produce aligned feature frames.  Keep
        # the model window independent from the capture block size and let the
        # FunASR cache carry the remainder between calls.
        self._model_chunk_ms = 200
        self._feed_count = 0
        self._samples = 0
        from ..resources import ResourcePolicy, registry
        policy = ResourcePolicy.load()
        registry.observe(self, "vad_pending_audio", lambda value: int(
            getattr(value._cache.get("onnx_state", {}).get("pending"), "nbytes", 0)))
        if device == "cpu" and policy.vad_backend == "onnx":
            try:
                import numpy as np
                from .onnx_backend import OnnxStreamingModel
                self._np = np
                self._model = OnnxStreamingModel(model_dir, threads=policy.native_threads.get("voice", 4))
                self.backend = "onnx"
                return
            except Exception as exc:
                logger.warning("轻量语音检测加载失败，保留原后端：%s", exc)
                self.load_error = f"轻量语音检测加载失败：{type(exc).__name__}: {exc}"
        self.backend = "funasr"
        try:
            import numpy as np  # type: ignore
            from funasr import AutoModel  # type: ignore
        except ImportError as exc:
            raise VadError("缺少 numpy/funasr，请先运行 scripts\\setup.ps1。") from exc

        self._np = np
        from ..compute import device_candidates
        last_error: Exception | None = None
        for candidate in device_candidates(device, "voice_vad", fallback_device):
            try:
                self._model = AutoModel(
                    model=str(model_dir),
                    device=candidate,
                    disable_update=True,
                    disable_pbar=True,
                )
                self.device = candidate
                self.load_error = self.load_error or (
                    f"CUDA（图形处理器）加载失败，已回退到 {candidate}：{type(last_error).__name__}: {last_error}"
                    if last_error is not None else None
                )
                return
            except Exception as exc:
                last_error = exc
                if candidate == "cuda":
                    try:
                        import torch  # type: ignore
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
        raise VadError(f"FSMN-VAD 模型初始化失败：{last_error}") from last_error

    def reset(self) -> None:
        self._cache = {}
        self._active = False
        self._feed_count = 0
        self._samples = 0

    @staticmethod
    def _segments(result: Any) -> list[Any]:
        if not isinstance(result, list):
            return []
        segments: list[Any] = []
        for item in result:
            if isinstance(item, dict):
                value = item.get("value")
                if isinstance(value, list):
                    segments.extend(value)
        return segments

    def feed(self, pcm: bytes, *, is_final: bool = False) -> VadActivity:
        if len(pcm) % 2:
            pcm = pcm[:-1]
        audio = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        self._feed_count += 1
        self._samples = getattr(self, "_samples", 0) + len(pcm) // 2
        input_rms = float(self._np.sqrt(self._np.mean(audio * audio))) if audio.size else 0.0
        input_peak = float(self._np.max(self._np.abs(audio))) if audio.size else 0.0
        try:
            result = self._model.generate(
                input=audio,
                cache=self._cache,
                is_final=is_final,
                chunk_size=self._model_chunk_ms,
                is_streaming_input=True,
                **({"max_end_silence_time": self.end_silence_ms}
                   if getattr(self, "end_silence_ms", None) is not None else {}),
            )
        except Exception as exc:
            raise VadError(f"FSMN-VAD 推理失败：{exc}") from exc

        segments = self._segments(result)
        has_open_segment = any(
            isinstance(segment, (list, tuple))
            and len(segment) >= 2
            and segment[1] in (-1, None)
            for segment in segments
        )
        has_closed_segment = any(
            isinstance(segment, (list, tuple))
            and len(segment) >= 2
            and segment[1] not in (-1, None)
            for segment in segments
        )
        was_active = self._active
        # In FunASR streaming mode an empty ``value`` means "no new VAD
        # event".  It must not clear the active speech state.  A start event
        # opens the segment; an end event or explicit finalization closes it.
        speech_started = (has_open_segment or has_closed_segment) and not was_active
        speech_ended = (
            (has_closed_segment or is_final)
            and not has_open_segment
            and (was_active or has_closed_segment)
        )
        if has_open_segment and not has_closed_segment:
            self._active = True
        elif has_closed_segment or is_final:
            self._active = False
        open_segment = self._active
        if self._feed_count == 1 or self._feed_count % 50 == 0 or segments or speech_started or speech_ended or is_final:
            logger.info(
                "vad.progress feed=%d pcm_bytes=%d rms=%.5f peak=%.5f final=%s segments=%s open=%s closed=%s started=%s ended=%s cache_keys=%s",
                self._feed_count,
                len(pcm),
                input_rms,
                input_peak,
                is_final,
                segments,
                open_segment,
                has_closed_segment,
                speech_started,
                speech_ended,
                sorted(self._cache.keys()),
            )
        return VadActivity(
            is_speech=open_segment,
            speech_started=speech_started,
            speech_ended=speech_ended,
            speech_end_sample=max((int(s[1] * getattr(self, "sample_rate", 16000) / 1000) for s in segments
                                   if isinstance(s, (list, tuple)) and len(s) >= 2 and isinstance(s[1], (int, float)) and s[1] >= 0), default=None),
            speech_start_sample=min((int(s[0] * getattr(self, "sample_rate", 16000) / 1000) for s in segments
                                     if isinstance(s, (list, tuple)) and len(s) >= 2 and isinstance(s[0], (int, float)) and s[0] >= 0), default=None),
        )

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

from .features import (
    CAMPLUS_FRONTEND_VERSION,
    FRONTEND_VERSION,
    InsufficientAudioError,
    pcm_to_campplus_fbank,
    pcm_to_fbank,
    probe_pcm,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FRONTEND_VERSION",
    "InsufficientAudioError",
    "SpeakerModelError",
    "CamPlusPlusOnnxEngine",
    "WeSpeakerOnnxEngine",
    "pcm_to_fbank",
]


class SpeakerModelError(RuntimeError):
    pass


class WeSpeakerOnnxEngine:
    """Optional ONNX embedding engine; importing it never makes the runtime mandatory."""

    backend_name = "WeSpeaker"  # backend_name（后端名称）：用于报告加载异常。
    device_model_key = "wespeaker"  # device_model_key（计算设备配置键）：各声纹后端共用既有配置。

    def __init__(self, model_path: Path, *, sample_rate: int = 16000, model_version: str = "",
                 device: str = "cpu", fallback_device: str = "cpu") -> None:
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.model_version = model_version or model_path.stem
        self.requested_device = device
        self.fallback_device = fallback_device
        self.device = "cpu"
        self.load_error: str | None = None
        self.session: Any = None
        self.input_name = "feats"
        self.output_name = "embs"
        self.input_layout = "btf"
        self.feature_dim = 80
        self.embedding_dim = 0
        self.error: str | None = None
        self._probe: tuple[float, ...] | None = None
        from ..resources import ResourcePolicy
        self._managed_audio = ResourcePolicy.load().shared_audio

    @property
    def loaded(self) -> bool:
        return self.session is not None and self.error is None

    @property
    def frontend_version(self) -> str:
        return FRONTEND_VERSION

    def load(self) -> None:
        if np is None:
            self.error = "numpy unavailable"
            raise SpeakerModelError(self.error)
        if not self.model_path.is_file():
            self.error = f"model not found: {self.model_path}"
            raise SpeakerModelError(self.error)
        try:
            import onnxruntime as ort  # type: ignore
            from ..compute import device_candidates
            last_error: Exception | None = None
            session = None
            from ..startup_logging import startup_stage
            with startup_stage("声纹运行设备选择"):
                candidates = device_candidates(self.requested_device, self.device_model_key,
                                               self.fallback_device, onnx_only=True)
            for candidate in candidates:
                try:
                    if candidate == "cuda":
                        ort.preload_dlls()  # preload_dlls（预加载显卡运行库）：独立声纹进程也必须执行。
                    logger.info("声纹模型加载尝试：设备=%s，后端=ONNX Runtime（模型推理运行时）", candidate)
                    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if candidate == "cuda" else ["CPUExecutionProvider"]
                    # VOICE_SPEAKER_SESSION_MODE（声纹会话策略）：保留原策略供真实资源对照。
                    session_mode = os.environ.get("VOICE_SPEAKER_SESSION_MODE", "original")
                    options = ort.SessionOptions()
                    if session_mode == "bounded":
                        options.intra_op_num_threads = 1 if candidate == "cuda" else 2
                        options.inter_op_num_threads = 1
                        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
                    elif session_mode != "original":
                        raise SpeakerModelError("声纹会话策略必须为 original（原策略）或 bounded（有限线程）。")
                    session = ort.InferenceSession(str(self.model_path), sess_options=options, providers=providers)
                    if candidate == "cuda" and "CUDAExecutionProvider" not in session.get_providers():
                        raise SpeakerModelError("ONNX Runtime did not activate CUDAExecutionProvider")
                    break
                except Exception as exc:
                    last_error = exc
                    session = None
            if session is None:
                raise SpeakerModelError(str(last_error or "ONNX Runtime could not create a session"))
            self.session = session
            self.device = "cuda" if "CUDAExecutionProvider" in session.get_providers() else "cpu"
            self.load_error = (
                f"CUDA（图形处理器）加载失败，已回退到 {self.device}：{type(last_error).__name__}: {last_error}"
                if last_error is not None else None
            )
            inputs = self.session.get_inputs()
            if not inputs:
                raise SpeakerModelError("ONNX model has no inputs")
            self.input_name = str(inputs[0].name)
            shape = inputs[0].shape
            if len(shape) >= 3:
                # Accepted layouts are B x T x F (this export: ['B', 'T', 80])
                # and B x F x T.  The time axis is dynamic in both, and the
                # feature axis is what identifies which is which.
                if shape[-1] == self.feature_dim:
                    self.input_layout = "btf"
                elif len(shape) >= 3 and shape[1] == self.feature_dim:
                    self.input_layout = "bft"
                else:
                    raise SpeakerModelError(
                        f"unsupported feature axis in ONNX input shape {shape}; "
                        f"expected {self.feature_dim} mel bins"
                    )
            outputs = self.session.get_outputs()
            if outputs:
                self.output_name = str(outputs[0].name)
                out_shape = outputs[0].shape
                if len(out_shape) == 2 and isinstance(out_shape[1], int) and out_shape[1] > 0:
                    self.embedding_dim = int(out_shape[1])
            self.error = None
            self._probe = None
        except SpeakerModelError:
            self.session = None
            raise
        except Exception as exc:
            self.session = None
            self.error = f"{self.backend_name} load failed: {type(exc).__name__}: {exc}"
            raise SpeakerModelError(self.error) from exc

    def _run(self, features: Any) -> tuple[float, ...]:
        if not self.loaded:
            raise SpeakerModelError(self.error or "model not loaded")
        model_features = features.T[None, :, :] if self.input_layout == "bft" else features[None, :, :]
        model_features = np.ascontiguousarray(model_features, dtype=np.float32)
        result = self.session.run([self.output_name], {self.input_name: model_features})
        if not result:
            raise SpeakerModelError("ONNX model returned no embedding")
        embedding = np.asarray(result[0], dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(embedding))
        if not embedding.size or not np.isfinite(embedding).all() or not np.isfinite(norm) or norm <= 1e-8:
            # The graph pools with an unbiased variance, so a too-short time
            # axis divides by zero and yields NaN.  Surface that as an explicit
            # error instead of letting a NaN score reach the decision layer.
            raise SpeakerModelError("ONNX model returned an invalid embedding")
        return tuple(float(x) for x in (embedding / norm))

    def embed_pcm(self, pcm: bytes) -> tuple[float, ...]:
        from ..resources import registry
        from ..audio.allocation import conversion_scope
        with registry.lease("speaker_native_pcm", pcm, release_on="native_complete"), conversion_scope(pcm, self.sample_rate, enabled=self._managed_audio):
            return self._embed_owned_pcm(pcm)

    def _features_for_pcm(self, pcm: bytes) -> Any:
        """将 PCM（脉冲编码调制音频）转为当前模型训练时使用的声学特征格式。"""
        return pcm_to_fbank(pcm, sample_rate=self.sample_rate)

    def _embed_owned_pcm(self, pcm: bytes) -> tuple[float, ...]:
        if not self.loaded:
            raise SpeakerModelError(self.error or "model not loaded")
        features = self._features_for_pcm(pcm)
        if features.shape[0] < 1:
            raise InsufficientAudioError("audio produced no feature frames")
        return self._run(features)

    def probe_embedding(self) -> tuple[float, ...]:
        """Embedding of a fixed synthetic signal, used as a frontend fingerprint.

        Comparing this vector at load time catches *any* change to the feature
        pipeline or the model weights, including numeric-only fixes that leave
        the parameter list identical.
        """
        if self._probe is None:
            self._probe = self._run(self._features_for_pcm(probe_pcm(self.sample_rate)))
        return self._probe

    @staticmethod
    def similarity(first: tuple[float, ...], second: tuple[float, ...]) -> float:
        if np is None or not first or not second or len(first) != len(second):
            return -1.0
        a = np.asarray(first, dtype=np.float32)
        b = np.asarray(second, dtype=np.float32)
        if not np.isfinite(a).all() or not np.isfinite(b).all():
            return -1.0
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom <= 1e-8:
            return -1.0
        cosine = float(np.dot(a, b) / denom)
        # WeSpeaker verification thresholds are calibrated against cosine
        # similarity itself (identical vectors=1, orthogonal=0, opposite=-1).
        # Do not shift this raw score into [0, 1], which would make the 0.50
        # product baseline equivalent to accepting every non-negative pair.
        return max(-1.0, min(1.0, cosine))


class CamPlusPlusOnnxEngine(WeSpeakerOnnxEngine):
    """CAM++ ONNX（开放神经网络交换格式）声纹引擎，共享归一化嵌入接口。

    It overrides the frontend identity and extraction only. ModelScope's
    200k-speaker checkpoint（20万说话人训练模型）uses the 3D-Speaker Kaldi
    FBank（梅尔滤波器组）Povey window（Povey窗）frontend（前端特征提取）；the
    existing WeSpeaker backend keeps its Hamming window（汉明窗）frontend.
    """

    backend_name = "CAM++"

    @property
    def frontend_version(self) -> str:
        return CAMPLUS_FRONTEND_VERSION

    def _features_for_pcm(self, pcm: bytes) -> Any:
        return pcm_to_campplus_fbank(pcm, sample_rate=self.sample_rate)

"""CAM++ 原生 FunASR（语音处理框架）引擎，离线权重与独立进程生命周期。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .engine import SpeakerModelError, WeSpeakerOnnxEngine
from .features import CAMPLUS_FRONTEND_VERSION, InsufficientAudioError, probe_pcm

WEIGHT_SHA256 = "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8"


class SpeakerEngine(Protocol):
    """引擎接口：加载、提取向量、特征指纹和实际设备。"""
    model_path: Path
    model_version: str
    device: str
    loaded: bool
    frontend_version: str

    def load(self) -> None: ...
    def embed_pcm(self, pcm: bytes) -> tuple[float, ...]: ...
    def probe_embedding(self) -> tuple[float, ...]: ...
    def close(self) -> None: ...
    def similarity(self, left, right) -> float: ...


class CamPlusPlusFunAsrEngine:
    backend_name = "CAM++"
    runtime = "FunASR / PyTorch（原生语音模型运行时）"
    device_model_key = "campplus"  # 计算设备键。
    embedding_dim = 192  # 声纹向量维度。
    feature_dim = 80  # 梅尔特征维度。

    def __init__(self, model_path: Path, *, sample_rate: int = 16000,
                 model_version: str = "", device: str = "cpu", fallback_device: str = "cpu") -> None:
        self.model_path = Path(model_path)
        self.sample_rate = sample_rate
        self.model_version = model_version or "campplus-zh-cn-common:v1.0.0:funasr-native-v1"
        self.requested_device, self.fallback_device = device, fallback_device
        self.device = "cpu"
        self.load_error: str | None = None
        self.error: str | None = None
        self.session: Any = None
        self._probe: tuple[float, ...] | None = None

    @property
    def loaded(self) -> bool:
        return self.session is not None and self.error is None

    @property
    def frontend_version(self) -> str:
        return CAMPLUS_FRONTEND_VERSION

    def load(self) -> None:
        if self.loaded:
            return
        if self.sample_rate != 16000 or not self.model_path.is_file():
            raise SpeakerModelError("CAM++ 需要本地官方权重和 16000 Hz（赫兹）音频")
        # 权重完整摘要由分发器维护；实际加载继续严格检查权重格式与参数键。
        from ..compute import device_candidates, loaded_funasr_device
        candidates = device_candidates(self.requested_device, self.device_model_key, self.fallback_device)
        from ..tensor_runtime import activate_tensor_runtime
        activate_tensor_runtime(candidates[0])
        import torch
        from ..asr.model_loader import auto_model_class
        AutoModel = auto_model_class(self.model_path.parent, family="campplus")
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                model = AutoModel(model="CAMPPlus", model_conf={"feat_dim": 80, "embedding_size": 192},
                                  init_param=str(self.model_path), ignore_init_mismatch=False,
                                  device=candidate, ncpu=2, batch_size=1,
                                  disable_update=True, disable_pbar=True, log_level="ERROR")
                # 官方加载器允许缺键，因此额外严格校验，不能以随机初始化参数报告就绪。
                state = torch.load(self.model_path, map_location="cpu", weights_only=True, mmap=True)
                model.model.load_state_dict(state, strict=True)
                del state
                self.session = model
                self.device = loaded_funasr_device(model, candidate)
                if candidate == "cuda" and self.device != "cuda":
                    last_error = SpeakerModelError("CUDA（显卡计算）不可用，FunASR已回退到CPU（中央处理器）")
                self.error = None
                self._probe = None
                self.probe_embedding()  # 原生算子和显卡在开放接口前预热。
                self.load_error = str(last_error)[:300] if last_error else None
                return
            except Exception as exc:
                last_error = exc
                self.session = None
        self.error = f"CAM++ 原生加载失败：{last_error}"
        raise SpeakerModelError(self.error)

    def embed_pcm(self, pcm: bytes) -> tuple[float, ...]:
        if not self.loaded:
            raise SpeakerModelError(self.error or "CAM++ 尚未加载")
        if len(pcm) % 2 or len(pcm) < 720 * 2:
            raise InsufficientAudioError("CAM++ 至少需要45毫秒、完整16位音频")
        from ..resources import registry
        from ..audio.allocation import conversion_scope, pcm_samples
        with registry.lease("speaker_native_pcm", pcm, release_on="native_complete"), conversion_scope(pcm, 16000):
            audio = pcm_samples(pcm, sample_rate=16000, precision="float32", amplitude="normalized")
            result = self.session.generate(input=audio, batch_size=1, fs=16000, disable_pbar=True)
            vector = result[0]["spk_embedding"].detach().float().cpu().numpy().reshape(-1)
            norm = float(np.linalg.norm(vector))
            if vector.size != 192 or not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 1e-8:
                raise SpeakerModelError("CAM++ 返回无效声纹向量")
            return tuple(float(x) for x in vector / norm)

    def probe_embedding(self) -> tuple[float, ...]:
        if self._probe is None:
            self._probe = self.embed_pcm(probe_pcm(self.sample_rate))
        return self._probe

    similarity = staticmethod(WeSpeakerOnnxEngine.similarity)

    def close(self) -> None:
        """调用方须在所有原生计算完成后释放模型引用。"""
        self.session = None
        self._probe = None

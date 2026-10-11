"""Speaker model factory（声纹模型工厂）：select an optional embedding backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .engine import CamPlusPlusOnnxEngine, SpeakerModelError, WeSpeakerOnnxEngine

# WESPEAKER_MODEL_ID（WeSpeaker模型编号）：保持既有声纹模型身份。
WESPEAKER_MODEL_ID = "wespeaker-cnceleb-resnet34"
# CAMPPLUS_MODEL_ID（CAM++模型编号）：可选的ModelScope中文通用模型身份。
CAMPPLUS_MODEL_ID = "campplus-zh-cn-common"


def create_speaker_engine(
    model_id: str,
    model_path: str | Path,
    *,
    sample_rate: int = 16000,
    model_version: str = "",
    device: str = "cpu",
    fallback_device: str = "cpu",
) -> WeSpeakerOnnxEngine | CamPlusPlusOnnxEngine:
    """按配置的模型编号与 ONNX（开放神经网络交换格式）路径创建声纹引擎。

    Both implementations expose the same ``load``/``embed_pcm``/``probe_embedding``
    contract（接口约定：加载、提取嵌入、计算探测向量）。

    参数：
        model_id（模型编号）：稳定的配置和声纹档案身份。
        model_path（模型文件路径）：ONNX 模型文件位置。
        sample_rate（采样率）：输入 PCM（脉冲编码调制）音频采样率。
        model_version（模型版本）：用于区分模型导出版本的标识。
        device（计算设备）：期望的 CPU（中央处理器）或 CUDA（显卡计算）后端。
        fallback_device（回退设备）：期望后端不可用时使用的设备。

    返回：实现上述统一接口的 WeSpeaker 或 CAM++ 声纹引擎。
    """
    common: dict[str, Any] = {
        "model_path": Path(model_path),
        "sample_rate": sample_rate,
        "model_version": model_version,
        "device": device,
        "fallback_device": fallback_device,
    }
    if model_id == WESPEAKER_MODEL_ID:
        return WeSpeakerOnnxEngine(**common)
    if model_id == CAMPPLUS_MODEL_ID:
        return CamPlusPlusOnnxEngine(**common)
    raise SpeakerModelError(f"unsupported speaker model_id（声纹模型编号）: {model_id}")


__all__ = [
    "CAMPPLUS_MODEL_ID",
    "WESPEAKER_MODEL_ID",
    "create_speaker_engine",
]

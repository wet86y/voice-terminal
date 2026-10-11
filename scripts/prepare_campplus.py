"""Download and export the official ModelScope CAM++ speaker checkpoint.

Usage (from the workspace root)::

    python scripts/prepare_campplus.py

The script downloads only the CAM++ checkpoint, fetches the two upstream
3D-Speaker architecture sources at a pinned commit into ``.local/cache``, then
exports and checks a dynamic-time-axis ONNX model against PyTorch on 0.75/1/2/3
second synthetic signals. It does not install or update Python packages.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from urllib.request import Request, urlopen

MODEL_ID = "iic/speech_campplus_sv_zh-cn_16k-common"
MODEL_REVISION = "v1.0.0"
WEIGHT_FILE = "campplus_cn_common.bin"
WEIGHT_SIZE_BYTES = 28_036_335
WEIGHT_SHA256 = "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8"
THREE_D_SPEAKER_COMMIT = "065629c313eaf1a01c65c640c46d77e61e9607b4"
THREE_D_SPEAKER_FILES = (
    "speakerlab/models/campplus/DTDNN.py",
    "speakerlab/models/campplus/layers.py",
)
FEATURE_TEST_SECONDS = (0.75, 1.0, 2.0, 3.0)


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_official_sources(root: Path) -> Path:
    source_root = root / ".local" / "cache" / f"3d-speaker-campplus-{THREE_D_SPEAKER_COMMIT}"
    package_dirs = (
        source_root / "speakerlab",
        source_root / "speakerlab" / "models",
        source_root / "speakerlab" / "models" / "campplus",
    )
    for directory in package_dirs:
        directory.mkdir(parents=True, exist_ok=True)
        init_file = directory / "__init__.py"
        if not init_file.exists():
            init_file.write_text('"""Pinned 3D-Speaker source package."""\n', encoding="utf-8")
    for relative in THREE_D_SPEAKER_FILES:
        target = source_root / relative
        if target.is_file() and target.stat().st_size:
            continue
        url = f"https://raw.githubusercontent.com/modelscope/3D-Speaker/{THREE_D_SPEAKER_COMMIT}/{relative}"
        request = Request(url, headers={"User-Agent": "voice-terminal-campplus-preparer"})
        with urlopen(request, timeout=60) as response:
            content = response.read()
        if b"Copyright 3D-Speaker" not in content or b"Apache License, Version 2.0" not in content:
            raise RuntimeError(f"upstream source header/license check failed: {relative}")
        target.write_bytes(content)
    license_path = source_root / "LICENSE"
    if not license_path.exists():
        url = f"https://raw.githubusercontent.com/modelscope/3D-Speaker/{THREE_D_SPEAKER_COMMIT}/LICENSE"
        request = Request(url, headers={"User-Agent": "voice-terminal-campplus-preparer"})
        with urlopen(request, timeout=60) as response:
            license_path.write_bytes(response.read())
    return source_root


def _make_model(weight_path: Path, source_root: Path):
    import torch

    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from speakerlab.models.campplus.DTDNN import CAMPPlus  # type: ignore[import-not-found]

    model = CAMPPlus(feat_dim=80, embedding_size=192)
    state: Any = torch.load(weight_path, map_location="cpu", weights_only=True)
    if not isinstance(state, dict) or not state:
        raise RuntimeError("checkpoint is not a non-empty CAM++ state dictionary")
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


def _install_onnx_pooling_compatibility():
    """Preserve PyTorch ceil-pool averages in ONNX at partial final segments.

    The upstream CAMLayer uses avg_pool1d(..., ceil_mode=True). PyTorch
    excludes the ceil-only tail padding, but ONNX Runtime's exported
    AveragePool with count_include_pad=True includes it. Setting the explicit
    flag to False leaves PyTorch outputs unchanged and records the intended
    boundary behavior in the graph.
    """
    from torch.nn import functional as F
    from speakerlab.models.campplus.layers import CAMLayer  # type: ignore[import-not-found]

    original = CAMLayer.seg_pooling

    def seg_pooling(self, x, seg_len=100, stype="avg"):
        if stype == "avg":
            seg = F.avg_pool1d(
                x, kernel_size=seg_len, stride=seg_len, ceil_mode=True, count_include_pad=False
            )
        elif stype == "max":
            seg = F.max_pool1d(x, kernel_size=seg_len, stride=seg_len, ceil_mode=True)
        else:
            raise ValueError("Wrong segment pooling type.")
        shape = seg.shape
        seg = seg.unsqueeze(-1).expand(*shape, seg_len).reshape(*shape[:-1], -1)
        return seg[..., :x.shape[-1]]

    CAMLayer.seg_pooling = seg_pooling
    return CAMLayer, original, seg_pooling


def _download_checkpoint(model_dir: Path) -> Path:
    try:
        from modelscope.hub.snapshot_download import snapshot_download  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("ModelScope is required for checkpoint download; install nothing automatically") from exc
    model_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=str(model_dir),
        allow_patterns=[WEIGHT_FILE],
    )
    weight_path = model_dir / WEIGHT_FILE
    if not weight_path.is_file():
        raise RuntimeError(f"ModelScope download did not create {weight_path}")
    actual_size = weight_path.stat().st_size
    actual_sha = sha256_file(weight_path)
    if actual_size != WEIGHT_SIZE_BYTES or actual_sha != WEIGHT_SHA256:
        raise RuntimeError(
            f"checkpoint integrity mismatch: size={actual_size}, sha256={actual_sha}; "
            f"expected size={WEIGHT_SIZE_BYTES}, sha256={WEIGHT_SHA256}"
        )
    return weight_path


def _make_probe_pcm(seconds: float, sample_rate: int = 16000) -> bytes:
    import numpy as np

    count = int(round(seconds * sample_rate))
    t = np.arange(count, dtype=np.float64) / sample_rate
    sweep = 180.0 + 1600.0 * t / max(t[-1], 1e-9)
    signal = 0.30 * np.sin(2 * np.pi * sweep * t)
    signal += 0.17 * np.sin(2 * np.pi * 440.0 * t)
    signal += 0.09 * np.sin(2 * np.pi * 1730.0 * t)
    signal += 0.01 * np.random.default_rng(0xCA770).standard_normal(count)
    peak = max(float(np.abs(signal).max()), 1e-9)
    return np.clip(signal * (0.5 / peak) * 32767.0, -32768.0, 32767.0).astype("<i2").tobytes()


def _direct_official_fbank(pcm: bytes):
    import numpy as np
    import torch
    import torchaudio.compliance.kaldi as kaldi

    waveform = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0).unsqueeze(0)
    with torch.no_grad():
        feat = kaldi.fbank(waveform, num_mel_bins=80, sample_frequency=16000, dither=0.0)
        return (feat - feat.mean(dim=0, keepdim=True)).cpu().numpy()


def _verify_and_export(model, model_dir: Path, root: Path) -> dict[str, Any]:
    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch

    sys.path.insert(0, str(root))
    from voice_terminal.speaker.features import pcm_to_campplus_fbank  # noqa: E402

    onnx_path = model_dir / "campplus.onnx"
    handle = tempfile.NamedTemporaryFile(prefix="campplus-", suffix=".onnx", dir=model_dir, delete=False)
    temp_path = Path(handle.name)
    handle.close()
    patched_layer, original_pooling, export_pooling = _install_onnx_pooling_compatibility()
    try:
        dummy = torch.randn(1, 345, 80, dtype=torch.float32)
        # Matches 3D-Speaker's official export helper: opset 11, input feature
        # (batch,time,80), output embedding, dynamic batch and input frame axes.
        torch.onnx.export(
            model,
            dummy,
            str(temp_path),
            export_params=True,
            opset_version=11,
            do_constant_folding=True,
            input_names=["feature"],
            output_names=["embedding"],
            dynamic_axes={
                "feature": {0: "batch_size", 1: "frame_num"},
                "embedding": {0: "batch_size"},
            },
            # Use the legacy exporter that 3D-Speaker's helper relied on;
            # PyTorch 2.11 otherwise selects dynamo and requires onnxscript.
            dynamo=False,
        )
        exported = onnx.load(str(temp_path))
        onnx.checker.check_model(exported)
        input_tensor = exported.graph.input[0]
        dims = input_tensor.type.tensor_type.shape.dim
        if len(dims) != 3 or dims[1].dim_param != "frame_num" or dims[2].dim_value != 80:
            raise RuntimeError("exported ONNX input must be dynamic [batch,frame_num,80]")
        output_dims = exported.graph.output[0].type.tensor_type.shape.dim
        if len(output_dims) != 2 or output_dims[1].dim_value != 192:
            raise RuntimeError("exported ONNX output must have 192 embedding dimensions")

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(temp_path), sess_options=options, providers=["CPUExecutionProvider"])
        if session.get_providers()[0] != "CPUExecutionProvider":
            raise RuntimeError("ONNX Runtime did not select CPUExecutionProvider")
        checks: list[dict[str, Any]] = []
        with torch.no_grad():
            for seconds in FEATURE_TEST_SECONDS:
                pcm = _make_probe_pcm(seconds)
                features = pcm_to_campplus_fbank(pcm)
                reference_features = _direct_official_fbank(pcm)
                if not np.array_equal(features.shape, reference_features.shape) or not np.allclose(
                    features, reference_features, rtol=0, atol=1e-6
                ):
                    raise RuntimeError(f"official frontend parity failed for {seconds:g}s")
                input_features = np.ascontiguousarray(features[None, :, :], dtype=np.float32)
                pytorch_embedding = model(torch.from_numpy(input_features)).cpu().numpy()
                patched_layer.seg_pooling = export_pooling
                try:
                    export_compatible_embedding = model(torch.from_numpy(input_features)).cpu().numpy()
                finally:
                    patched_layer.seg_pooling = original_pooling
                onnx_embedding = session.run(["embedding"], {"feature": input_features})[0]
                patch_max_abs = float(np.max(np.abs(pytorch_embedding - export_compatible_embedding)))
                max_abs = float(np.max(np.abs(pytorch_embedding - onnx_embedding)))
                cosine = float(np.dot(pytorch_embedding.reshape(-1), onnx_embedding.reshape(-1)) /
                               (np.linalg.norm(pytorch_embedding) * np.linalg.norm(onnx_embedding)))
                finite = bool(np.isfinite(onnx_embedding).all())
                if not finite or patch_max_abs > 1.0e-6 or max_abs > 1.0e-3 or cosine < 0.9999:
                    raise RuntimeError(
                        f"export parity failed for {seconds:g}s: patch_max_abs={patch_max_abs}, "
                        f"onnx_max_abs={max_abs}, cosine={cosine}, finite={finite}"
                    )
                checks.append({
                    "seconds": seconds,
                    "frames": int(features.shape[0]),
                    "feature_dim": int(features.shape[1]),
                    "embedding_dim": int(onnx_embedding.shape[-1]),
                    "frontend_max_abs_error": float(np.max(np.abs(features - reference_features))),
                    "export_compatibility_max_abs_error": patch_max_abs,
                    "embedding_max_abs_error": max_abs,
                    "embedding_cosine": cosine,
                    "finite": finite,
                })
        os.replace(temp_path, onnx_path)
        return {"checks": checks, "onnx_sha256": sha256_file(onnx_path), "onnx_size_bytes": onnx_path.stat().st_size}
    finally:
        patched_layer.seg_pooling = original_pooling
        temp_path.unlink(missing_ok=True)


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def prepare(model_dir: Path, *, download: bool = True) -> dict[str, Any]:
    root = workspace_root()
    model_dir = model_dir.resolve()
    weight_path = _download_checkpoint(model_dir) if download else model_dir / WEIGHT_FILE
    if not weight_path.is_file():
        raise RuntimeError(f"checkpoint not found: {weight_path}; rerun without --skip-download")
    actual_hash = sha256_file(weight_path)
    if actual_hash != WEIGHT_SHA256 or weight_path.stat().st_size != WEIGHT_SIZE_BYTES:
        raise RuntimeError(f"checkpoint failed pinned revision verification: {weight_path}")
    source_root = _download_official_sources(root)
    model = _make_model(weight_path, source_root)
    exported = _verify_and_export(model, model_dir, root)
    result = {
        "schema": 1,
        "fields_zh": {
            "schema": "清单结构版本",
            "model_id": "ModelScope模型编号",
            "model_id_note_zh": "模型编号字段的中文说明",
            "revision": "固定模型修订版",
            "revision_note_zh": "修订版字段的中文说明",
            "weight_file": "PyTorch权重文件名",
            "weight_size_bytes": "权重文件大小（字节）",
            "weight_sha256": "权重文件SHA-256校验摘要",
            "onnx_file": "导出的ONNX模型文件名",
            "onnx_size_bytes": "ONNX文件大小（字节）",
            "onnx_sha256": "ONNX文件SHA-256校验摘要",
            "onnx_input": "ONNX模型输入名称和维度",
            "onnx_output": "ONNX模型输出名称和维度",
            "frontend": "模型使用的声学特征前端配置",
            "frontend_note_zh": "前端配置的中文说明",
            "frontend_version": "前端版本标识，用于区分声纹档案",
            "export_source": "ONNX导出实现来源",
            "export_source_url": "ONNX导出脚本的固定源码链接",
            "export_compatibility_note_zh": "动态池化导出兼容处理的等价性说明",
            "architecture_source_commit": "3D-Speaker模型架构源码提交号",
            "architecture_source_url": "固定模型架构源码链接",
            "versions": "本次准备使用的软件依赖版本",
            "versions_note_zh": "软件依赖版本名称的中文解释",
            "verification": "各测试音频时长下的数值一致性结果",
            "verification_fields_zh": "数值验证字段的中文解释",
            "short_utterance_accuracy_validated": "是否验证过短句声纹识别准确率",
            "short_utterance_accuracy_note_zh": "短句准确率验证状态的中文说明",
        },
        "model_id": MODEL_ID,
        "model_id_note_zh": "ModelScope 模型编号",
        "revision": MODEL_REVISION,
        "revision_note_zh": "固定模型修订版",
        "weight_file": WEIGHT_FILE,
        "weight_size_bytes": WEIGHT_SIZE_BYTES,
        "weight_sha256": actual_hash,
        "onnx_file": "campplus.onnx",
        "onnx_size_bytes": exported["onnx_size_bytes"],
        "onnx_sha256": exported["onnx_sha256"],
        "onnx_input": {"name": "feature", "shape": ["batch_size", "frame_num", 80],
                       "note_zh": "特征输入：批次、动态帧数、80 维梅尔滤波器组"},
        "onnx_output": {"name": "embedding", "shape": ["batch_size", 192],
                        "note_zh": "嵌入输出：批次、192 维说话人向量"},
        "frontend": "3D-Speaker FBank(80, sample_rate=16000, mean_nor=True); torchaudio Kaldi defaults; dither=0",
        "frontend_note_zh": "80维声学特征；16000 Hz；按时间均值归一；采用 Kaldi 默认 Povey 窗；抖动为0",
        "frontend_version": "3d-speaker-kaldi-fbank-povey-cmn-v1",
        "export_source": "3D-Speaker export_speaker_embedding_onnx.py export_onnx_file equivalent",
        "export_compatibility_note_zh": "导出时将 CAMLayer 尾段平均池化标记为不计入 ceil padding；与原始 PyTorch 结果等价，可避免动态 ONNX 在非整段长度时分母不一致。",
        "export_source_url": "https://github.com/modelscope/3D-Speaker/blob/" + THREE_D_SPEAKER_COMMIT + "/speakerlab/bin/export_speaker_embedding_onnx.py",
        "architecture_source_commit": THREE_D_SPEAKER_COMMIT,
        "architecture_source_url": "https://github.com/modelscope/3D-Speaker/tree/" + THREE_D_SPEAKER_COMMIT,
        "versions": {name: _version(name) for name in ("torch", "torchaudio", "onnx", "onnxruntime", "modelscope")},
        "versions_note_zh": "torch（张量框架）、torchaudio（音频特征库）、onnx（模型交换格式库）、onnxruntime（模型运行时）、modelscope（模型仓库客户端）的版本。",
        "verification_fields_zh": {
            "seconds": "测试音频时长（秒）",
            "frames": "FBank（梅尔滤波器组）特征帧数",
            "feature_dim": "每帧声学特征维度",
            "embedding_dim": "说话人嵌入向量维度",
            "frontend_max_abs_error": "与官方前端参考值之间的最大绝对误差",
            "export_compatibility_max_abs_error": "导出兼容池化与原版PyTorch输出之间的最大绝对误差",
            "embedding_max_abs_error": "PyTorch与ONNX嵌入之间的最大绝对误差",
            "embedding_cosine": "两种推理输出的余弦相似度",
            "finite": "输出是否全部为有限数值",
        },
        "verification": exported["checks"],
        "short_utterance_accuracy_validated": False,
        "short_utterance_accuracy_note_zh": "数值检查只验证实现一致性；未验证0.75至3秒中文短句声纹准确率。",
    }
    (model_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    root = workspace_root()
    parser = argparse.ArgumentParser(description="Prepare the optional ModelScope CAM++ ONNX model")
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=root / ".local" / "models" / "speaker" / "campplus-zh-cn-common",
        help="model_dir（CAM++ 模型目录）",
    )
    parser.add_argument("--skip-download", action="store_true", help="reuse the pinned checkpoint already in model_dir")
    args = parser.parse_args()
    manifest = prepare(args.model_dir, download=not args.skip_download)
    print(json.dumps({
        "model_dir": str(args.model_dir.resolve()),
        "weight_sha256": manifest["weight_sha256"],
        "onnx_sha256": manifest["onnx_sha256"],
        "verification": manifest["verification"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

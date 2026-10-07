"""安装阶段导出 FSMN（前馈序列记忆网络），保留原始资产并原子提交候选。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())))
from voice_terminal.vad.onnx_backend import file_hash


def export(model_dir: Path) -> Path:
    for name in ("model.pt", "config.yaml", "am.mvn"):
        if not (model_dir / name).is_file():
            raise FileNotFoundError(f"仅从已安装的本地权重导出，缺少：{model_dir / name}")
    destination = model_dir / "onnx"
    if destination.exists():
        raise RuntimeError("候选导出目录已存在；校验或保留现有版本，不自动覆盖")
    from funasr import AutoModel
    model = AutoModel(model=str(model_dir), device="cpu", disable_update=True, disable_pbar=True)
    with tempfile.TemporaryDirectory(prefix=".vad-export-", dir=model_dir) as temporary:
        staging = Path(temporary)
        # 本机 PyTorch（张量推理框架）2.11 的默认新导出器不保证旧动态轴语义；显式用兼容导出。
        import copy
        import torch
        config = copy.deepcopy(model.kwargs)
        config.pop("model", None)
        rebuilt = copy.deepcopy(model.model).cpu().export(**config)
        rebuilt.eval()
        torch.onnx.export(rebuilt, rebuilt.export_dummy_inputs(), str(staging / "model.onnx"),
                          input_names=rebuilt.export_input_names(), output_names=rebuilt.export_output_names(),
                          dynamic_axes=rebuilt.export_dynamic_axes(), opset_version=14, dynamo=False)
        import onnx
        onnx.checker.check_model(onnx.load(staging / "model.onnx"))
        for name in ("config.yaml", "am.mvn"):
            shutil.copy2(model_dir / name, staging / name)
        import onnxruntime as ort
        options = ort.SessionOptions(); options.intra_op_num_threads = 4
        session = ort.InferenceSession(str(staging / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
        import numpy as np
        comparisons = []
        torch.manual_seed(42)
        for frames in (1, 5, 20, 30, 65):
            inputs = rebuilt.export_dummy_inputs(frame=frames)
            with torch.inference_mode():
                scores, caches = rebuilt(*inputs)
            expected = [scores, *caches]
            actual = session.run(None, {name: tensor.numpy() for name, tensor in zip(rebuilt.export_input_names(), inputs)})
            differences = []
            for reference, output in zip(expected, actual, strict=True):
                reference = reference.numpy()
                if not np.allclose(reference, output, rtol=1e-4, atol=1e-5):
                    raise RuntimeError(f"未量化导出的数值对照失败：{frames} 帧")
                differences.append(float(np.max(np.abs(reference-output))))
            comparisons.append({"frames": frames, "max_absolute_differences": differences})
        manifest = {"schema_version": 1, "source_sha256": file_hash(model_dir / "model.pt"),
                    "quantized": False, "numerical_validation": comparisons,
                    "files": {name: file_hash(staging / name) for name in ("model.onnx", "config.yaml", "am.mvn")}}
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staging, destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出并校验轻量流式检测资产")
    parser.add_argument("--model-dir", type=Path, required=True, help="现有检测模型目录")
    print(export(parser.parse_args().model_dir.resolve()))

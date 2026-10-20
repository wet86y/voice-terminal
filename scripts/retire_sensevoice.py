"""退役旧模型：仅删除安装收据或根清单确认的 SenseVoice（旧语音理解模型）文件。

默认只核对并打印计划；--apply（执行）前会逐一核验三个替代模型的固定摘要。
未登记文件保留，不递归删除目录，不删除评测缓存、历史会话或旧报告。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_terminal.analysis.snapshot import verify_snapshot
from voice_terminal.distribution import manifest
from voice_terminal.model_installation import inspect_installed_model
from voice_terminal.paths import atomic_json

OLD_WEIGHT_SHA256 = "833ca2dcfdf8ec91bd4f31cfac36d6124e0c459074d5e909aec9cabe6204a3ea"


def retire(models: Path, *, apply: bool = False) -> dict:
    models = models.resolve(strict=True)
    target = models / "analysis/sensevoice"
    report = {"target": str(target), "applied": apply, "removed": [], "retained": [],
              "fields_zh": {"target": "已解析的旧组件目录", "applied": "是否执行删除",
                            "removed": "经安装清单核验的已删除文件", "retained": "未登记而保留的文件"}}
    if not target.exists():
        return report
    if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
        raise ValueError("旧组件目录不能是链接")
    installed = inspect_installed_model(models, "analysis/sensevoice", "sensevoice-small", verify_hashes=True)
    if installed.error:
        raise ValueError(f"旧组件受管清单核验失败，保留原文件：{installed.error}")
    weight = next((row for row in installed.files if row["path"] == "model.pt"), None)
    if weight is None or weight["sha256"] != OLD_WEIGHT_SHA256:
        raise ValueError("旧权重身份与退役组件不一致，保留原文件")
    for folder, resource in (("funasr/paraformer-zh-offline", "paraformer-offline"),
                              ("analysis/emotion2vec-plus-base", "emotion2vec-plus-base"),
                              ("analysis/yamnet", "yamnet")):
        verify_snapshot(models / folder, resource)
    planned = [target / row["path"] for row in installed.files]
    receipt = target / "resource-install.json"
    if receipt.is_file() and json.loads(receipt.read_text("utf-8-sig")).get("resource_id") == "sensevoice-small":
        planned.append(receipt)
    root = target.resolve(strict=True)
    for path in planned:
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("清单文件越出旧组件目录")
    if apply:
        for path in planned:
            path.unlink()
            report["removed"].append(path.relative_to(models).as_posix())
        for directory in sorted({path.parent for path in planned}, key=lambda path: len(path.parts), reverse=True):
            if directory.is_relative_to(root) and not any(directory.iterdir()):
                directory.rmdir()
        if target.exists() and not any(target.iterdir()):
            target.rmdir()
        record = manifest(models, kind="models", build="audio-enhancements")
        record["build"] = hashlib.sha256(json.dumps(record["files"], sort_keys=True).encode()).hexdigest()[:16]
        atomic_json(models / "manifest.json", record)
    report["retained"] = [str(path.relative_to(models)) for path in target.rglob("*")
                          if path.is_file() and path not in planned] if target.exists() else []
    report["planned_files"] = [str(path.relative_to(models)) for path in planned]
    report["fields_zh"]["planned_files"] = "核验通过的待退役文件"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True, help="受管模型根目录")
    parser.add_argument("--apply", action="store_true", help="执行已核验文件的删除")
    parser.add_argument("--report", type=Path, help="迁移报告保存位置")
    args = parser.parse_args()
    result = retire(args.models, apply=args.apply)
    if args.report:
        atomic_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))

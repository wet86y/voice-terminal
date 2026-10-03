"""依赖诊断：安装器、启动器和诊断工具共用同一运行版本清单。"""
from __future__ import annotations
import argparse
from importlib import metadata
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def diagnose(*, candidate=False, check_dependencies=True) -> dict:
    required = {}
    for line in (ROOT / "apps/server/runtime-constraints.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, version = line.split("==", 1)
            required[name] = version
    optional = {"funasr-onnx", "kaldi-native-fbank", "onnx", "ml-dtypes"}
    mismatches = []
    for name, version in required.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            if not candidate and name in optional:
                continue
            actual = None
        if name in {"sherpa-onnx", "sherpa-onnx-core", "onnxruntime"}:
            # 可选 CUDA（图形处理器计算）组合保持安装脚本的既有版本选择。
            if name.startswith("sherpa") and actual == "1.13.8+cuda12.cudnn9":
                continue
            if name == "onnxruntime" and actual is None:
                try:
                    actual = metadata.version("onnxruntime-gpu")
                except metadata.PackageNotFoundError:
                    pass
        if actual != version:
            mismatches.append({"package": name, "expected": version, "actual": actual})  # 包、期望版本、实际版本。
    torch_versions = {}
    for profile in ("cpu", "cu128"):
        for line in (ROOT / f"apps/server/requirements-torch-{profile}.txt").read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.startswith("#"):
                name, version = line.split("==", 1)
                torch_versions.setdefault(name, set()).update((version, version.split("+")[0]))
    for name, allowed in torch_versions.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = None
        if actual not in allowed:
            mismatches.append({"package": name, "expected": " / ".join(sorted(allowed)), "actual": actual})
    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True,
                           timeout=30) if check_dependencies else None
    return {"python": sys.version, "executable": sys.executable, "python_compatible": sys.version_info[:2] == (3, 12),
            "isolated": sys.prefix != sys.base_prefix, "mismatches": mismatches,
            "dependency_check_ok": check.returncode == 0 if check else None,
            "dependency_check": check.stdout.strip() if check else "启动只做版本检查，完整一致性检查由安装器与诊断工具执行"}


def main():
    parser = argparse.ArgumentParser(description="按统一依赖清单诊断运行环境")
    parser.add_argument("--candidate", action="store_true", help="要求完整轻量候选依赖")
    result = diagnose(candidate=parser.parse_args().candidate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["python_compatible"] and not result["mismatches"] and result["dependency_check_ok"] else 1)


if __name__ == "__main__":
    main()

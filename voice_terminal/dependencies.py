"""依赖诊断：安装器、启动器和诊断工具共用同一运行版本清单。"""
from __future__ import annotations
import argparse
from importlib import metadata
import json
from pathlib import Path
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
ONNX_GPU_VERSION = "1.24.4"  # ONNX_GPU_VERSION（已验证的显卡推理运行库版本）。


def dependency_errors(directory: Path | None = None) -> list[str]:
    """dependency_errors（依赖闭包错误）：显卡运行库提供相同的 ONNX（模型格式）模块。"""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    options = {"path": [str(directory)]} if directory is not None else {}
    installed = {canonicalize_name(item.metadata["Name"]): item for item in metadata.distributions(**options)}
    failures = []
    if "onnxruntime" in installed and "onnxruntime-gpu" in installed:
        failures.append("ONNX Runtime（模型推理运行时）中央处理器版与显卡版不能同时安装，模块文件会相互覆盖")
    for package in installed.values():
        for raw in package.requires or []:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            name = canonicalize_name(requirement.name)
            dependency = installed.get(name)
            if dependency is None and name == "onnxruntime":
                dependency = installed.get("onnxruntime-gpu")
            if dependency is None or dependency.version not in requirement.specifier:
                failures.append(f"{package.metadata['Name']} 需要 {requirement}")
    return failures


def diagnose(*, candidate=False, check_dependencies=True) -> dict:
    required = {}
    for line in (ROOT / "apps/server/runtime-constraints.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, version = line.split("==", 1)
            required[name] = version
    # 产品私有环境按安装锁诊断；开发、模型导出工具仍使用完整开发约束。
    runtime_record = Path(sys.executable).parent / "runtime.json"
    if not candidate and runtime_record.is_file():
        lock = json.loads((ROOT / "config/runtime/voice-runtime.lock.json").read_text(encoding="utf-8"))
        required = {row["name"]: row["version"] for row in lock["wheels"]
                    if row["name"] not in {"torch", "torchaudio"}}
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
                    version = ONNX_GPU_VERSION
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
    try:
        metadata.version("onnxruntime")
        metadata.version("onnxruntime-gpu")
    except metadata.PackageNotFoundError:
        pass
    else:
        mismatches.append({"package": "onnxruntime / onnxruntime-gpu", "expected": "仅安装一种模型推理运行库", "actual": "两者同时存在"})
    # funasr-onnx（语音推理包装）元数据只声明中央处理器包名；同模块的显卡发行包是有效替代。
    # 不能同时安装两个发行包来让 pip check（包名依赖检查）表面通过。
    failures = dependency_errors() if check_dependencies else None
    return {"python": sys.version, "executable": sys.executable, "python_compatible": sys.version_info[:2] == (3, 12),
            "isolated": sys.prefix != sys.base_prefix, "mismatches": mismatches,
            "dependency_check_ok": not failures if failures is not None else None,
            "dependency_check": "\n".join(failures) if failures is not None else "启动只做版本检查，完整一致性检查由安装器与诊断工具执行"}


def main():
    parser = argparse.ArgumentParser(description="按统一依赖清单诊断运行环境")
    parser.add_argument("--candidate", action="store_true", help="要求完整轻量候选依赖")
    result = diagnose(candidate=parser.parse_args().candidate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["python_compatible"] and not result["mismatches"] and result["dependency_check_ok"] else 1)


if __name__ == "__main__":
    main()

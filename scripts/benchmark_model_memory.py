"""顺序对照 baseline（保全源码）与 candidate（当前源码）的单模型运行资源。

每侧均使用新的 Python 子进程，以隔离已加载的 DLL（动态库）、运行时状态与
CUDA（显卡计算平台）上下文。此脚本不会触碰麦克风，也不调用云端。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "benchmark_runtime_resources.py"
DEFAULT_BASELINE = ROOT / ".local" / "cache" / "memory-optimization-20261010" / "baseline-source"

SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH", "COOKIE")


def _parse_env(values: list[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"{label} 必须写成 KEY=VALUE：{item}")
        key, value = item.split("=", 1)
        if not key or "=" in key:
            raise ValueError(f"{label} 含无效环境变量名：{item}")
        result[key] = value
    return result


def _redact_environment(env: dict[str, str]) -> dict[str, str]:
    return {key: ("<已隐藏>" if any(word in key.upper() for word in SECRET_WORDS) else value)
            for key, value in sorted(env.items())}


def _source_manifest(root: Path) -> dict[str, Any]:
    path = root / "source-manifest.json"
    if not path.is_file():
        return {"manifest_path": str(path), "manifest_sha256": None, "manifest_file_count": None}
    raw = path.read_bytes()
    try:
        parsed = json.loads(raw)
        count = len(parsed.get("files", []))
    except Exception:
        count = None
    return {"manifest_path": str(path), "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "manifest_file_count": count}


def _side_environment(args: argparse.Namespace, side: str) -> dict[str, str]:
    env: dict[str, str] = {}
    env.update(_parse_env(getattr(args, f"{side}_env"), f"--{side}-env"))
    import_mode = getattr(args, f"{side}_asr_import_mode")
    load_mode = getattr(args, f"{side}_weight_load_mode")
    speaker_mode = getattr(args, f"{side}_speaker_session_mode")
    # 这两项可被独立对照；不依赖程序内的默认值。
    env["VOICE_ASR_IMPORT_MODE"] = import_mode
    env["VOICE_MODEL_LOAD_MODE"] = load_mode
    env["VOICE_SPEAKER_SESSION_MODE"] = speaker_mode
    return env


def _worker_command(args: argparse.Namespace, side: str, side_output: Path) -> tuple[list[str], dict[str, str]]:
    root = getattr(args, f"{side}_root").resolve()
    model_root = getattr(args, f"{side}_model_root").resolve()
    command = [
        sys.executable, str(WORKER),
        "--role", args.role,
        "--program-root", str(root),
        "--model-root", str(model_root),
        "--device", args.device,
        "--threads", str(args.threads),
        "--warmup-runs", str(args.warmup_runs),
        "--iterations", str(args.iterations),
        "--sample-interval", str(args.sample_interval),
        "--backend", getattr(args, f"{side}_backend"),
        "--output", str(side_output),
    ]
    audio = args.audio.resolve() if args.audio else None
    if audio:
        command.extend(("--audio", str(audio)))
    vad_dir = getattr(args, f"{side}_vad_dir")
    if vad_dir:
        command.extend(("--vad-dir", str(vad_dir.resolve())))
    refine_dir = getattr(args, f"{side}_refine_model_dir")
    if refine_dir:
        command.extend(("--refine-model-dir", str(refine_dir.resolve())))
    if args.empty_cuda_cache:
        command.append("--empty-cuda-cache")
    env = os.environ.copy()
    env.update(_side_environment(args, side))
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "VOICE_MODEL_THREADS"):
        env[name] = str(args.threads)
    return command, env


def _read_worker_result(path: Path, return_code: int, stdout: str, stderr: str) -> dict[str, Any]:
    result: dict[str, Any]
    if path.is_file():
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            result = {"success": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    else:
        result = {"success": False, "error": {"type": "WorkerDidNotWriteReport",
                                                  "message": "子进程未生成 JSON 报告"}}
    result["exit_code"] = return_code
    result["stdout"] = stdout[-12000:]
    result["stderr"] = stderr[-12000:]
    result["subprocess_success"] = return_code == 0
    return result


def _compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """只给出同一角色的实测字段差值，不外推为整套应用节省。"""
    paths = {
        "rss_peak_bytes": ("resources", "sampled_peaks", "rss_bytes"),
        "windows_peak_wset_bytes": ("resources", "sampled_peaks", "peak_wset_bytes"),
        "private_commit_peak_bytes": ("resources", "sampled_peaks", "private_commit_bytes"),
        "dedicated_gpu_peak_bytes": ("resources", "sampled_peaks", "gpu_dedicated_bytes"),
        "shared_gpu_peak_bytes": ("resources", "sampled_peaks", "gpu_shared_bytes"),
        "torch_allocated_peak_bytes": ("resources", "sampled_peaks", "torch_allocated_bytes"),
        "torch_reserved_peak_bytes": ("resources", "sampled_peaks", "torch_reserved_bytes"),
        "available_memory_min_bytes": ("resources", "sampled_peaks", "system_available_physical_bytes_min"),
        "inference_p50_ms": ("inference_ms", "percentiles", "p50"),
        "inference_p95_ms": ("inference_ms", "percentiles", "p95"),
        "model_import_ms": ("resources", "stage_durations_ms", "model_import"),
        "model_load_ms": ("resources", "stage_durations_ms", "model_load"),
        "explicit_warmup_ms": ("resources", "stage_durations_ms", "explicit_warmup"),
        "release_ms": ("resources", "stage_durations_ms", "release"),
    }
    def get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
        for component in path:
            if not isinstance(data, dict):
                return None
            data = data.get(component)
        return data
    output: dict[str, Any] = {}
    for name, path in paths.items():
        before, after = get(baseline, path), get(candidate, path)
        output[name] = {"baseline": before, "candidate": after,
                        "candidate_minus_baseline": after - before
                        if isinstance(before, (int, float)) and isinstance(after, (int, float)) else None}
    output["semantic_outputs_equal"] = (
        baseline.get("semantic_outputs") == candidate.get("semantic_outputs")
        if "semantic_outputs" in baseline and "semantic_outputs" in candidate else None)
    if baseline.get("role") == "speaker" and candidate.get("role") == "speaker":
        output["speaker_embedding_comparisons"] = _compare_speaker_embeddings(
            baseline.get("outputs_raw"), candidate.get("outputs_raw"),
            rtol=baseline.get("speaker_embedding_rtol", 1e-5),
            atol=baseline.get("speaker_embedding_atol", 1e-6))
        matches = output["speaker_embedding_comparisons"].get("iterations", [])
        output["semantic_outputs_equal"] = bool(matches) and all(item.get("allclose") is True for item in matches)
    output["audio_sha256_equal"] = (
        baseline.get("audio", {}).get("sha256") == candidate.get("audio", {}).get("sha256")
        if "audio" in baseline and "audio" in candidate else None)
    output["interpretation"] = "以上仅是当前单模型、指定音频与所选阶段的实测差值；不得直接外推为整套应用多进程总节省。"
    return output


def _compare_speaker_embeddings(baseline: Any, candidate: Any, *, rtol: float, atol: float) -> dict[str, Any]:
    """用 numpy.allclose 比较嵌入，并记录最大绝对差和余弦距离。"""
    try:
        import numpy as np
    except Exception as exc:
        return {"iterations": [], "allclose": None,
                "error": f"NumPy（数值计算库）不可用：{type(exc).__name__}: {exc}"}
    if not isinstance(baseline, list) or not isinstance(candidate, list):
        return {"iterations": [], "allclose": None, "error": "缺少声纹嵌入输出"}
    comparisons = []
    for index, (left_value, right_value) in enumerate(zip(baseline, candidate)):
        left = np.asarray(left_value, dtype=np.float64)
        right = np.asarray(right_value, dtype=np.float64)
        same_shape = left.shape == right.shape
        if same_shape and left.size:
            difference = np.abs(left - right)
            max_absolute_difference = float(np.max(difference))
            denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
            cosine_distance = (float(1.0 - np.dot(left.ravel(), right.ravel()) / denominator)
                               if denominator > 0 else None)
            allclose = bool(np.allclose(left, right, rtol=rtol, atol=atol))
        elif same_shape:
            max_absolute_difference = 0.0
            cosine_distance = None
            allclose = bool(np.allclose(left, right, rtol=rtol, atol=atol))
        else:
            max_absolute_difference = None
            cosine_distance = None
            allclose = False
        comparisons.append({"iteration": index, "dimensions_match": same_shape,
                            "embedding_shape": list(left.shape), "candidate_shape": list(right.shape),
                            "allclose": allclose,
                            "max_absolute_difference": max_absolute_difference,
                            "cosine_distance": cosine_distance})
    return {"iterations": comparisons, "rtol": rtol, "atol": atol,
            "allclose": len(baseline) == len(candidate) and bool(comparisons)
            and all(item["allclose"] for item in comparisons),
            "baseline_iteration_count": len(baseline), "candidate_iteration_count": len(candidate)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="串行运行隔离基线/候选进程，输出可复核资源对照 JSON")
    parser.add_argument("--role", choices=("vad", "speaker", "asr", "refine", "endpoint"), required=True)
    parser.add_argument("--baseline-root", type=Path, default=DEFAULT_BASELINE,
                        help="冻结的原始源码根目录，须含 voice_terminal 与 source-manifest.json")
    parser.add_argument("--candidate-root", type=Path, default=ROOT, help="候选源码根目录；缺省为当前仓库")
    parser.add_argument("--model-root", type=Path, help="基线和候选共用模型资产根目录")
    parser.add_argument("--baseline-model-root", type=Path, help="可单独指定基线模型资产根目录")
    parser.add_argument("--candidate-model-root", type=Path, help="可单独指定候选模型资产根目录")
    parser.add_argument("--audio", type=Path, help="固定 16 kHz 单声道 WAV；不指定时用同一 Paraformer 示例音频")
    parser.add_argument("--device", choices=("cpu", "cuda", "auto"), default="cpu")
    parser.add_argument("--threads", type=int, default=2, help="两侧共用线程预算")
    parser.add_argument("--speaker-rtol", type=float, default=1e-5, help="声纹向量 numpy.allclose 相对容差")
    parser.add_argument("--speaker-atol", type=float, default=1e-6, help="声纹向量 numpy.allclose 绝对容差")
    parser.add_argument("--warmup-runs", type=int, default=2, help="两侧相同显式预热次数")
    parser.add_argument("--iterations", type=int, default=3, help="两侧相同正式推理轮数")
    parser.add_argument("--sample-interval", type=float, default=.25, help="资源后台采样间隔（秒）")
    parser.add_argument("--baseline-backend", choices=("funasr", "onnx"), default="funasr")
    parser.add_argument("--candidate-backend", choices=("funasr", "onnx"), default="funasr")
    parser.add_argument("--baseline-vad-dir", type=Path)
    parser.add_argument("--candidate-vad-dir", type=Path)
    parser.add_argument("--baseline-refine-model-dir", type=Path)
    parser.add_argument("--candidate-refine-model-dir", "--refine-model-dir", dest="candidate_refine_model_dir", type=Path,
                        help="候选精修资产目录；用于 ONNX 时需已存在 model_quant.onnx 与 BPE 文件")
    parser.add_argument("--baseline-asr-import-mode", choices=("full", "selective"),
                        help="基线 FunASR 导入模式；缺省依角色采用当时默认行为")
    parser.add_argument("--candidate-asr-import-mode", choices=("full", "selective"), default="selective")
    parser.add_argument("--baseline-weight-load-mode", choices=("original", "mmap"), default="original")
    parser.add_argument("--candidate-weight-load-mode", choices=("original", "mmap"), default="mmap")
    parser.add_argument("--baseline-speaker-session-mode", choices=("original", "bounded"), default="original")
    parser.add_argument("--candidate-speaker-session-mode", choices=("original", "bounded"), default="bounded")
    parser.add_argument("--baseline-env", action="append", default=[], metavar="KEY=VALUE",
                        help="额外基线环境变量，可重复指定")
    parser.add_argument("--candidate-env", action="append", default=[], metavar="KEY=VALUE",
                        help="额外候选环境变量，可重复指定")
    parser.add_argument("--empty-cuda-cache", action="store_true", help="释放后显式清理 PyTorch CUDA 缓存分配器")
    parser.add_argument("--stop-on-error", action="store_true", help="基线失败后不启动候选子进程")
    parser.add_argument("--order", choices=("baseline-first", "candidate-first"), default="baseline-first",
                        help="两侧启动顺序；复测可反转顺序，检查文件缓存和系统内存压力的影响")
    parser.add_argument("--dry-run", action="store_true", help="只校验参数并显示串行命令，不启动模型子进程")
    parser.add_argument("--output", type=Path, required=True, help="最终 JSON 对照报告路径")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.threads < 1 or args.warmup_runs < 0 or args.iterations < 1 or args.sample_interval <= 0:
        raise SystemExit("线程数/推理轮数须为正数，预热次数须非负，采样间隔须为正数")
    args.baseline_root = args.baseline_root.resolve()
    args.candidate_root = args.candidate_root.resolve()
    for side in ("baseline", "candidate"):
        root = getattr(args, f"{side}_root")
        if not (root / "voice_terminal").is_dir():
            raise SystemExit(f"{side} 源码根目录缺少 voice_terminal：{root}")
    if not (args.baseline_root / "source-manifest.json").is_file():
        raise SystemExit(f"基线必须有保全清单 source-manifest.json：{args.baseline_root}")
    common_model_root = args.model_root.resolve() if args.model_root else ROOT / "models"
    args.baseline_model_root = (args.baseline_model_root or common_model_root).resolve()
    args.candidate_model_root = (args.candidate_model_root or common_model_root).resolve()
    if args.baseline_asr_import_mode is None:
        # Paraformer 基线已经采用按需导入；原版精修/VAD 仍走完整 FunASR 导入。
        args.baseline_asr_import_mode = "full" if args.role in {"refine", "vad"} else "selective"
    if args.role not in {"refine", "vad"} and (args.baseline_backend != "funasr" or args.candidate_backend != "funasr"):
        raise SystemExit("只有 refine（精修）与 vad（语音活动检测）角色支持 ONNX 后端")
    if args.role == "refine" and "onnx" in {args.baseline_backend, args.candidate_backend} and args.device != "cpu":
        raise SystemExit("官方 funasr_onnx SenseVoiceSmall 此对照按 CPU（中央处理器）路径运行，请使用 --device cpu")
    # 预先校验额外环境变量，避免基线跑完后才发现候选命令输入有误。
    _parse_env(args.baseline_env, "--baseline-env")
    _parse_env(args.candidate_env, "--candidate-env")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sides: dict[str, Any] = {}
    final: dict[str, Any] = {
        "description_zh": "基线与候选分别在串行独立 Python 进程中运行；内存字段不能代表整套应用所有进程的加和。",
        "role": args.role,
        "requested_device": args.device,
        "thread_budget": args.threads,
        "warmup_runs": args.warmup_runs,
        "iterations": args.iterations,
        "execution_order": args.order,
        "baseline_source": {"root": str(args.baseline_root), **_source_manifest(args.baseline_root)},
        "candidate_source": {"root": str(args.candidate_root), **_source_manifest(args.candidate_root)},
        "field_descriptions_zh": {
            "baseline": "保全的修改前源码一侧。",
            "execution_order": "两个隔离进程的执行顺序；不同侧始终串行运行。",
            "candidate": "当前候选源码一侧。",
            "resources.samples": "带阶段标签的周期样本与阶段边界样本；采样最大值可能错过短时峰值。",
            "rss_bytes": "常驻工作集字节；共享映射页仍计入进程 RSS。",
            "peak_wset_bytes": "Windows 进程自启动以来的 Peak Working Set（峰值工作集）；非 Windows/不可读为 null，与周期采样峰值分列。",
            "private_commit_bytes": "Windows 进程私有提交量近似值，不等于系统总提交量。",
            "system_available_physical_bytes": "采样时系统可用物理内存字节。",
            "gpu_dedicated_bytes": "进程专用 GPU 内存字节。",
            "gpu_shared_bytes": "进程共享 GPU 内存字节。",
            "torch_allocated_bytes": "PyTorch 已分配张量显存字节。",
            "torch_reserved_bytes": "PyTorch 缓存分配器保留显存字节，可能大于已分配量。",
            "speaker_embedding_comparisons": "声纹嵌入对照使用 numpy.allclose，并附最大绝对差和余弦距离。",
            "semantic_outputs_equal": "两侧结构化文本/语言/情绪/事件标签输出是否一致；缺失输出为 null。",
            "candidate_minus_baseline": "候选值减去基线值；内存正值表示候选更高。",
            "model_import_ms": "模型 API（编程接口）模块导入耗时。",
            "model_load_ms": "模型构造和权重加载耗时；含模型内部预热的角色会另行注明。",
            "explicit_warmup_ms": "双方使用相同次数固定音频预热的累计耗时。",
            "release_ms": "清理模型引用和垃圾回收阶段耗时。",
        },
        "source_comparison_scope": "仅比较此角色与给定固定音频，不按单模型结果外推应用总量。",
        "success": True,
    }
    temp_context = tempfile.TemporaryDirectory(prefix="benchmark-model-memory-")
    temp_root = Path(temp_context.name)
    try:
        order = ("candidate", "baseline") if args.order == "candidate-first" else ("baseline", "candidate")
        for side in order:
            report_path = temp_root / f"{side}.json"
            command, env = _worker_command(args, side, report_path)
            run_info = {"command": command, "environment_overrides": _redact_environment(_side_environment(args, side))}
            if args.dry_run:
                sides[side] = {"command": run_info["command"],
                               "environment_overrides": run_info["environment_overrides"],
                               "source_root": str(getattr(args, f"{side}_root")),
                               "source_manifest": _source_manifest(getattr(args, f"{side}_root"))}
                continue
            started = time.perf_counter()
            try:
                completed = subprocess.run(command, env=env, capture_output=True, text=True,
                                           encoding="utf-8", errors="replace", check=False)
                side_result = _read_worker_result(report_path, completed.returncode,
                                                   completed.stdout, completed.stderr)
            except Exception as exc:
                side_result = {"success": False, "subprocess_success": False,
                               "error": {"type": type(exc).__name__, "message": str(exc)}}
            side_result["subprocess_elapsed_seconds"] = time.perf_counter() - started
            side_result["source_root"] = str(getattr(args, f"{side}_root"))
            side_result["source_manifest"] = _source_manifest(getattr(args, f"{side}_root"))
            side_result["environment_overrides"] = run_info["environment_overrides"]
            side_result["speaker_embedding_rtol"] = args.speaker_rtol
            side_result["speaker_embedding_atol"] = args.speaker_atol
            sides[side] = side_result
            if not side_result.get("success") and args.stop_on_error:
                final["success"] = False
                final["stopped_after"] = side
                break
        final["sides"] = sides
        if args.dry_run:
            final["dry_run"] = True
            final["comparison"] = None
        elif "baseline" in sides and "candidate" in sides:
            final["comparison"] = _compare(sides["baseline"], sides["candidate"])
            final["success"] = bool(sides["baseline"].get("success") and sides["candidate"].get("success"))
        else:
            final["comparison"] = None
            final["success"] = False
        output.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        temp_context.cleanup()
    print(json.dumps({"output": str(output), "role": args.role, "success": final["success"],
                      "dry_run": bool(args.dry_run),
                      "semantic_outputs_equal": (final.get("comparison") or {}).get("semantic_outputs_equal")},
                     ensure_ascii=False))
    return 0 if final["success"] or args.dry_run else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""SenseVoice（语音精修）FunASR FP32 与本地 ONNX INT8 质量对照。

Reference（参考）侧先用当前 SenseVoice 包装器给出 VAD 切片；candidate（候选）
侧再对相同 PCM（脉冲编码调制音频）和相同切片推理。两侧为串行、独立 Python
子进程。脚本不访问麦克风或网络，不下载模型，也不自动导出 ONNX。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
import wave
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ROOT = ROOT / ".local" / "models"
DEFAULT_CANDIDATE_MODEL = ROOT / ".local" / "cache" / "memory-optimization-20261010" / "models" / "sensevoice-onnx"
DEFAULT_KWS_RELATIVE = Path("kws") / "sherpa-onnx-kws-zipformer-zh-en-3M" / "test_wavs"

FIELD_DESCRIPTIONS_ZH = {
    "side": "被测侧的名称。",
    "success": "该侧所有模型与用例均执行成功且满足设备/配置要求；总报告还要求所有语义字段完全一致。",
    "reference": "参考侧：当前 SenseVoice 包装器使用 FunASR 权重，CPU（中央处理器）FP32（32位浮点）运行。",
    "candidate": "候选侧：本地 funasr_onnx SenseVoiceSmall 使用 model_quant.onnx（INT8，8位整数）并在 CPU 运行。",
    "execution_order": "参考侧先完成并退出，之后候选侧在新 Python 进程中运行。",
    "case_id": "固定测试用例标识。",
    "label_zh": "用例的中文名称。",
    "source_kind": "音频来源类型：已有 WAV 文件或确定性生成的静音 WAV。",
    "expected_language": "测试文件名所标出的 zh（中文）或 en（英语）；不作为模型输出判分替代。",
    "audio_path": "固定输入 WAV 的本地路径。",
    "import_mode_requested": "参考侧明确请求 selective（按需导入），并同时记录加载的 FunASR 模块数量。",
    "weight_load_mode_requested": "参考侧明确请求 mmap（内存映射）权重加载。",
    "weight_load_mode_applied": "从 FunASR 实际 load_pretrained_model 函数标记读取，验证不是仅设置环境变量。",
    "model_parameter_dtype": "参考模型第一个可见参数的数据类型，用于确认 FP32。",
    "precision_verified": "参考模型 dtype（数据类型）是否被确认是 torch.float32；无法确认则不宣称 FP32 对照成功。",
    "configuration_verified": "参考运行的设备、导入策略、实际权重加载器与浮点精度验证结果。",
    "actual_runtime": "运行时实测设备、执行提供程序、模型文件、权重精度及线程设置。",
    "device": "模型实际使用的设备。",
    "internal_vad_device": "参考 SenseVoice 包装器内部 VAD 实际设备。",
    "providers": "ONNX Runtime（模型推理运行时）实际启用的执行提供程序列表。",
    "model_file": "候选实际载入的 ONNX 模型文件名。",
    "intra_op_num_threads_requested": "ONNX 算子内部线程数请求值。",
    "threads_requested": "传给两侧运行时的线程预算。",
    "torch_num_threads": "参考侧 PyTorch（深度学习框架）实际计算线程数。",
    "torch_num_interop_threads": "参考侧 PyTorch 实际算子间线程数。",
    "torch_version": "参考侧 PyTorch 版本。",
    "asset_validation": "本地模型文件完备性检查。",
    "model_files_present": "按文件名列出的模型文件是否存在。",
    "vad_files_present": "按文件名列出的 VAD 文件是否存在。",
    "complete": "本地运行所需资产是否齐全。",
    "missing": "缺少的本地文件；出现缺件时工具禁止下载或导出。",
    "downloads_or_exports_permitted": "是否允许运行中下载模型或导出模型；本工具固定为 false。",
    "audio_sha256": "输入 WAV（波形音频）原始文件的 SHA-256（文件摘要）；静音样本为临时生成的固定 1 秒 WAV。",
    "pcm_sha256": "两侧解码后将同一 16 kHz 单声道 int16 PCM（脉冲编码音频）送入模型的字节摘要。",
    "sample_rate_hz": "输入音频采样率（赫兹）；只接受 16000。",
    "sample_count": "单声道 PCM 样本数。",
    "duration_seconds": "音频时长（秒）。",
    "manifest_hash_matches": "实际读取的输入文件摘要是否等于运行开始前清单中的摘要。",
    "input_status": "音频读取与格式校验状态。",
    "input_error": "音频读取或校验失败的异常信息。",
    "status": "该用例在此侧的执行状态。",
    "error": "该侧模型、音频或推理失败的详细错误。",
    "segments": "参考侧实际 VAD（语音活动检测）切片及其开始/结束毫秒和样本索引；候选侧重用完全相同的样本索引。",
    "full_text": "一段音频所有精修片段按原顺序拼接的文本。",
    "text": "单个 VAD 切片产生的精修文本。",
    "language": "模型识别的语言标签。",
    "emotion": "模型输出的情绪标签。",
    "events": "模型输出的声学事件标签。",
    "no_speech": "包装器是否将整段音频判为没有文本语音。",
    "segment_count": "参考 VAD 切片数；候选结果逐片映射到同一切片后统计。",
    "start_ms": "VAD 切片起始毫秒。",
    "end_ms": "VAD 切片结束毫秒。",
    "start_sample": "按 16 kHz 计算的切片起始 PCM 样本索引。",
    "end_sample": "按 16 kHz 计算的切片结束 PCM 样本索引。",
    "slice_pcm_sha256": "该 VAD 切片的实际 PCM 数据摘要，用来证明两侧处理同一音频范围。",
    "raw_tags": "SenseVoice 原始语言、情绪与事件标签列表。",
    "raw_tags_equal": "两侧每个切片解析出的原始语言、情绪与声学事件标签是否完全一致。",
    "raw_tagged_output": "ONNX 候选该片原始标签文本。",
    "raw_tagged_outputs": "ONNX 候选调用返回的全部原始标签文本。",
    "model_inference_performed": "该音频是否实际调用了被测 SenseVoice 模型；共享参考 VAD（语音活动检测）无切片时为 false。",
    "no_speech_basis": "无语音判断依据；若参考 VAD 无切片，候选侧记录为共享 VAD 路径，未实际运行候选模型。",
    "wrapper_complete": "参考包装器是否报告完整精修结果。",
    "output_count": "ONNX 单个切片返回的原始结果条目数；必须恰为 1。",
    "errors": "该用例中逐切片的执行异常列表。",
    "reference_segment_count": "参考 VAD 生成的切片总数。",
    "comparison": "逐用例、全文和逐段语义字段是否完全相同；任一差异或异常均使总退出码非零。",
    "passed": "该侧/用例/总报告是否通过严格语义对照。",
    "input_sha256_equal": "两侧读取到的 WAV 文件摘要是否一致。",
    "pcm_sha256_equal": "两侧解码后送入模型的 PCM 摘要是否一致。",
    "manifest_audio_hashes_valid": "两侧实际音频摘要是否都匹配用例清单摘要。",
    "reference_status": "参考侧此用例状态。",
    "candidate_status": "候选侧此用例状态。",
    "execution_succeeded": "参考与候选两侧是否均实际完成该用例推理。",
    "failure_type": "失败类型：execution_error（执行异常）、input_mismatch（输入不一致）、semantic_mismatch（模型输出差异）或 none（通过）。",
    "semantic_match": "在两侧均成功执行时，全文、无语音结果、段数、标签及逐段文本/切片是否严格相同。",
    "full_text_equal": "两侧整段精修文本是否逐字符一致。",
    "no_speech_equal": "两侧是否一致判定整段无文本语音。",
    "segment_count_equal": "两侧逐片结果数量是否一致。",
    "language_tags_equal": "两侧各片语言标签是否按顺序一致。",
    "emotion_tags_equal": "两侧各片情绪标签是否按顺序一致。",
    "event_tags_equal": "两侧各片声学事件标签是否按顺序一致。",
    "segment_comparisons": "逐片对照详情。",
    "exists_on_both_sides": "该切片是否在两侧结果中都存在。",
    "same_vad_slice": "两侧切片样本起止索引是否完全相同。",
    "same_slice_pcm": "两侧切片 PCM 摘要是否相同。",
    "text_equal": "该片精修文本是否逐字符相同。",
    "language_equal": "该片语言标签是否相同。",
    "emotion_equal": "该片情绪标签是否相同。",
    "events_equal": "该片事件标签是否相同。",
    "case_count": "参与对照的用例数量。",
    "passed_case_count": "通过所有严格对照条件的用例数量。",
    "failed_case_ids": "发生输出差异、运行异常或验证失败的用例标识列表。",
    "execution_error_case_ids": "至少一侧未能实际完成推理的用例标识列表。",
    "semantic_mismatch_case_ids": "两侧成功推理但输出存在差异的用例标识列表。",
    "all_model_executions_succeeded": "全部用例在参考侧和候选侧是否都完成了实际推理。",
    "all_audio_hashes_equal": "全部用例的两侧音频与 PCM 摘要是否一致。",
    "all_semantic_outputs_equal": "全部用例的文本、标签和切片数量是否一致。",
    "asset_validation": "本地权重/配置文件存在性清单；缺文件时记录失败，不触发下载或导出。",
    "model_error": "模型构造或执行异常及其 traceback（调用栈）；出现时保留全部用例记录。",
    "sentencepiece_path_workaround": "ONNX侧用 Python read_bytes 构造 SentencePieceProcessor，绕开 Windows 原生加载器对中文路径的限制；不改 site-packages（第三方安装目录）。",
    "created_at_utc": "报告生成时间，协调世界时。",
    "program_root": "被测应用源码根目录。",
    "threads": "两侧线程请求设置。",
    "intra_op": "算子内部并行线程数。",
    "reference_inter_op": "参考侧算子间线程数。",
    "exit_code": "隔离子进程退出码；非零代表执行/验收失败。",
    "process_return_code_ok": "子进程是否以退出码零结束。",
    "stdout": "子进程标准输出末尾文本。",
    "stderr": "子进程标准错误末尾文本。",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wav_silence_bytes(seconds: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000 * seconds)
    return buffer.getvalue()


def _case_paths(args: argparse.Namespace) -> list[dict[str, Any]]:
    asr_example = args.asr_example or args.model_root / "funasr" / "paraformer-zh-streaming" / "example" / "asr_example.wav"
    vad_example = args.vad_example or args.model_root / "funasr" / "fsmn-vad" / "example" / "vad_example.wav"
    kws_dir = args.kws_test_wavs_dir or (args.model_root / DEFAULT_KWS_RELATIVE)
    if not kws_dir.is_absolute():
        kws_dir = (ROOT / kws_dir).resolve()
    base_cases = [
        ("paraformer_asr_example", "Paraformer示例音频", asr_example, None),
        ("fsmn_vad_example", "FSMN-VAD示例音频", vad_example, None),
    ]
    if not kws_dir.is_dir():
        raise FileNotFoundError(f"KWS test_wavs（关键字唤醒测试音频）目录不存在：{kws_dir}")
    kws_files = sorted(path for path in kws_dir.glob("*.wav")
                       if path.stem.startswith(("zh_", "en_")))
    chinese = [path for path in kws_files if path.stem.startswith("zh_")]
    english = [path for path in kws_files if path.stem.startswith("en_")]
    if not chinese or not english:
        raise FileNotFoundError(f"KWS测试目录必须至少包含zh_*.wav与en_*.wav：{kws_dir}")
    for path in kws_files:
        lang = "zh" if path.stem.startswith("zh_") else "en"
        base_cases.append((f"kws_{path.stem}", f"KWS {lang} 测试音频", path, lang))
    cases = []
    for case_id, label, path, expected_language in base_cases:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"用例音频不存在：{resolved}")
        raw = resolved.read_bytes()
        cases.append({"case_id": case_id, "label_zh": label, "audio_path": str(resolved),
                      "source_kind": "wav_file", "expected_language": expected_language,
                      "audio_sha256_expected": _sha256(raw)})
    silence = _wav_silence_bytes(1)
    cases.append({"case_id": "silence_1s", "label_zh": "1秒静音", "audio_path": None,
                  "source_kind": "generated_silence", "expected_language": None,
                  "audio_sha256_expected": _sha256(silence), "generated_wav_sha256": _sha256(silence),
                  "generated_duration_seconds": 1})
    return cases


def _asset_validation(side: str, model_dir: Path, vad_dir: Path) -> dict[str, Any]:
    if side == "reference":
        required_model = ("model.pt", "config.yaml", "am.mvn", "chn_jpn_yue_eng_ko_spectok.bpe.model")
        required_vad = ("model.pt", "config.yaml", "am.mvn")
    else:
        required_model = ("model_quant.onnx", "config.yaml", "am.mvn", "chn_jpn_yue_eng_ko_spectok.bpe.model")
        required_vad = ()
    model_files = {name: (model_dir / name).is_file() for name in required_model}
    vad_files = {name: (vad_dir / name).is_file() for name in required_vad}
    missing = ([f"refine/{name}" for name, exists in model_files.items() if not exists]
               + [f"vad/{name}" for name, exists in vad_files.items() if not exists])
    return {"model_dir": str(model_dir.resolve()), "vad_dir": str(vad_dir.resolve()),
            "model_files_present": model_files, "vad_files_present": vad_files,
            "complete": not missing, "missing": missing,
            "downloads_or_exports_permitted": False}


def _load_cases(case_manifest: Path) -> tuple[list[dict[str, Any]], Any, Any]:
    import numpy as np
    import soundfile as sf

    manifest = json.loads(case_manifest.read_text(encoding="utf-8"))
    loaded: list[dict[str, Any]] = []
    for item in manifest["cases"]:
        path = Path(item["audio_path"]).resolve()
        case = {**item, "audio_path": str(path)}
        try:
            raw = path.read_bytes()
            values, sample_rate = sf.read(path, dtype="int16", always_2d=False)
            if int(sample_rate) != 16000:
                raise ValueError(f"采样率必须为16000Hz，实际是{sample_rate}")
            if getattr(values, "ndim", 1) != 1:
                raise ValueError("输入必须是单声道WAV")
            pcm = np.asarray(values, dtype="<i2").tobytes()
            case.update({"audio_sha256": _sha256(raw), "pcm_sha256": _sha256(pcm),
                         "sample_rate_hz": int(sample_rate), "sample_count": int(len(values)),
                         "duration_seconds": len(values) / sample_rate, "pcm": pcm,
                         "input_status": "ok"})
            expected = item.get("audio_sha256_expected")
            case["manifest_hash_matches"] = expected is None or expected == case["audio_sha256"]
        except Exception as exc:
            case.update({"audio_sha256": None, "pcm_sha256": None, "sample_rate_hz": None,
                         "sample_count": None, "duration_seconds": None, "pcm": None,
                         "input_status": "audio_error",
                         "input_error": f"{type(exc).__name__}: {exc}"})
        loaded.append(case)
    return loaded, np, sf


def _serialize_input(case: dict[str, Any]) -> dict[str, Any]:
    return {key: case.get(key) for key in ("case_id", "label_zh", "audio_path", "source_kind",
            "expected_language", "audio_sha256", "pcm_sha256", "sample_rate_hz", "sample_count",
            "duration_seconds", "manifest_hash_matches", "input_status", "input_error") if key in case}


def _model_dtype(sensevoice: Any) -> str | None:
    """尽量读取参考模型第一个参数 dtype（数据类型）；不可见时保留 unknown。"""
    candidates = [getattr(sensevoice, "model", None),
                  getattr(getattr(sensevoice, "model", None), "model", None)]
    for candidate in candidates:
        parameters = getattr(candidate, "parameters", None)
        if callable(parameters):
            try:
                return str(next(iter(parameters())).dtype)
            except (StopIteration, TypeError, AttributeError):
                continue
    return None


def _funasr_effective_load_mode() -> str | None:
    module = sys.modules.get("funasr.train_utils.load_pretrained_model")
    if module is None:
        return None
    function = getattr(module, "load_pretrained_model", None)
    return getattr(function, "__voice_model_load_mode__", "original")


def _reference_worker(args: argparse.Namespace) -> dict[str, Any]:
    os.environ["VOICE_ASR_IMPORT_MODE"] = "selective"
    os.environ["VOICE_MODEL_LOAD_MODE"] = "mmap"
    os.environ["VOICE_MODEL_THREADS"] = str(args.threads)
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[name] = str(args.threads)
    import logging
    logging.disable(logging.CRITICAL)
    cases, np, _ = _load_cases(args.case_manifest)
    asset = _asset_validation("reference", args.reference_model_dir, args.vad_model_dir)
    result: dict[str, Any] = {"side": "reference_funasr_fp32_cpu", "success": False,
        "device": "cpu", "precision_expected": "FP32（32位浮点）FunASR权重原样加载",
        "import_mode_requested": "selective（按需导入）", "weight_load_mode_requested": "mmap（内存映射）",
        "threads_requested": args.threads, "asset_validation": asset, "cases": []}
    if not asset["complete"]:
        result["model_error"] = {"type": "MissingLocalAssets", "message": str(asset["missing"])}
        result["cases"] = [{**_serialize_input(case), "status": "asset_error", "error": str(asset["missing"])}
                            for case in cases]
        return result
    try:
        import torch
        torch.set_num_threads(args.threads)
        try:
            torch.set_num_interop_threads(args.interop_threads)
        except RuntimeError:
            # 此 worker 的新进程中通常可设置；若第三方已初始化线程池，记录实际值。
            pass
        from voice_terminal.analysis.sensevoice import SenseVoice
        model = SenseVoice(args.reference_model_dir, args.vad_model_dir,
                           device="cpu", fallback_device="cpu")
        result["actual_runtime"] = {"runtime": "PyTorch/FunASR CPU（深度学习框架/语音处理框架中央处理器）",
            "device": model.device, "internal_vad_device": model.internal_vad_device,
            "weight_load_mode_applied": _funasr_effective_load_mode(),
            "model_parameter_dtype": _model_dtype(model),
            "torch_num_threads": int(torch.get_num_threads()),
            "torch_num_interop_threads": int(torch.get_num_interop_threads()),
            "torch_version": str(torch.__version__)}
        result["configuration_verified"] = {
            "cpu_model": model.device == "cpu" and model.internal_vad_device == "cpu",
            "selective_import_requested": os.environ.get("VOICE_ASR_IMPORT_MODE") == "selective",
            "mmap_weight_loader_applied": result["actual_runtime"]["weight_load_mode_applied"] == "mmap",
            "fp32_parameter_confirmed": result["actual_runtime"]["model_parameter_dtype"] in {"torch.float32", "float32"},
        }
        for case in cases:
            base = _serialize_input(case)
            if case["input_status"] != "ok":
                result["cases"].append({**base, "status": "audio_error", "error": case.get("input_error")})
                continue
            try:
                output = model(case["pcm"])
                segments = []
                for segment in output.get("segments", []):
                    start_ms, end_ms = int(segment["start_ms"]), int(segment["end_ms"])
                    start_sample, end_sample = start_ms * 16, end_ms * 16
                    sliced_pcm = case["pcm"][start_sample * 2:end_sample * 2]
                    segments.append({"start_ms": start_ms, "end_ms": end_ms,
                        "start_sample": start_sample, "end_sample": end_sample,
                        "slice_pcm_sha256": _sha256(sliced_pcm),
                        "text": segment.get("text", ""), "language": segment.get("language", "unknown"),
                        "emotion": segment.get("emotion", "unknown"),
                        "events": list(segment.get("events", [])),
                        "raw_tags": list(segment.get("raw_tags", []))})
                result["cases"].append({**base, "status": "ok", "full_text": output.get("text", ""),
                    "no_speech": bool(output.get("no_speech", not bool(output.get("text", "")))),
                    "segment_count": len(segments), "segments": segments,
                    "wrapper_complete": bool(output.get("complete", False))})
            except Exception as exc:
                result["cases"].append({**base, "status": "inference_error",
                    "error": {"type": type(exc).__name__, "message": str(exc),
                              "traceback": traceback.format_exc()}})
        result["success"] = (len(result["cases"]) == len(cases)
            and all(item.get("status") == "ok" for item in result["cases"])
            and result.get("actual_runtime", {}).get("device") == "cpu"
            and result.get("actual_runtime", {}).get("internal_vad_device") == "cpu"
            and all(result.get("configuration_verified", {}).values()))
    except Exception as exc:
        result["model_error"] = {"type": type(exc).__name__, "message": str(exc),
                                  "traceback": traceback.format_exc()}
        result["cases"] = [{**_serialize_input(case), "status": "model_load_error",
                            "error": f"{type(exc).__name__}: {exc}"} for case in cases]
    return result


def _patch_sentencepiece_path_loader() -> str:
    """仅替换当前进程中的 tokenizer 类，使用 Python 字节读取中文 BPE 路径。"""
    import sentencepiece
    import funasr_onnx.sensevoice_bin as sensevoice_bin
    base_class = sensevoice_bin.SentencepiecesTokenizer

    class BytesSentencepiecesTokenizer(base_class):
        def _build_sentence_piece_processor(self):
            if self.sp is None:
                self.sp = sentencepiece.SentencePieceProcessor(
                    model_proto=Path(self.bpemodel).read_bytes())

    sensevoice_bin.SentencepiecesTokenizer = BytesSentencepiecesTokenizer
    return "model_proto=Path(bpemodel).read_bytes()"


def _candidate_worker(args: argparse.Namespace) -> dict[str, Any]:
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[name] = str(args.threads)
    cases, np, _ = _load_cases(args.case_manifest)
    asset = _asset_validation("candidate", args.candidate_model_dir, args.vad_model_dir)
    result: dict[str, Any] = {"side": "candidate_funasr_onnx_int8_cpu", "success": False,
        "device": "cpu", "precision": "INT8（8位整数）model_quant.onnx",
        "threads_requested": args.threads, "asset_validation": asset,
        "sentencepiece_path_workaround": None, "cases": []}
    try:
        reference = json.loads(args.reference_results.read_text(encoding="utf-8"))
        ref_by_id = {item["case_id"]: item for item in reference.get("cases", [])}
    except Exception as exc:
        reference = None
        ref_by_id = {}
        result["reference_results_error"] = f"{type(exc).__name__}: {exc}"
    if not asset["complete"]:
        result["model_error"] = {"type": "MissingLocalAssets", "message": str(asset["missing"])}
        result["cases"] = [{**_serialize_input(case), "status": "asset_error", "error": str(asset["missing"])}
                            for case in cases]
        return result
    if reference is None:
        result["cases"] = [{**_serialize_input(case), "status": "reference_error",
                            "error": result["reference_results_error"]} for case in cases]
        return result
    try:
        from voice_terminal.analysis.sensevoice import parse_transcript
        if not (args.candidate_model_dir / "model_quant.onnx").is_file():
            raise FileNotFoundError("缺少本地 model_quant.onnx，拒绝自动导出")
        result["sentencepiece_path_workaround"] = _patch_sentencepiece_path_loader()
        from funasr_onnx.sensevoice_bin import SenseVoiceSmall
        model = SenseVoiceSmall(model_dir=str(args.candidate_model_dir), quantize=True,
                                device_id="-1", intra_op_num_threads=args.threads)
        session = getattr(getattr(model, "ort_infer", None), "session", None)
        get_providers = getattr(session, "get_providers", None)
        providers = list(get_providers()) if callable(get_providers) else None
        result["actual_runtime"] = {"runtime": "ONNX Runtime（模型推理运行时）",
            "device": "cpu", "providers": providers,
            "model_file": "model_quant.onnx", "intra_op_num_threads_requested": args.threads}
        if providers is None:
            raise RuntimeError("无法读取 ONNX Runtime 实际执行提供程序；设备验证失败")
        if providers != ["CPUExecutionProvider"]:
            raise RuntimeError(f"候选要求仅使用 CPUExecutionProvider（中央处理器执行提供程序），实际为 {providers}")
        result["cases"] = []
        for case in cases:
            base = _serialize_input(case)
            ref = ref_by_id.get(case["case_id"])
            if case["input_status"] != "ok":
                result["cases"].append({**base, "status": "audio_error", "error": case.get("input_error")})
                continue
            if not ref or ref.get("status") != "ok":
                result["cases"].append({**base, "status": "reference_error",
                    "error": (ref or {}).get("error", "参考侧没有此用例或未成功")})
                continue
            if case.get("audio_sha256") != ref.get("audio_sha256") or case.get("pcm_sha256") != ref.get("pcm_sha256"):
                result["cases"].append({**base, "status": "input_mismatch",
                    "error": "候选侧音频摘要与参考侧不同，拒绝比较"})
                continue
            segments = []
            errors = []
            try:
                waveform_all = np.frombuffer(case["pcm"], dtype="<i2").astype(np.float32) / 32768.0
                for reference_segment in ref.get("segments", []):
                    start_sample = int(reference_segment["start_sample"])
                    end_sample = int(reference_segment["end_sample"])
                    start_ms = int(reference_segment["start_ms"])
                    end_ms = int(reference_segment["end_ms"])
                    slice_pcm = case["pcm"][start_sample * 2:end_sample * 2]
                    waveform = waveform_all[start_sample:end_sample]
                    try:
                        outputs = model(waveform, language="auto", textnorm="withitn")
                        raw_tagged = outputs[0] if isinstance(outputs, list) and outputs else ""
                        parsed = parse_transcript(raw_tagged)
                        segments.append({"start_ms": start_ms, "end_ms": end_ms,
                            "start_sample": start_sample, "end_sample": end_sample,
                            "slice_pcm_sha256": _sha256(slice_pcm),
                            "raw_tagged_output": raw_tagged,
                            "raw_tagged_outputs": outputs if isinstance(outputs, list) else None,
                            "text": parsed["text"], "language": parsed["language"],
                            "emotion": parsed["emotion"], "events": parsed["events"],
                            "raw_tags": parsed["raw_tags"],
                            "output_count": len(outputs) if isinstance(outputs, list) else None})
                        if not isinstance(outputs, list) or len(outputs) != 1:
                            errors.append(f"单条VAD切片应返回1个结果，实际为 {len(outputs) if isinstance(outputs, list) else '非列表'}")
                    except Exception as exc:
                        errors.append(f"{type(exc).__name__}: {exc}")
                        segments.append({"start_ms": start_ms, "end_ms": end_ms,
                            "start_sample": start_sample, "end_sample": end_sample,
                            "slice_pcm_sha256": _sha256(slice_pcm), "status": "inference_error",
                            "error": {"type": type(exc).__name__, "message": str(exc),
                                      "traceback": traceback.format_exc()}})
                full_text = "".join(segment.get("text", "") for segment in segments)
                result["cases"].append({**base, "status": "inference_error" if errors else "ok",
                    "full_text": full_text, "no_speech": not bool(full_text),
                    "model_inference_performed": bool(ref.get("segments")),
                    "no_speech_basis": "shared_reference_vad_no_segments" if not ref.get("segments")
                        else "candidate_full_text_empty",
                    "segment_count": len(segments), "segments": segments,
                    "errors": errors, "reference_segment_count": ref.get("segment_count")})
            except Exception as exc:
                result["cases"].append({**base, "status": "inference_error",
                    "error": {"type": type(exc).__name__, "message": str(exc),
                              "traceback": traceback.format_exc()}})
        result["success"] = (len(result["cases"]) == len(cases)
                             and all(item.get("status") == "ok" for item in result["cases"])
                             and providers == ["CPUExecutionProvider"])
    except Exception as exc:
        result["model_error"] = {"type": type(exc).__name__, "message": str(exc),
                                  "traceback": traceback.format_exc()}
        result["cases"] = [{**_serialize_input(case), "status": "model_load_error",
                            "error": f"{type(exc).__name__}: {exc}"} for case in cases]
    return result


def _tags(segments: list[dict[str, Any]], key: str) -> list[Any]:
    if key == "events":
        return [event for segment in segments for event in segment.get("events", [])]
    return [segment.get(key) for segment in segments]


def _compare_case(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    execution_succeeded = reference.get("status") == "ok" and candidate.get("status") == "ok"
    result: dict[str, Any] = {"case_id": reference.get("case_id"),
        "input_sha256_equal": reference.get("audio_sha256") == candidate.get("audio_sha256"),
        "pcm_sha256_equal": reference.get("pcm_sha256") == candidate.get("pcm_sha256"),
        "manifest_audio_hashes_valid": reference.get("manifest_hash_matches") is True
            and candidate.get("manifest_hash_matches") is True,
        "reference_status": reference.get("status"), "candidate_status": candidate.get("status"),
        "execution_succeeded": execution_succeeded}
    if not execution_succeeded:
        result.update({"passed": False, "semantic_match": None, "failure_type": "execution_error",
                       "error": "任一侧模型、输入或推理执行异常；详细异常在各侧用例记录中"})
        return result
    left_segments, right_segments = reference.get("segments", []), candidate.get("segments", [])
    result.update({
        "full_text_equal": reference.get("full_text") == candidate.get("full_text"),
        "no_speech_equal": reference.get("no_speech") == candidate.get("no_speech"),
        "segment_count_equal": reference.get("segment_count") == candidate.get("segment_count"),
        "language_tags_equal": _tags(left_segments, "language") == _tags(right_segments, "language"),
        "emotion_tags_equal": _tags(left_segments, "emotion") == _tags(right_segments, "emotion"),
        "event_tags_equal": _tags(left_segments, "events") == _tags(right_segments, "events"),
        "raw_tags_equal": _tags(left_segments, "raw_tags") == _tags(right_segments, "raw_tags"),
    })
    segment_results = []
    for index in range(max(len(left_segments), len(right_segments))):
        left = left_segments[index] if index < len(left_segments) else None
        right = right_segments[index] if index < len(right_segments) else None
        if left is None or right is None:
            segment_results.append({"index": index, "exists_on_both_sides": False,
                                    "reference": left, "candidate": right})
        else:
            segment_results.append({"index": index, "exists_on_both_sides": True,
                "same_vad_slice": left.get("start_sample") == right.get("start_sample")
                                  and left.get("end_sample") == right.get("end_sample"),
                "same_slice_pcm": left.get("slice_pcm_sha256") == right.get("slice_pcm_sha256"),
                "text_equal": left.get("text") == right.get("text"),
                "language_equal": left.get("language") == right.get("language"),
                "emotion_equal": left.get("emotion") == right.get("emotion"),
                "events_equal": left.get("events") == right.get("events"),
                "raw_tags_equal": left.get("raw_tags") == right.get("raw_tags"),
                "reference": {key: left.get(key) for key in ("start_ms", "end_ms", "text", "language", "emotion", "events", "raw_tags")},
                "candidate": {key: right.get(key) for key in ("start_ms", "end_ms", "text", "language", "emotion", "events", "raw_tags")}})
    result["segment_comparisons"] = segment_results
    bool_fields = [key for key, value in result.items() if key.endswith("_equal")]
    segment_match = all(item.get("exists_on_both_sides") is True
                        and item.get("same_vad_slice") is True
                        and item.get("same_slice_pcm") is True
                        and item.get("text_equal") is True
                        and item.get("language_equal") is True
                        and item.get("emotion_equal") is True
                        and item.get("events_equal") is True
                        and item.get("raw_tags_equal") is True for item in segment_results)
    semantic_match = all(result.get(name) is True for name in bool_fields) and segment_match
    input_match = (result.get("input_sha256_equal") is True
                   and result.get("pcm_sha256_equal") is True
                   and result.get("manifest_audio_hashes_valid") is True)
    result["semantic_match"] = semantic_match
    result["failure_type"] = "none" if semantic_match and input_match else (
        "input_mismatch" if not input_match else "semantic_mismatch")
    result["passed"] = semantic_match and input_match
    return result


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker-side", choices=("reference", "candidate"), help=argparse.SUPPRESS)
    parser.add_argument("--program-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--case-manifest", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reference-results", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reference-model-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--vad-model-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--candidate-model-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--threads", type=int, default=2, help=argparse.SUPPRESS)
    parser.add_argument("--interop-threads", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    return parser


def _run_worker(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(args.program_root.resolve()))
    if args.worker_side == "reference":
        result = _reference_worker(args)
    else:
        result = _candidate_worker(args)
    args.worker_output.parent.mkdir(parents=True, exist_ok=True)
    args.worker_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"side": result.get("side"), "success": result.get("success"),
                      "case_count": len(result.get("cases", []))}, ensure_ascii=False))
    return 0 if result.get("success") else 2


def _user_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SenseVoice FP32 FunASR 与 INT8 ONNX 的本地语义质量验收")
    parser.add_argument("--output", type=Path, help="最终 JSON（数据交换格式）报告；正常执行时必需")
    parser.add_argument("--program-root", type=Path, default=ROOT, help="当前应用源码根目录")
    parser.add_argument("--model-root", type=Path, default=DEFAULT_MODEL_ROOT,
                        help="本机 Paraformer、FSMN-VAD 与 KWS 测试音频的资产根目录")
    parser.add_argument("--reference-model-dir", type=Path, help="FunASR FP32 SenseVoice 模型目录")
    parser.add_argument("--vad-model-dir", type=Path, help="参考包装器内部 FSMN-VAD 模型目录")
    parser.add_argument("--candidate-model-dir", type=Path, default=DEFAULT_CANDIDATE_MODEL,
                        help="已下载的 funasr_onnx INT8 SenseVoice 模型目录；缺件不下载、不导出")
    parser.add_argument("--asr-example", type=Path, help="默认 Paraformer 示例 WAV 的替代路径")
    parser.add_argument("--vad-example", type=Path, help="默认 FSMN-VAD 示例 WAV 的替代路径")
    parser.add_argument("--kws-test-wavs-dir", type=Path, help="KWS test_wavs（关键字测试音频）目录替代路径")
    parser.add_argument("--threads", type=int, default=2, help="FunASR与ONNX的共同线程预算")
    parser.add_argument("--interop-threads", type=int, default=1, help="参考侧PyTorch算子间线程数")
    parser.add_argument("--python-executable", type=Path, default=Path(sys.executable),
                        help="执行隔离子进程的Python解释器，须安装项目运行依赖")
    parser.add_argument("--list-cases", action="store_true", help="仅列用例与文件SHA，不启动模型")
    parser.add_argument("--dry-run", action="store_true", help="展示串行进程配置但不启动模型")
    parser.add_argument("--stop-on-reference-error", action="store_true",
                        help="参考侧失败时跳过候选模型执行，但仍记录所有用例")
    return parser


def _main_user(args: argparse.Namespace) -> int:
    args.program_root = args.program_root.resolve()
    args.model_root = args.model_root.resolve()
    args.reference_model_dir = (args.reference_model_dir or args.model_root / "analysis" / "sensevoice").resolve()
    args.vad_model_dir = (args.vad_model_dir or args.model_root / "funasr" / "fsmn-vad").resolve()
    args.candidate_model_dir = args.candidate_model_dir.resolve()
    cases = _case_paths(args)
    if args.list_cases:
        output = [{key: value for key, value in item.items() if key != "audio_path"}
                  | {"audio_path": item.get("audio_path")} for item in cases]
        print(json.dumps({"case_count": len(cases), "cases": output,
                          "field_descriptions_zh": FIELD_DESCRIPTIONS_ZH}, ensure_ascii=False, indent=2))
        return 0
    if args.threads < 1 or args.interop_threads < 1:
        raise SystemExit("线程数必须为正数")
    if not args.output:
        raise SystemExit("正常对照执行需要 --output 报告路径")
    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    reference_asset = _asset_validation("reference", args.reference_model_dir, args.vad_model_dir)
    candidate_asset = _asset_validation("candidate", args.candidate_model_dir, args.vad_model_dir)

    with tempfile.TemporaryDirectory(prefix="sensevoice-quality-") as temp_name:
        temp = Path(temp_name)
        silence_path = temp / "silence_1s.wav"
        silence_bytes = _wav_silence_bytes(1)
        silence_path.write_bytes(silence_bytes)
        cases = _case_paths(args)
        for item in cases:
            if item["source_kind"] == "generated_silence":
                item["audio_path"] = str(silence_path.resolve())
        manifest_path = temp / "cases.json"
        manifest_path.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
        reference_path = temp / "reference.json"
        candidate_path = temp / "candidate.json"

        def worker_command(side: str, worker_output: Path) -> list[str]:
            command = [str(args.python_executable.resolve()), str(Path(__file__).resolve()),
                "--worker-side", side, "--program-root", str(args.program_root),
                "--case-manifest", str(manifest_path), "--reference-model-dir", str(args.reference_model_dir),
                "--vad-model-dir", str(args.vad_model_dir), "--candidate-model-dir", str(args.candidate_model_dir),
                "--threads", str(args.threads), "--interop-threads", str(args.interop_threads),
                "--worker-output", str(worker_output)]
            if side == "candidate":
                command.extend(("--reference-results", str(reference_path)))
            return command

        commands = {"reference": worker_command("reference", reference_path),
                    "candidate": worker_command("candidate", candidate_path)}
        final: dict[str, Any] = {
            "description_zh": "当前FunASR FP32 CPU包装器为参考；已下载的funasr_onnx INT8 CPU模型为候选。",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "execution_order": "reference_then_candidate; 每侧全新进程且严格串行",
            "program_root": str(args.program_root),
            "reference_asset_validation": reference_asset,
            "candidate_asset_validation": candidate_asset,
            "threads": {"intra_op": args.threads, "reference_inter_op": args.interop_threads},
            "case_manifest": [{key: value for key, value in item.items() if key != "audio_path"}
                               | {"audio_path": item["audio_path"]} for item in cases],
            "field_descriptions_zh": FIELD_DESCRIPTIONS_ZH,
            "success": False,
        }
        if args.dry_run:
            final["dry_run"] = True
            final["commands"] = commands
            print(json.dumps(final, ensure_ascii=False, indent=2))
            return 0

        sides: dict[str, Any] = {}
        for side, command, report_path in (("reference", commands["reference"], reference_path),
                                           ("candidate", commands["candidate"], candidate_path)):
            if side == "candidate" and args.stop_on_reference_error and not sides.get("reference", {}).get("success"):
                sides[side] = {"side": side, "success": False, "skipped": True,
                               "error": "参考侧失败，按 --stop-on-reference-error 跳过模型执行",
                               "cases": [{"case_id": item["case_id"], "status": "skipped"} for item in cases]}
                continue
            env = os.environ.copy()
            env.update({"OMP_NUM_THREADS": str(args.threads), "MKL_NUM_THREADS": str(args.threads),
                        "OPENBLAS_NUM_THREADS": str(args.threads), "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1", "MODELSCOPE_OFFLINE": "1"})
            try:
                completed = subprocess.run(command, cwd=args.program_root, env=env,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
                return_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
            except Exception as exc:
                return_code, stdout, stderr = -1, "", f"{type(exc).__name__}: {exc}"
            if report_path.is_file():
                try:
                    side_result = json.loads(report_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    side_result = {"side": side, "success": False, "cases": [],
                                   "error": f"JSON报告解析失败：{type(exc).__name__}: {exc}"}
            else:
                side_result = {"side": side, "success": False, "cases": [],
                               "error": "子进程没有写出结果报告"}
            side_result["exit_code"] = return_code
            side_result["stdout"] = stdout[-8000:]
            side_result["stderr"] = stderr[-8000:]
            side_result["process_return_code_ok"] = return_code == 0
            rows_by_id = {row.get("case_id"): row for row in side_result.get("cases", [])
                          if isinstance(row, dict)}
            complete_rows = []
            for item in cases:
                row = rows_by_id.get(item["case_id"])
                if row is None:
                    row = {"case_id": item["case_id"], "label_zh": item["label_zh"],
                           "audio_path": item["audio_path"], "audio_sha256": item.get("audio_sha256_expected"),
                           "pcm_sha256": None, "status": "worker_error",
                           "error": side_result.get("error", "子进程异常退出且未记录此用例")}
                complete_rows.append(row)
            side_result["cases"] = complete_rows
            sides[side] = side_result

        final["sides"] = sides
        reference_by_id = {item.get("case_id"): item for item in sides.get("reference", {}).get("cases", [])}
        candidate_by_id = {item.get("case_id"): item for item in sides.get("candidate", {}).get("cases", [])}
        case_comparisons = []
        for item in cases:
            case_id = item["case_id"]
            left, right = reference_by_id.get(case_id), candidate_by_id.get(case_id)
            if left is None or right is None:
                case_comparisons.append({"case_id": case_id, "passed": False,
                                         "error": "某一侧报告缺少该用例"})
            else:
                case_comparisons.append(_compare_case(left, right))
        final["comparison"] = {"cases": case_comparisons,
            "case_count": len(case_comparisons),
            "passed_case_count": sum(bool(item.get("passed")) for item in case_comparisons),
            "failed_case_ids": [item.get("case_id") for item in case_comparisons if not item.get("passed")],
            "execution_error_case_ids": [item.get("case_id") for item in case_comparisons
                if item.get("failure_type") == "execution_error"],
            "semantic_mismatch_case_ids": [item.get("case_id") for item in case_comparisons
                if item.get("failure_type") == "semantic_mismatch"],
            "all_model_executions_succeeded": all(item.get("execution_succeeded") is True
                for item in case_comparisons),
            "all_audio_hashes_equal": all(item.get("input_sha256_equal") is True
                and item.get("pcm_sha256_equal") is True for item in case_comparisons),
            "all_semantic_outputs_equal": bool(case_comparisons)
                and all(item.get("semantic_match") is True for item in case_comparisons)}
        final["success"] = (len(case_comparisons) == len(cases)
            and all(item.get("passed") is True for item in case_comparisons)
            and sides.get("reference", {}).get("success") is True
            and sides.get("candidate", {}).get("success") is True)
        output_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output_path), "success": final["success"],
                          "case_count": len(case_comparisons),
                          "passed_case_count": final["comparison"]["passed_case_count"]}, ensure_ascii=False))
        return 0 if final["success"] else 2


def main() -> int:
    if "--worker-side" in sys.argv[1:]:
        worker_args = _worker_parser().parse_args()
        missing = [name for name in ("case_manifest", "reference_model_dir", "vad_model_dir",
                    "candidate_model_dir", "worker_output") if getattr(worker_args, name, None) is None]
        if worker_args.worker_side == "candidate" and worker_args.reference_results is None:
            missing.append("reference_results")
        if missing:
            raise SystemExit("内部 worker 参数缺失：" + ", ".join(missing))
        return _run_worker(worker_args)
    args = _user_parser().parse_args()
    return _main_user(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""固定本地录音对照：空最终刷新、短输入、分块边界与重置。"""
import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vad_validation_support import build_vad_detector


def _build_detector(model_dir: Path, backend: str):
    detector = build_vad_detector(
        model_dir=model_dir,
        expected_backend=backend,
        device="cpu",
        end_silence_ms=200,
    )
    if detector.backend != backend:
        raise AssertionError(
            f"requested backend（要求后端）={backend!r}, "
            f"actual backend（实际后端）={detector.backend!r}"
        )
    return detector


def main() -> int:
    parser = argparse.ArgumentParser(description="流式检测功能对照（不访问麦克风）")
    parser.add_argument("--backend", choices=("funasr", "onnx"), required=True,
                        help="backend（检测后端）：明确选择原语音框架或轻量交换格式推理。")
    parser.add_argument("--model-dir", type=Path, required=True, help="model-dir（本次检测模型目录）。")
    parser.add_argument("--model-root", type=Path, default=__import__('voice_terminal.paths', fromlist=['AppPaths']).AppPaths.resolve().install_root / 'models',
                        help="model-root（录音来源模型根目录）。")
    parser.add_argument("--output", type=Path, required=True, help="output（流式验收报告位置）。")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    import soundfile as sf
    detector = _build_detector(args.model_dir, args.backend)
    def audio(path):
        values, rate = sf.read(path, dtype="int16"); assert rate == 16000
        return values.tobytes()
    asr = audio(args.model_root / "funasr/paraformer-zh-streaming/example/asr_example.wav")
    vad = audio(args.model_root / "funasr/fsmn-vad/example/vad_example.wav")
    cases = {"empty": b"", "silence20ms": bytes(640), "silence200ms": bytes(6400),
             "short10ms": asr[:320], "asr": asr, "vad": vad, "active_truncated": asr[:80000]}
    result = {
        "backend": args.backend,
        "backend_zh": "本次流式验收所用推理后端",
        "backend_values_zh": {"funasr": "FunASR（语音识别框架）后端", "onnx": "ONNX（开放神经网络交换格式）后端"},
        "sample_rate": 16000,
        "sample_rate_zh": "测试录音的采样率，单位为赫兹",
        "chunk_sizes_bytes": [640, 1280, 6400],
        "chunk_sizes_bytes_zh": "每次送入检测器的 PCM（脉冲编码调制）字节数，对应 20、40、200 毫秒",
        "final_modes_zh": {"final_chunk": "最后一个音频数据块携带最终标记", "empty_final": "音频数据之后另发空最终刷新"},
        "event_fields_zh": {
            "is_speech": "当前是否处于语音活动状态",
            "speech_started": "本次输入是否检测到语音开始",
            "speech_ended": "本次输入是否检测到语音结束",
            "speech_end_sample": "语音结束位置，单位为采样点；null 表示无事件",
            "speech_start_sample": "语音开始位置，单位为采样点；null 表示无事件",
            "feed_offset": "本次事件所在音频块在输入中的字节偏移",
        },
        "cases_zh": "按输入类型、分块大小和最终刷新方式索引的流式测试结果",
        "cases": {},
    }
    results = result["cases"]
    for name, pcm in cases.items():
        for chunk_bytes in (640, 1280, 6400):
            for final_mode in ("final_chunk", "empty_final"):
                detector.reset(); events = []
                key = f"{name}/{chunk_bytes}/{final_mode}"
                try:
                    for offset in range(0, len(pcm), chunk_bytes):
                        end = min(offset + chunk_bytes, len(pcm))
                        activity = detector.feed(pcm[offset:offset+chunk_bytes],
                            is_final=final_mode == "final_chunk" and end >= len(pcm))
                        if activity.speech_started or activity.speech_ended:
                            events.append({**asdict(activity), "feed_offset": offset})
                    # 空音频没有可标记为 final（最终）的数据块，因此两种模式都显式发送空刷新；
                    # 其他 empty_final（空最终刷新）用例则在所有音频块之后发送空刷新。
                    if not pcm or final_mode == "empty_final":
                        activity = detector.feed(b"", is_final=True)
                        if activity.speech_started or activity.speech_ended:
                            events.append({**asdict(activity), "feed_offset": len(pcm)})
                    assert not detector._active
                    detector.reset(); assert not detector._cache and not detector._active
                    results[key] = {
                        "events": events,
                        "events_zh": "检测到的语音开始/结束事件",
                        "error": None,
                        "error_zh": "测试异常文本；null 表示该组合通过",
                    }
                except Exception as exc:
                    detector.reset()
                    results[key] = {
                        "events": events,
                        "events_zh": "异常发生前检测到的语音开始/结束事件",
                        "error": f"{type(exc).__name__}: {exc}",
                        "error_zh": "该输入/分块/刷新组合的实际异常文本",
                    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    errors = [name for name, value in results.items() if value["error"]]
    print(json.dumps({"backend": args.backend, "cases": len(results), "errors": errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

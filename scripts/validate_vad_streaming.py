"""固定本地录音对照：空最终刷新、短输入、分块边界与重置。"""
import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())))


def main():
    parser = argparse.ArgumentParser(description="流式检测功能对照（不访问麦克风）")
    parser.add_argument("--backend", choices=("funasr", "onnx"), required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, default=__import__('voice_terminal.paths', fromlist=['AppPaths']).AppPaths.resolve().install_root / 'models')
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    import numpy as np
    import soundfile as sf
    from voice_terminal.vad.detector import FsmnVadDetector
    if args.backend == "funasr":
        detector = FsmnVadDetector(model_dir=args.model_dir, device="cpu", end_silence_ms=200)
    else:
        from voice_terminal.vad.onnx_backend import OnnxStreamingModel
        detector = FsmnVadDetector.__new__(FsmnVadDetector)
        detector._np = np; detector._model = OnnxStreamingModel(args.model_dir)
        detector._cache = {}; detector._active = False; detector._model_chunk_ms = 200
        detector._feed_count = 0; detector._samples = 0; detector.sample_rate = 16000; detector.end_silence_ms = 200
    def audio(path):
        values, rate = sf.read(path, dtype="int16"); assert rate == 16000
        return values.tobytes()
    asr = audio(args.model_root / "funasr/paraformer-zh-streaming/example/asr_example.wav")
    vad = audio(args.model_root / "funasr/fsmn-vad/example/vad_example.wav")
    cases = {"empty": b"", "silence20ms": bytes(640), "silence200ms": bytes(6400),
             "short10ms": asr[:320], "asr": asr, "vad": vad, "active_truncated": asr[:80000]}
    result = {}
    for name, pcm in cases.items():
        for chunk_bytes in (640, 1280, 6400):
            for empty_final in (False, True):
                detector.reset(); events = []
                key = f"{name}/{chunk_bytes}/{'empty_final' if empty_final else 'final_chunk'}"
                try:
                    for offset in range(0, len(pcm), chunk_bytes):
                        activity = detector.feed(pcm[offset:offset+chunk_bytes],
                            is_final=not empty_final and offset+chunk_bytes >= len(pcm))
                        if activity.speech_started or activity.speech_ended:
                            events.append({**asdict(activity), "feed_offset": offset})
                    if empty_final or not pcm:
                        activity = detector.feed(b"", is_final=True)
                        if activity.speech_started or activity.speech_ended:
                            events.append({**asdict(activity), "feed_offset": len(pcm)})
                    assert not detector._active
                    detector.reset(); assert not detector._cache and not detector._active
                    result[key] = {"events": events, "error": None}
                except Exception as exc:
                    result[key] = {"events": events, "error": f"{type(exc).__name__}: {exc}"}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(result), "errors": [name for name, value in result.items() if value["error"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

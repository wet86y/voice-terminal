"""运行资源对照：每次独立进程，真实权重、固定音频与可复测原始结果。"""
from __future__ import annotations
import argparse
import asyncio
from collections import deque
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="真实模型运行资源对照（不访问麦克风或云端）")
    parser.add_argument("--role", choices=("vad", "speaker", "asr", "refine", "endpoint"), required=True)
    parser.add_argument("--threads", type=int)
    parser.add_argument("--onnx", action="store_true")
    parser.add_argument("--model-root", type=Path, default=__import__('voice_terminal.paths', fromlist=['AppPaths']).AppPaths.resolve().install_root / 'models')
    parser.add_argument("--vad-dir", type=Path)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--soak-seconds", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.threads:
        for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ[name] = str(args.threads)
    logging.disable(logging.CRITICAL)
    import psutil
    process = psutil.Process()
    started = time.perf_counter()
    import numpy as np
    import soundfile as sf
    pcm, rate = sf.read(args.model_root / "funasr/paraformer-zh-streaming/example/asr_example.wav", dtype="int16")
    assert rate == 16000
    pcm = pcm.tobytes()
    vad_dir = args.vad_dir or args.model_root / "funasr/fsmn-vad"
    if args.role == "vad":
        from voice_terminal.vad.detector import FsmnVadDetector
        if args.onnx:
            # 原后端与候选复用相同事件适配层，直接对照推理与前端实现。
            from voice_terminal.vad.onnx_backend import OnnxStreamingModel
            detector = FsmnVadDetector.__new__(FsmnVadDetector)
            detector._np = np; detector._model = OnnxStreamingModel(vad_dir)
            detector._cache = {}; detector._active = False; detector._model_chunk_ms = 200
            detector._feed_count = 0; detector._samples = 0
            detector.sample_rate = 16000; detector.end_silence_ms = 200
        else:
            detector = FsmnVadDetector(model_dir=vad_dir, device="cpu", end_silence_ms=200)
        def run(data):
            detector.reset(); events = []
            for offset in range(0, len(data), 640):
                value = detector.feed(data[offset:offset+640], is_final=offset+640 >= len(data))
                if value.speech_started or value.speech_ended:
                    events.append({**asdict(value), "feed_offset": offset})
            detector.feed(b"", is_final=True)
            return events
    elif args.role == "speaker":
        from voice_terminal.speaker.engine import WeSpeakerOnnxEngine
        model = WeSpeakerOnnxEngine(args.model_root / "speaker/wespeaker-cnceleb-resnet34/cnceleb_resnet34.onnx", device="cpu")
        model.load()
        def run(data):
            return list(model.embed_pcm(data[:64000]))
    elif args.role == "asr":
        from voice_terminal.asr.worker import AsrWorkerEngine
        model = AsrWorkerEngine(model_dir=args.model_root / "funasr/paraformer-zh-streaming", device="cuda")
        model.load()
        counter = 0
        async def infer(data):
            nonlocal counter
            counter += 1; turn = str(counter); result = None
            for offset in range(0, len(data), 640):
                result = await model.infer(turn, data[offset:offset+640], offset+640 >= len(data))
            return result.text
        def run(data):
            return asyncio.run(infer(data))
    elif args.role == "refine":
        from voice_terminal.analysis.sensevoice import SenseVoice
        model = SenseVoice(args.model_root / "analysis/sensevoice", args.model_root / "funasr/fsmn-vad", device="cuda")
        run = model
    else:
        from voice_terminal.analysis.smart_turn import SmartTurn
        model = SmartTurn(args.model_root / "analysis/smart-turn", device="cpu")
        run = model
    load_ms = (time.perf_counter() - started) * 1000
    for _ in range(3):
        run(pcm)
    samples, results, memory_samples = [], [], []
    for _ in range(args.iterations):
        before = time.perf_counter(); result = run(pcm)
        samples.append((time.perf_counter() - before) * 1000); results.append(result)
        memory = process.memory_info()
        memory_samples.append({"private_commit_bytes": getattr(memory, "private", None), "rss_bytes": memory.rss,
                               "thread_count": process.num_threads()})
    soak = None
    if args.soak_seconds:
        if args.role != "vad":
            raise ValueError("长时间实时回放目前仅适用于语音检测")
        vad_pcm, rate = sf.read(args.model_root / "funasr/fsmn-vad/example/vad_example.wav", dtype="int16")
        assert rate == 16000
        vad_pcm = vad_pcm.tobytes()
        latency = deque(maxlen=8192); memory_history = []; events_digest = hashlib.sha256()
        start = time.monotonic(); deadline = start + args.soak_seconds; offset = frames = rounds = 0
        detector.reset()
        while time.monotonic() < deadline:
            chunk = vad_pcm[offset:offset+640]; final = offset+640 >= len(vad_pcm)
            before = time.perf_counter(); event = detector.feed(chunk, is_final=final)
            latency.append((time.perf_counter()-before)*1000)
            if event.speech_started or event.speech_ended:
                events_digest.update(json.dumps(asdict(event), sort_keys=True).encode())
            frames += 1; offset += len(chunk)
            if final:
                rounds += 1; offset = 0; detector.reset()
            if frames % 500 == 0:
                memory = process.memory_info()
                memory_history.append({"elapsed_seconds": time.monotonic()-start,
                                       "private_commit_bytes": getattr(memory,"private",None), "rss_bytes": memory.rss})
            time.sleep(max(0, start + frames * .02 - time.monotonic()))
        soak = {"elapsed_seconds": time.monotonic()-start, "frames": frames, "rounds": rounds,
                "event_digest": events_digest.hexdigest(), "latency_ms": percentile(list(latency)), "memory_samples": memory_history}
    payload = {"role": args.role, "baseline_source": False, "onnx": args.onnx,
               "thread_budget": args.threads, "python": sys.executable, "load_ms": load_ms,
               "latency_ms": percentile(samples), "samples_ms": samples, "results": results,
               "memory_samples": memory_samples, "soak": soak, "torch_imported": "torch" in sys.modules}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "role": args.role, "load_ms": load_ms,
                      "latency_ms": payload["latency_ms"], "torch_imported": payload["torch_imported"]}, ensure_ascii=False))


def percentile(values):
    ordered = sorted(values)
    return {"p50": statistics.median(values), "p95": ordered[min(len(ordered)-1, int(len(ordered)*.95))],
            "p99": ordered[min(len(ordered)-1, int(len(ordered)*.99))]}


if __name__ == "__main__":
    main()

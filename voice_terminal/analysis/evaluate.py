"""Reproducible offline comparison; synthetic results are never human acceptance."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import time

from .client import AnalysisClient
from ..asr.worker import AsrWorkerEngine


def cer(reference: str, hypothesis: str) -> float:
    reference, hypothesis = [re.sub(r"[^\w]", "", text).casefold() for text in (reference, hypothesis)]
    previous = list(range(len(hypothesis) + 1))
    for i, a in enumerate(reference, 1):
        row = [i]
        for j, b in enumerate(hypothesis, 1):
            row.append(min(previous[j] + 1, row[-1] + 1, previous[j - 1] + (a != b)))
        previous = row
    return previous[-1] / max(1, len(reference))


async def evaluate(args):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    from math import gcd
    import psutil
    import torch
    entries = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    engine = AsrWorkerEngine(model_dir=args.model_dir / "funasr/paraformer-zh-streaming",
                             device="cuda" if torch.cuda.is_available() else "cpu")
    await asyncio.to_thread(engine.load)
    client = AnalysisClient(args.url)
    rows = []
    process = psutil.Process()
    peak_rss = process.memory_info().rss
    for index, item in enumerate(entries):
        audio, rate = sf.read(item["path"], dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if rate != 16000:
            factor = gcd(rate, 16000)
            audio = resample_poly(audio, 16000 // factor, rate // factor)
        duration_s = len(audio) / 16000
        limit_s = 62.0 if args.allow_over_max else 60.0
        if duration_s > limit_s:
            rows.append({"name": item["name"], "duration_s": duration_s,
                         "error": f"fixture exceeds {limit_s:g} seconds"})
            continue
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        turn = f"eval-{index}"
        start = time.perf_counter()
        for offset in range(0, len(pcm), 19200):
            await engine.infer(turn, pcm[offset:offset + 19200], False)
        draft = (await engine.infer(turn, b"", True)).text
        asr_ms = (time.perf_counter() - start) * 1000
        result = await client.analyze("refine", pcm, turn, 0, 60000)
        endpoint = await client.analyze("endpoint", pcm[-256000:] + bytes(700 * 32), turn, 0, 5000)
        peak_rss = max(peak_rss, process.memory_info().rss)
        segments = result["result"].get("segments", [])
        ranges = [(int(part.get("start_ms", -1)), int(part.get("end_ms", -1))) for part in segments]
        bounded = all(0 <= start < end <= round(duration_s * 1000) and end - start <= 30000
                      for start, end in ranges)
        ordered = bounded and all(index == 0 or start >= ranges[index - 1][1]
                                  for index, (start, end) in enumerate(ranges))
        row = {"name": item["name"], "source": item.get("source", "user-supplied"), "duration_s": duration_s,
               "paraformer": draft, "refined": result["result"]["text"], "reference": item["reference"],
               "paraformer_cer": cer(item["reference"], draft), "sensevoice_cer": cer(item["reference"], result["result"]["text"]),
               "asr_compute_ms": asr_ms, "sensevoice_ms": result["elapsed_ms"],
               "refine_exceeds_default_budget": result["elapsed_ms"] > 3000,
               "endpoint_probability": endpoint["result"]["probability"], "smart_turn_ms": endpoint["elapsed_ms"],
               "segments": segments, "segment_ordered": ordered, "segments_bounded": bounded,
               "max_segment_ms": max((end - start for start, end in ranges), default=0),
               "min_segment_ms": min((end - start for start, end in ranges), default=0),
               "fallback": False}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    report = {"kind": "offline audio replay, not microphone acceptance", "device": engine.device,
              "replay_process_peak_rss_bytes": peak_rss, "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".local/cache/evaluation/report.json"))
    parser.add_argument("--model-dir", type=Path, default=__import__('voice_terminal.paths', fromlist=['AppPaths']).AppPaths.resolve().install_root / 'models')
    parser.add_argument("--url", default="ws://127.0.0.1:8767/analysis")
    parser.add_argument("--allow-over-max", action="store_true",
                        help="仅离线分段验证时允许处理 60-62 秒输入；运行时仍限制为 60 秒")
    asyncio.run(evaluate(parser.parse_args()))


if __name__ == "__main__":
    main()

"""真实权重回放：VAD（语音活动检测）闭段、双路识别和独立分析进程并发。

使用项目私有 Python（解释器）运行；默认每种识别模式三次回放，不调用在线模型。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import multiprocessing
from pathlib import Path
import socket
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def analysis_process(port: int):
    logging.disable(logging.CRITICAL)
    import uvicorn
    from voice_terminal.analysis.perception import Emotion2vec, Yamnet
    from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app
    lanes = {}
    for name, factory in (
        ("emotion", lambda: Emotion2vec(ROOT / ".local/models/analysis/emotion2vec-plus-base")),
        ("acoustic", lambda: Yamnet(ROOT / ".local/models/analysis/yamnet")),
    ):
        lanes[name] = AnalysisLane()
        lanes[name].prepare(factory)
    uvicorn.run(create_analysis_app(lanes), host="127.0.0.1", port=port, log_level="error")


async def run(args):
    logging.disable(logging.CRITICAL)
    import psutil
    import soundfile as sf
    import websockets
    from voice_terminal.analysis.client import AnalysisClient
    from voice_terminal.asr.client import AsrWorkerClient
    from voice_terminal.asr.protocol import AudioChunk, AudioSegment
    from voice_terminal.asr.worker import AsrWorkerEngine, handle_asr_connection
    from voice_terminal.audio_enhancements import AudioEnhancements
    from voice_terminal.controller.enhancements import AudioEnhancementsMixin
    from voice_terminal.controller.turn_workspace import AudioFrame
    from voice_terminal.vad.detector import FsmnVadDetector

    audio, rate = sf.read(args.audio, dtype="int16", always_2d=False)
    if rate != 16000 or audio.ndim != 1:
        raise ValueError("固定音频必须是 16 千赫兹单声道")
    pcm = audio.tobytes() + b"\0\0" * 16000
    engine = AsrWorkerEngine(model_dir=ROOT / ".local/models/funasr/paraformer-zh-streaming",
        device=args.device, offline_model_dir=ROOT / ".local/models/funasr/paraformer-zh-offline")
    await engine.prepare()
    vad = FsmnVadDetector(model_dir=ROOT / ".local/models/funasr/fsmn-vad", end_silence_ms=500)
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
    process = multiprocessing.get_context("spawn").Process(target=analysis_process, args=(port,))
    process.start()
    analysis = AnalysisClient(f"ws://127.0.0.1:{port}/analysis")
    try:
        for attempt in range(120):
            try:
                await analysis.health()
                break
            except Exception:
                if not process.is_alive():
                    raise RuntimeError("分析进程加载失败")
                await asyncio.sleep(.5)
        else:
            raise TimeoutError("分析进程启动超时")
        perception = AudioEnhancementsMixin()
        perception._init_enhancements(AudioEnhancements(False, True, True))
        perception.speaker_mode = "off"
        perception.analysis = analysis
        rows, cursor = [], 0
        emotion_durations, acoustic_durations = [], []
        original_analyze = analysis.analyze
        async def measured(operation, *values, **options):
            reply = await original_analyze(operation, *values, **options)
            (emotion_durations if operation == "emotion" else acoustic_durations).append(reply["elapsed_ms"])
            return reply
        analysis.analyze = measured
        async def handler(socket):
            await handle_asr_connection(socket, engine)
        async with websockets.serve(handler, "127.0.0.1", 0, max_size=3*1024*1024) as server:
            asr_port = server.sockets[0].getsockname()[1]
            for dual in (False, True):
                await engine.offline.configure(dual, engine.device, strict=True)
                identity = id(engine.offline.model) if dual else None
                for iteration in range(args.iterations):
                    vad.reset()
                    first, refinements, segment_ends = None, [], {}
                    final = asyncio.Event()
                    started = time.perf_counter()
                    async def receive(message):
                        nonlocal first
                        if message.get("type") == "error":
                            raise RuntimeError(message)
                        if message.get("text") and message.get("type") == "asr.partial" and first is None:
                            first = (time.perf_counter()-started)*1000
                        if message.get("type") == "asr.refined":
                            refinements.append({**message, "closure_wait_ms": (time.perf_counter()-segment_ends[message["segment_id"]])*1000})
                        if message.get("type") == "asr.final":
                            final.set()
                    client = AsrWorkerClient(f"ws://127.0.0.1:{asr_port}", on_result=receive)
                    await client.connect()
                    turn = f"{'dual' if dual else 'single'}-{iteration}"
                    voice_start, closures, lag = None, 0, []
                    started = time.perf_counter()
                    for position in range(0, len(pcm)//2, 320):
                        chunk = pcm[position*2:(position+320)*2]
                        target = started + position/16000
                        await asyncio.sleep(max(0, target-time.perf_counter()))
                        lag.append(max(0, time.perf_counter()-target)*1000)
                        await client.send_audio(AudioChunk(turn, chunk))
                        activity = await asyncio.to_thread(vad.feed, chunk)
                        if activity.speech_started:
                            voice_start = activity.speech_start_sample or position
                        if activity.speech_ended and voice_start is not None:
                            end = activity.speech_end_sample
                            segment_id = f"{turn}/{closures}"
                            segment_ends[segment_id] = time.perf_counter()
                            await client.send_audio(AudioSegment(turn, pcm[voice_start*2:end*2], segment_id, voice_start, end))
                            closures += 1
                            voice_start = None
                        perception._perception_frame(AudioFrame(chunk, cursor+position, time.monotonic()), activity)
                    await client.send_audio(AudioChunk(turn, b"", is_final=True))
                    await asyncio.wait_for(final.wait(), 10)
                    if dual:
                        async with asyncio.timeout(30):
                            while len(refinements) < closures:
                                await asyncio.sleep(.01)
                    rows.append({"dual_pass": dual, "iteration": iteration, "first_text_ms": first,
                        "vad_closures": closures, "refinements": refinements,
                        "capture_lag_max_ms": max(lag), "capture_lag_median_ms": statistics.median(lag),
                        "online_instance": id(engine._model), "offline_instance": id(engine.offline.model) if dual else None,
                        "same_offline_instance": not dual or id(engine.offline.model) == identity,
                        "rss_bytes": psutil.Process().memory_info().rss,
                        "analysis_rss_bytes": psutil.Process(process.pid).memory_info().rss})
                    await client.close()
                    cursor += len(pcm)//2
        await perception._stop_enhancements(clear=True)
        singles = [row["first_text_ms"] for row in rows if not row["dual_pass"]]
        doubles = [row["first_text_ms"] for row in rows if row["dual_pass"]]
        summary = {"requested_device": args.device, "actual_device": engine.device,
            "offline_device": engine.offline.model.device, "iterations": args.iterations,
            "audio_sha256": hashlib.sha256(args.audio.read_bytes()).hexdigest(),
            "first_text_median_delta_ms": statistics.median(doubles)-statistics.median(singles),
            "emotion_inferences": len(emotion_durations), "emotion_median_ms": statistics.median(emotion_durations),
            "acoustic_inferences": len(acoustic_durations), "acoustic_median_ms": statistics.median(acoustic_durations),
            "rows": rows, "fields_zh": {"first_text_median_delta_ms": "双路相对单路的首字延迟中位数变化",
                "capture_lag_max_ms": "回放投递相对真实采集节拍的最大延迟", "rss_bytes": "识别与检测进程常驻内存",
                "analysis_rss_bytes": "独立情绪与声学分析进程常驻内存", "closure_wait_ms": "语音检测闭段到精修回包的等待"}}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps({key: value for key, value in summary.items() if key != "rows"}, ensure_ascii=False))
    finally:
        engine.close()
        process.terminate(); process.join(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))

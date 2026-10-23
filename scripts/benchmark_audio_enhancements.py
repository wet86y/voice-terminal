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
    from voice_terminal.asr.text import join_asr_segments
    from voice_terminal.audio_enhancements import AudioEnhancements
    from voice_terminal.controller.enhancements import AudioEnhancementsMixin
    from voice_terminal.controller.turn_workspace import AudioFrame
    from voice_terminal.vad.detector import FsmnVadDetector

    audio, rate = sf.read(args.audio, dtype="int16", always_2d=False)
    if rate != 16000 or audio.ndim != 1:
        raise ValueError("固定音频必须是 16 千赫兹单声道")
    pcm = audio.tobytes() + b"\0\0" * 16000
    def new_engine():
        return AsrWorkerEngine(model_dir=ROOT / ".local/models/funasr/paraformer-zh-streaming",
            device=args.device, offline_model_dir=args.offline_model_dir)
    engine = new_engine()
    await engine.prepare()
    traces = {}
    def trace_engine():
        infer_sync = engine._infer_sync
        def traced(turn, audio, final):
            row = {"bytes": len(audio), "final": final, "sha256": hashlib.sha256(audio).hexdigest()}
            traces.setdefault(turn, []).append(row)
            result = infer_sync(turn, audio, final)
            row["raw_text"] = result  # raw_text（标点处理前的模型原始增量）。
            return result
        engine._infer_sync = traced
    trace_engine()
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
        rows, cursor, reference_checks = [], 0, []
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
            for mode_index, dual in enumerate((True, False) if args.mode_order == "dual-first" else (False, True)):
                if mode_index:
                    engine.close()
                    engine = new_engine()
                    await engine.prepare()
                    trace_engine()
                # 官方在线前端带随机扰动；配对模式必须具有相同的预热及随机状态历史。
                # 每种模式内部仍复用主模型，开启离线通道不能重建它。
                online_identity = id(engine._model)
                await engine.offline.configure(dual, engine.device, strict=True)
                engine.dual_pass_enabled = dual
                assert id(engine._model) == online_identity
                identity = id(engine.offline.model) if dual else None
                for iteration in range(args.iterations):
                    vad.reset()
                    first, refinements, segment_ends, primary_results = None, [], {}, []
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
                        if message.get("type") in {"asr.partial", "asr.final"}:
                            primary_results.append({"type": message["type"], "text": message["text"]})
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
                            await client.send_audio(AudioSegment(turn, pcm[voice_start*2:end*2], segment_id, voice_start, end,
                                                                 hotwords=tuple(args.hotword)))
                            closures += 1
                            voice_start = None
                        perception._perception_frame(AudioFrame(chunk, cursor+position, time.monotonic()), activity)
                    await client.send_audio(AudioChunk(turn, b"", is_final=True))
                    await asyncio.wait_for(final.wait(), 10)
                    if dual:
                        async with asyncio.timeout(30):
                            while len(refinements) < closures:
                                await asyncio.sleep(.01)
                    punctuated = None
                    if refinements:
                        punctuated = await client.punctuate_text(join_asr_segments([
                            row["text"] for row in sorted(refinements, key=lambda row: row["start_sample"])]), final=True)
                        assert punctuated["punctuation_status"] == "READY"
                    rows.append({"dual_pass": dual, "iteration": iteration, "first_text_ms": first,
                        "primary_calls": traces.get(turn, []), "primary_results": primary_results,
                        "punctuated_refinement": punctuated,
                        "vad_closures": closures, "refinements": refinements,
                        "capture_lag_max_ms": max(lag), "capture_lag_median_ms": statistics.median(lag),
                        "online_instance": id(engine._model), "offline_instance": id(engine.offline.model) if dual else None,
                        "offline_device": engine.offline.model.device if dual else None,
                        "same_offline_instance": not dual or id(engine.offline.model) == identity,
                        "rss_bytes": psutil.Process().memory_info().rss,
                        "analysis_rss_bytes": psutil.Process(process.pid).memory_info().rss})
                    await client.close()
                    cursor += len(pcm)//2
                reference = await engine.transcribe_reference(f"reference-{dual}", audio.tobytes())
                assert reference["route"] == ("offline" if dual else "streaming")
                assert reference["punctuation_status"] == "READY"
                reference_checks.append(reference)
        await perception._stop_enhancements(clear=True)
        singles = [row["first_text_ms"] for row in rows if not row["dual_pass"]]
        doubles = [row["first_text_ms"] for row in rows if row["dual_pass"]]
        summary = {"requested_device": args.device, "actual_device": engine.device,
            "offline_device": next(row["offline_device"] for row in rows if row["dual_pass"]), "iterations": args.iterations,
            "mode_order": args.mode_order,
            "audio_sha256": hashlib.sha256(args.audio.read_bytes()).hexdigest(),
            "reference_checks": reference_checks,
            "first_text_median_delta_ms": statistics.median(doubles)-statistics.median(singles),
            "emotion_inferences": len(emotion_durations), "emotion_median_ms": statistics.median(emotion_durations),
            "acoustic_inferences": len(acoustic_durations), "acoustic_median_ms": statistics.median(acoustic_durations),
            "rows": rows, "fields_zh": {"first_text_median_delta_ms": "双路相对单路的首字延迟中位数变化",
                "capture_lag_max_ms": "回放投递相对真实采集节拍的最大延迟", "rss_bytes": "识别与检测进程常驻内存",
                "analysis_rss_bytes": "独立情绪与声学分析进程常驻内存", "closure_wait_ms": "语音检测闭段到精修回包的等待"}}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        for single, dual in zip((row for row in rows if not row["dual_pass"]), (row for row in rows if row["dual_pass"])):
            inputs = lambda row: [{key: value for key, value in call.items() if key != "raw_text"} for call in row["primary_calls"]]
            assert inputs(single) == inputs(dual), "双路开关改变了主路模型输入或最终块标记"
            assert [call["raw_text"] for call in single["primary_calls"]] == [call["raw_text"] for call in dual["primary_calls"]], "双路开关改变了主模型原始输出"
            assert single["primary_results"] == dual["primary_results"], "双路开关改变了主路输出序列"
            assert dual["vad_closures"] and len(dual["refinements"]) == dual["vad_closures"]
        assert summary["actual_device"] == args.device and summary["offline_device"].startswith(args.device)
        assert summary["first_text_median_delta_ms"] <= 50, "双路首字延迟中位数增量超过 50 毫秒"
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
    parser.add_argument("--offline-model-dir", type=Path, default=ROOT / ".local/models/funasr/paraformer-zh-offline",
                        help="离线候选模型目录；默认使用当前受管资源")
    parser.add_argument("--hotword", action="append", default=[], help="原生目标热词，可重复指定")
    parser.add_argument("--mode-order", choices=("single-first", "dual-first"), default="single-first",
                        help="单双路回放顺序，用于排除预热及轮次先后影响")
    asyncio.run(run(parser.parse_args()))

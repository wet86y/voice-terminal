"""本地 TTS（语音合成）内存池对照：独立端口、固定种子、真实合成，不打开音频设备。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.providers.audio_cpp_tts import AudioCppTtsClient, AudioCppTtsServer
from voice_terminal.tts.voices import ModelVoiceLibraries

CONTEXT_KEYS = ("weight_context_mb", "text_embedding_graph_context_mb", "lm_step_graph_context_mb",
                "projection_graph_context_mb", "local_encoder_graph_context_mb", "dit_graph_context_mb",
                "audiovae_weight_context_mb", "audiovae_graph_context_mb", "audiovae_encoder_graph_context_mb")
TEXTS = ("今天我们先检查本地语音合成，确认长句能够连续播放。",
         "接下来检查第二个句子，保持音色和推理参数完全一致。",
         "最后核对内存是否持续增长，并验证声学上下文正确释放。")


def replay(arrivals, finished_at, *, buffered):
    """按真实分块到达时刻模拟20毫秒设备回调；不打开设备，不把自然静音当成欠载。"""
    from voice_terminal.audio.player import PcmPlayer
    player = PcmPlayer(sample_rate=16000, channels=1, output_device=None)
    player.allow("replay")
    if buffered:
        player.configure_stream_buffer("replay", startup_ms=800, resume_ms=400)
    tick, index, gap_ms, max_gap, gaps, total_gap = 0.0, 0, 0, 0, 0, 0
    source = b"".join(pcm for _, pcm in arrivals)
    actual = bytearray()
    started_at = None
    while tick < finished_at + len(source) / 32000 + 5:
        while index < len(arrivals) and arrivals[index][0] <= tick:
            player.enqueue("replay", arrivals[index][1])
            index += 1
        if tick >= finished_at:
            player.finish_stream("replay")
        before = player._played_bytes
        block = bytearray(640)
        player._callback(block, 320, None, None)
        consumed = player._played_bytes - before
        actual.extend(block[:consumed])
        if consumed and started_at is None:
            started_at = tick
        if started_at is not None and tick < finished_at and consumed < len(block):
            missing_ms = (len(block) - consumed) * 1000 // 32000
            if not gap_ms:
                gaps += 1
            gap_ms += missing_ms
            total_gap += missing_ms
            max_gap = max(max_gap, gap_ms)
        else:
            gap_ms = 0
        if tick >= finished_at and not player.buffered_ms:
            break
        tick += .02
    assert bytes(actual) == source, "设备回调未完整按序输出音频"
    return {"buffered": buffered, "first_playback_ms": round((started_at or 0) * 1000),
            "starvation_count": gaps, "starvation_ms": total_gap, "max_starvation_ms": max_gap,
            "exact_pcm_order": True}


async def case(model, steps, bounded, report_dir):
    import psutil
    runtime = ROOT / ".local"
    label = model + ("-bounded" if bounded else "-baseline")
    config_path = report_dir / (label + ".json")
    AudioCppTtsServer.write_runtime_config(runtime, config_path)
    config = json.loads(config_path.read_text("utf-8"))
    config["port"] = 18884
    for entry in config["models"]:
        family = entry["family"]
        spec = json.loads((runtime / "sources/audio.cpp/model_specs" / (family + ".json")).read_text("utf-8"))
        supported = {item["name"] for item in spec["options"]["session"]}
        for key in CONTEXT_KEYS:
            entry["session_options"].pop(f"{family}.{key}", None)
            if bounded and key in supported:
                entry["session_options"][f"{family}.{key}"] = 128
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
    server = AudioCppTtsServer(runtime, config_path, local_model=model)
    libraries = ModelVoiceLibraries(runtime / "data/tts")
    client = AudioCppTtsClient(server, voice_library=libraries,
        active_voice_provider=lambda name: libraries.for_model(name).active_selection,
        inference_steps_by_model={model: steps})
    session = None
    result = {"model": model, "bounded": bounded, "steps": steps, "memory_samples": [], "segments": []}
    def sample():
        process = psutil.Process(server._process.pid)
        info = process.memory_info()
        result["memory_samples"].append({"rss_bytes": info.rss, "private_commit_bytes": info.private,
                                         "vms_bytes": info.vms})
    try:
        server._start_process(log_path=report_dir / (label + ".log"))
        await client.ensure_ready()
        sample()
        session = client.session(turn_id=f"selftest-{label}", speech_id=f"selftest-{label}")
        session._request_options = {"seed": 1234}
        await session.start()
        chunks = []
        arrivals = []
        started_at = time.perf_counter()
        async def consume():
            async for pcm in session.audio_chunks():
                chunks.append(pcm)
                arrivals.append((time.perf_counter() - started_at, pcm))
        reader = asyncio.create_task(consume())
        try:
            for text in TEXTS:
                await session.send_text(text)
                sample()
            await session.finish()
            await reader
            finished_at = time.perf_counter() - started_at
        finally:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        pcm = b"".join(chunks)
        with wave.open(str(report_dir / (label + ".wav")), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(pcm)
        import numpy as np
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        result.update(pcm_sha256=hashlib.sha256(pcm).hexdigest(), pcm_bytes=len(pcm),
                      peak_amplitude=int(np.abs(samples).max()),
                      clipped_samples=int((np.abs(samples) >= 32767).sum()),
                      segments=session.request_metrics,
                      playback_replay=[replay(arrivals, finished_at, buffered=value) for value in (False, True)])
        await session.close()
        sample()
        return result
    finally:
        if session is not None:
            await session.close()
        await client.close()


async def main(args):
    report_dir = ROOT / ".local/cache/reports/tts-runtime-fixes/native-memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {"fields": {"model": "本地模型", "bounded": "是否使用128MiB元数据内存池",
        "steps": "推理步数", "memory_samples": "加载后、每句后、释放后进程采样",
        "rss_bytes": "物理驻留字节", "private_commit_bytes": "私有提交字节", "vms_bytes": "虚拟地址空间字节",
        "segments": "逐句耗时指标", "pcm_sha256": "完整音频摘要", "pcm_bytes": "音频字节数",
        "peak_amplitude": "最大采样幅度", "clipped_samples": "达到16位幅度上限的采样数",
        "identical_pcm": "基线和候选的完整音频是否逐字节相同"}, "cases": []}
    report["fields"].update(playback_replay="按真实分块到达时刻重放设备回调",
        buffered="是否使用起播800毫秒与恢复400毫秒储备", first_playback_ms="首个音频输出毫秒",
        starvation_count="非自然静音的欠载次数", starvation_ms="欠载累计毫秒",
        max_starvation_ms="最长连续欠载毫秒", exact_pcm_order="回调完整且按序输出原始音频")
    try:
        for model in args.models:
            baseline = await case(model, args.steps, False, report_dir)
            report["cases"].append(baseline)
            bounded = await case(model, args.steps, True, report_dir)
            bounded["identical_pcm"] = bounded["pcm_sha256"] == baseline["pcm_sha256"]
            report["cases"].append(bounded)
            print(json.dumps({"model": model, "identical_pcm": bounded["identical_pcm"],
                "baseline_commit": baseline["memory_samples"][-1]["private_commit_bytes"],
                "bounded_commit": bounded["memory_samples"][-1]["private_commit_bytes"]}), flush=True)
    finally:
        (report_dir / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", choices=("voxcpm1", "voxcpm2"), default=["voxcpm1", "voxcpm2"])
    parser.add_argument("--steps", type=int, default=15)
    asyncio.run(main(parser.parse_args()))

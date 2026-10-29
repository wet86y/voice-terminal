"""语音调节实机验收：候选依赖、固定文本、静音收集及实际输出边界。"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import shutil
import socket
import sys
import time
import wave
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.workspace_assets import ManagedRun

TEXT = "你好，我们现在开始这段语音测试。窗外的风慢慢吹过，桌上的灯还亮着。等你准备好了，我会继续说明接下来的安排。"


def save_wave(path, pcm, rate):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)


def install_candidate_for_probe(directory):
    """仅解包到本轮验证目录，进程搜索路径不影响当前程序的正式组件。"""
    lock = json.loads((ROOT / "config/runtime/voice-runtime.lock.json").read_text("utf-8"))
    row = next(item for item in lock["wheels"] if item["name"] == "voice-audio-dsp")
    wheel_path = ROOT / ".local/build/runtime-lock" / row["file"]
    assert hashlib.sha256(wheel_path.read_bytes()).hexdigest() == row["sha256"]
    candidate = directory / "dsp"
    with zipfile.ZipFile(wheel_path) as source:
        source.extractall(candidate)
    sys.path.insert(0, str(candidate))
    from voice_audio_dsp import available
    assert available()


def dsp_probe(directory):
    import numpy as np
    from voice_terminal.audio.speech_controls import PcmControlProcessor
    rows = []
    for rate in (16000, 32000, 48000):
        samples = np.rint(np.sin(np.arange(rate * 2) * 2 * np.pi * 220 / rate) * 8000).astype("<i2")
        for speed in (.8, 1, 1.2):
            processor = PcmControlProcessor(rate, speed=speed, volume=.6)
            started = time.perf_counter()
            pieces, position, first_input = [], 0, None
            rng = random.Random(20261028)
            while position < len(samples):
                end = min(len(samples), position + rng.randint(1, 2048))
                piece = processor.push(samples[position:end].tobytes())
                pieces.append(piece)
                if piece and first_input is None:
                    first_input = end
                position = end
            pieces.append(processor.finish())
            pcm = b"".join(pieces)
            out = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
            elapsed = time.perf_counter() - started
            center = out[rate // 4:-rate // 4]
            spectrum = np.abs(np.fft.rfft(center * np.hanning(len(center))))
            frequency = float(np.argmax(spectrum) * rate / len(center))
            assert abs(len(out) - round(len(samples) / speed)) <= 2
            assert abs(frequency - 220) < 2, frequency
            assert processor.output_frames == len(out)
            assert processor.buffered_input_frames == 0
            assert elapsed < len(out) / rate
            # 经过真实播放器回调验证：进度和打断快照用实际输出时钟，AEC（回声消除）取最终输出。
            from voice_terminal.audio.player import PcmPlayer
            reference = []
            player = PcmPlayer(sample_rate=rate, channels=1, output_device=None, on_audio_rendered=reference.append)
            player.allow("dsp-probe")
            player.enqueue("dsp-probe", pcm)
            assert player.progress("dsp-probe")["enqueued_ms"] == int(len(out) * 1000 / rate)
            consumed = len(out) // 2
            rendered = bytearray(consumed * 2)
            player._callback(rendered, consumed, None, None)
            assert reference == [bytes(rendered)]
            snapshot = player.stop_and_snapshot("dsp-probe")
            assert snapshot.played_ms == int(consumed * 1000 / rate)
            assert snapshot.remaining_ms == snapshot.enqueued_ms - snapshot.output_estimated_ms
            rows.append({"sample_rate": rate, "speed": speed, "volume": .6,
                         "input_frames": len(samples), "output_frames": len(out),
                         "peak_frequency_hz": frequency, "processing_ms": elapsed * 1000,
                         "playback_snapshot": snapshot.to_dict(), "aec_matches_output": True,
                         "first_output_input_ms": (first_input or len(samples)) * 1000 / rate})
            for frames in (1, 19, rate // 100, rate // 10):
                short = PcmControlProcessor(rate, speed=speed)
                output = short.push(samples[:frames].tobytes()) + short.finish()
                assert abs(len(output) // 2 - round(frames / speed)) <= 2
                short.close()
    return rows


async def collect(session, text):
    chunks, arrivals = [], []
    started = time.perf_counter()
    await session.start()
    async def read():
        async for pcm in session.audio_chunks():
            chunks.append(pcm)
            arrivals.append(time.perf_counter() - started)
    reader = asyncio.create_task(read())
    try:
        await session.send_text(text)
        if getattr(session, "bidi", False):
            await session.flush()
        else:
            await session.finish()
        await asyncio.wait_for(reader, 180)
        pcm = b"".join(chunks)
        assert pcm and session.received_bytes == len(pcm)
        if not getattr(session, "bidi", False):
            receipt = await session.wait_for_segment(0)
            assert receipt.end_received_bytes == len(pcm)
        return pcm, {"first_audio_ms": arrivals[0] * 1000, "elapsed_ms": (time.perf_counter() - started) * 1000,
                     "audio_bytes": len(pcm), "chunks": len(chunks), "receipt_matches_audio": True}
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await session.close()


async def cases(client, directory, model, *, online=False):
    from voice_terminal.tts.controls import SpeechControls
    from voice_terminal.tts.controlled_session import ControlledTtsSession
    settings = [SpeechControls(), SpeechControls(speed=.8), SpeechControls(speed=1.2),
                SpeechControls(volume=.6), SpeechControls(volume=1.4)]
    if online or model == "voxcpm2":
        settings.append(SpeechControls("happy" if online else "轻柔、亲近，略带笑意，像在安抚对方"))
    rows = []
    for index, values in enumerate(settings):
        if online:
            await client.prepare_speech_controls(values)
            raw = client.session(fresh=True, controls=values)
            session = ControlledTtsSession(client, raw, lambda value=values: value,
                                            lambda value, fresh: client.session(fresh=fresh, controls=value))
            payload = client.start_payload(controls=values)
            assert payload["voice_setting"]["speed"] == values.speed
            assert payload["voice_setting"]["vol"] == values.volume
            assert ("emotion" in payload["voice_setting"]) == bool(values.guidance)
        else:
            session = client.session()
            session.speech_control_reader = lambda value=values: value
            assert session._request_text(TEXT) == (f"({values.guidance}){TEXT}" if values.guidance else TEXT)
        pcm, metrics = await collect(session, TEXT)
        filename = f"{model}-{index + 1}.wav"
        save_wave(directory / filename, pcm, client.sample_rate)
        row = {"model": model, "parameters": asdict(values), "file": filename,
               "sample_rate": client.sample_rate, "duration_ms": len(pcm) * 1000 / (client.sample_rate * 2), **metrics}
        if not online:
            spans = session.audio_transform_spans
            assert spans and spans[-1].output_end * 2 == len(pcm)
            row["transform_spans"] = [asdict(span) for span in spans]
        rows.append(row)
        with (directory / "samples.jsonl").open("a", encoding="utf-8") as progress:
            progress.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return rows


async def rollover_case(client, directory, model):
    """在线真实任务滚动：前后两段使用不同参数，检查累计回执与音频不丢失。"""
    from voice_terminal.tts.controls import SpeechControls
    from voice_terminal.tts.controlled_session import ControlledTtsSession
    current = [SpeechControls()]
    await client.prepare_speech_controls(current[0])
    session = ControlledTtsSession(client, client.session(fresh=True, controls=current[0]), lambda: current[0],
                                   lambda value, fresh: client.session(fresh=fresh, controls=value))
    chunks = []
    async def read():
        async for pcm in session.audio_chunks():
            chunks.append(pcm)
    reader = asyncio.create_task(read())
    try:
        await session.start()
        await session.send_text("你好，我们现在开始这段语音测试。")
        first = await session.wait_for_segment(0)
        current[0] = SpeechControls(speed=1.2, volume=.6)
        await session.send_text("后面这一段说得更快，也更轻一些。")
        await session.finish()
        await asyncio.wait_for(reader, 60)
        second = await session.wait_for_segment(1)
        pcm = b"".join(chunks)
        assert 0 < first.end_received_bytes < second.end_received_bytes == len(pcm) == session.received_bytes
        filename = model + "-rollover.wav"
        save_wave(directory / filename, pcm, client.sample_rate)
        return {"model": model, "file": filename, "audio_bytes": len(pcm), "receipt_matches_audio": True,
                "segment_end_bytes": [first.end_received_bytes, second.end_received_bytes],
                "parameters": [{"speed": 1, "volume": 1}, {"speed": 1.2, "volume": .6}]}
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await session.close()


async def online_cases(directory):
    from voice_terminal.config import load_config
    from voice_terminal.model_registry.vault import WindowsCredentialVault
    from voice_terminal.model_registry.service import ModelRegistryService
    from voice_terminal.providers.minimax_tts import MiniMaxTtsClient
    config = load_config(ROOT / "config/default.yaml", ROOT / ".local/data/config/local.yaml")
    registry_path = ROOT / ".local/data/config/model-registry.json"
    service = ModelRegistryService(config=config, registry_path=registry_path, vault=WindowsCredentialVault())
    registry = json.loads(registry_path.read_text("utf-8"))
    secret = service._fixed_secret(registry, "minimax") or config.api_key("MINIMAX_API_KEY")
    if not secret:
        raise RuntimeError("未配置在线语音凭据。")
    path = ROOT / ".local/data/config/product-settings.json"
    stored = json.loads(path.read_text("utf-8"))["tts"] if path.is_file() else {}
    rows = []
    for model in ("speech-2.8-hd", "speech-2.8-turbo"):
        client = MiniMaxTtsClient(api_key=secret, base_url=config.tts.base_url, model=model,
                                 websocket_path=config.tts.websocket_path,
                                 voice_id=stored.get("online_voice_id", config.tts.voice_id),
                                 sample_rate=config.tts.online_sample_rate, timeout_ms=30000,
                                 transport="websocket", session_reuse=False)
        try:
            rows.extend(await cases(client, directory, model, online=True))
            rows.append(await rollover_case(client, directory, model))
        finally:
            await client.close()
    return rows


async def measure_local_buffering(client, model):
    """同时旁路和处理同一实际网络流，测量可归因的首次输出增量。"""
    from voice_terminal.audio.speech_controls import PcmControlProcessor
    from voice_terminal.tts.controls import DEFAULT_CONTROLS
    processors = {speed: PcmControlProcessor(client.sample_rate, speed=speed) for speed in (.8, 1, 1.2)}
    chunks = {speed: [] for speed in processors}
    first = {}
    session = client.session()
    session.speech_control_reader = lambda: DEFAULT_CONTROLS
    started = time.perf_counter()
    async def read():
        async for pcm in session.audio_chunks():
            for speed, processor in processors.items():
                output = processor.push(pcm)
                chunks[speed].append(output)
                if output and speed not in first:
                    first[speed] = (time.perf_counter() - started) * 1000
        for speed, processor in processors.items():
            output = processor.finish()
            chunks[speed].append(output)
            if output and speed not in first:
                first[speed] = (time.perf_counter() - started) * 1000
    reader = None
    try:
        await session.start()
        reader = asyncio.create_task(read())
        await session.send_text(TEXT)
        await session.finish()
        await asyncio.wait_for(reader, 120)
        receipt = await session.wait_for_segment(0)
        assert receipt.end_received_bytes == session.received_bytes == sum(map(len, chunks[1]))
        rows = []
        for speed, processor in processors.items():
            frames = sum(map(len, chunks[speed])) // 2
            assert abs(frames - round(session.received_bytes / 2 / speed)) <= 2
            rows.append({"model": model, "speed": speed, "first_audio_ms": first[speed],
                         "added_startup_ms": max(0, first[speed] - first[1]), "output_frames": frames})
        return rows
    finally:
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await session.close()
        for processor in processors.values():
            processor.close()


async def local_cases(directory, *, measure_only=False):
    from voice_terminal.providers.audio_cpp_tts import AudioCppTtsClient, AudioCppTtsServer
    from voice_terminal.tts.voices import ModelVoiceLibraries
    libraries = ModelVoiceLibraries(directory / "voices")
    seed = ROOT / "config/resources/assets/tts"
    transcript = json.loads((seed / "default.json").read_text("utf-8"))["reference_text"]
    rows = []
    for model in ("voxcpm1", "voxcpm2"):
        libraries.for_model(model).register_default((seed / "default.wav").read_bytes(), transcript)
        config_path = directory / (model + "-server.json")
        AudioCppTtsServer.write_runtime_config(ROOT / ".local", config_path)
        native_config = json.loads(config_path.read_text("utf-8"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            native_config["port"] = sock.getsockname()[1]
        config_path.write_text(json.dumps(native_config), encoding="utf-8")
        server = AudioCppTtsServer(ROOT / ".local", config_path, local_model=model)
        client = AudioCppTtsClient(server, voice_library=libraries,
                                   active_voice_provider=lambda selected: libraries.for_model(selected).active_selection,
                                   gpu_max_performance=False)
        try:
            rows.extend(await measure_local_buffering(client, model) if measure_only else await cases(client, directory, model))
        finally:
            await client.close()
    return rows


def derive_local_comparisons(directory, rows):
    """从同一份真实本地合成波形派生独立变量对比，排除再次合成的随机差异。"""
    from voice_terminal.audio.speech_controls import PcmControlProcessor
    from voice_terminal.tts.controls import SpeechControls
    for model in ("voxcpm1", "voxcpm2"):
        baseline = next((row for row in rows if row["model"] == model and row.get("parameters") == asdict(SpeechControls())), None)
        if baseline is None:
            continue
        with wave.open(str(directory / baseline["file"]), "rb") as source:
            rate = source.getframerate()
            pcm = source.readframes(source.getnframes())
        for label, values in (("speed-slow", SpeechControls(speed=.8)), ("speed-fast", SpeechControls(speed=1.2)),
                              ("volume-low", SpeechControls(volume=.6)), ("volume-high", SpeechControls(volume=1.4))):
            processor = PcmControlProcessor(rate, speed=values.speed, volume=values.volume)
            started = time.perf_counter()
            blocks = [processor.push(pcm[pos:pos + 2048]) for pos in range(0, len(pcm), 2048)]
            blocks.append(processor.finish())
            output = b"".join(blocks)
            assert abs(len(output) / 2 - round(len(pcm) / 2 / values.speed)) <= 2
            filename = model + "-" + label + ".wav"
            save_wave(directory / filename, output, rate)
            rows.append({"model": model, "parameters": asdict(values), "file": filename,
                         "sample_rate": rate, "duration_ms": len(output) * 1000 / (rate * 2),
                         "audio_bytes": len(output), "derived_from": baseline["file"],
                         "processing_ms": (time.perf_counter() - started) * 1000})
            processor.close()


def write_listening_report(directory, report):
    lines = ["# 智能语音调节验收与试听", "", "统一正文：" + report["text"], "",
             "自动校验包括实际音频长度、分段回执、随机分块、短音频收尾及220 Hz（赫兹）保音高检查。",
             "在线两种模型另验证了同一播报内参数变化后的原生任务滚动；本地独立变量样音从同一默认波形派生。",
             "听感尚待人工确认；首音频时间包含服务与连接波动，不能直接视为处理器净新增延迟。", "",
             "| 模型 | 参数 | 时长（秒） | 音频 |", "| --- | --- | --- | --- |"]
    for row in report["synthesis"]:
        parameters = row["parameters"]
        if isinstance(parameters, list):
            label = "任务滚动：默认→语速1.2、音量0.6"
        else:
            label = f"语速{parameters['speed']:g}／音量{parameters['volume']:g}"
            if parameters.get("guidance"):
                label += "／语气：" + parameters["guidance"]
            if row.get("derived_from"):
                label += "（同源波形处理）"
        duration = row.get("duration_ms", row["audio_bytes"] / 64) / 1000
        lines.append(f"| {row['model']} | {label} | {duration:.3f} | [试听]({(directory / row['file']).as_posix()}) |")
    lines += ["", "## 保音高处理测量", "", "| 采样率 | 语速 | 输入／输出帧 | 主频（赫兹） | 处理耗时（毫秒） | 首次输出前输入量（毫秒） |",
              "| --- | --- | --- | --- | --- | --- |"]
    for row in report["dsp"]:
        lines.append(f"| {row['sample_rate']} | {row['speed']} | {row['input_frames']}／{row['output_frames']} | "
                     f"{row['peak_frequency_hz']:.2f} | {row['processing_ms']:.2f} | {row['first_output_input_ms']:.2f} |")
    if report.get("latency"):
        lines += ["", "## 同一真实网络流的新增起播延迟", "", "| 模型 | 语速 | 首次输出（毫秒） | 相对旁路增量（毫秒） |",
                  "| --- | --- | --- | --- |"]
        for row in report["latency"]:
            lines.append(f"| {row['model']} | {row['speed']} | {row['first_audio_ms']:.2f} | {row['added_startup_ms']:.2f} |")
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main(args):
    with ManagedRun("speech-control-acceptance", ROOT, keep_success=True) as run:
        install_candidate_for_probe(run.path)
        report = {"text": TEXT, "dsp": dsp_probe(run.path), "synthesis": [],
                  "fields": {"text": "统一验收正文", "dsp": "真实动态库变速与音高检查", "synthesis": "实际合成样本",
                             "model": "模型", "parameters": "冻结参数", "file": "音频文件", "guidance": "语气引导",
                             "speed": "语速倍率", "volume": "相对音量", "sample_rate": "采样率", "duration_ms": "音频时长毫秒",
                             "audio_bytes": "实际输出字节数", "input_frames": "源采样帧数", "output_frames": "实际输出采样帧数",
                             "peak_frequency_hz": "主要频率赫兹", "processing_ms": "处理耗时毫秒", "chunks": "流式分块数量",
                             "first_output_input_ms": "首次产出前累计输入音频毫秒", "derived_from": "派生波形来源",
                             "playback_snapshot": "处理后播放打断快照", "aec_matches_output": "回声参考与最终输出一致",
                             "latency": "同源实际网络流首次输出对比", "added_startup_ms": "相对旁路的新增起播毫秒",
                             "segment_end_bytes": "累计分段回执字节边界", "reused_reports": "已完成验收来源",
                             "first_audio_ms": "首音频毫秒", "elapsed_ms": "全程耗时毫秒", "receipt_matches_audio": "回执与输出一致",
                             "transform_spans": "实际合成段映射", "input_start": "源开始帧", "input_end": "源结束帧",
                             "output_start": "输出开始帧", "output_end": "输出结束帧"}}
        if args.online:
            report["synthesis"].extend(await online_cases(run.path))
        if args.local:
            report["synthesis"].extend(await local_cases(run.path))
        for report_path in args.reuse_report:
            source = Path(report_path).resolve()
            if not source.is_relative_to((ROOT / ".local/tmp/tasks").resolve()):
                raise ValueError("复用验收报告必须位于受管任务目录。")
            previous = json.loads(source.read_text("utf-8"))
            for row in previous["synthesis"]:
                audio_path = source.parent / row["file"]
                if not audio_path.resolve().is_relative_to(source.parent):
                    raise ValueError("验收音频路径越界。")
                shutil.copy2(audio_path, run.path / row["file"])
                report["synthesis"].append(row)
            report.setdefault("reused_reports", []).append(str(source))
            if "latency" in previous:
                report.setdefault("latency", []).extend(previous["latency"])
        if args.measure_local_buffering:
            report["latency"] = await local_cases(run.path, measure_only=True)
        if args.derive_local:
            derive_local_comparisons(run.path, report["synthesis"])
        (run.path / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_listening_report(run.path, report)
        print(json.dumps({"report": str(run.path / "results.json")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="请求已配置的在线接口")
    parser.add_argument("--local", action="store_true", help="使用已安装本地组件进行合成")
    parser.add_argument("--reuse-report", action="append", default=[], help="复用受管现场中已完成的合成报告与音频")
    parser.add_argument("--derive-local", action="store_true", help="对真实本地默认波形派生独立语速与音量样音")
    parser.add_argument("--measure-local-buffering", action="store_true", help="使用同一真实本地网络流测量保音高变速新增起播延迟")
    asyncio.run(main(parser.parse_args()))

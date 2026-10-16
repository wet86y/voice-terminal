"""TTS（文本转语音）真实对照：10 步流式参考配方与 30 步样音完整解码，始终静音。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import socket
import statistics
import sys
import time
from uuid import uuid4
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from voice_terminal.providers.audio_cpp_tts import AudioCppTtsClient, AudioCppTtsServer

TEXTS = ["你好，现在开始测试。", "请确认参考音频和准确转写都已生效，语音仍然逐块输出。",
         "清晨的街道慢慢苏醒，第一班公交车从远处开过来。我们继续检查较长句子的生成速度、吐字和停顿，确认播报能够自然流畅地进行。"]
SAMPLE = "你好，很高兴认识你。现在开始测试这个音色，我会用自然的语气清楚地说话。"
DESCRIPTION = "温柔自然的年轻女声，吐字清晰，语速平稳。"


def save_wav(path, pcm):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(pcm)


async def stream(http, url, body, output=None):
    started = time.perf_counter()
    times, chunks, done_at, terminal = [], [], None, False
    async with http.stream("POST", url + "/v1/audio/speech", json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            assert not terminal, "流结束标记之后仍收到事件"
            if payload == "[DONE]":
                assert done_at is not None, "流结束标记早于音频完成事件"
                terminal = True
                continue
            if not payload:
                continue
            event = json.loads(payload)
            if event.get("type") == "error":
                raise RuntimeError(str(event))
            if event.get("type") == "speech.audio.delta":
                assert done_at is None, "音频完成事件之后仍收到音频"
                times.append(time.perf_counter())
                chunks.append(base64.b64decode(event["audio"], validate=True))
            if event.get("type") == "speech.audio.done":
                assert done_at is None and chunks, "音频完成事件重复或早于音频"
                done_at = time.perf_counter()
    ended = time.perf_counter()
    assert done_at is not None and terminal and chunks, "流式结束事件或音频缺失"
    pcm = b"".join(chunks)
    duration_ms = len(pcm) / 96
    row = {"first_audio_ms": round((times[0] - started) * 1000, 2),
           "total_ms": round((ended - started) * 1000, 2), "audio_ms": duration_ms,
           "rtf": round((ended - started) * 1000 / duration_ms, 4),
           "chunks": len(chunks), "first_before_done": times[0] < done_at - .02,
           "max_gap_ms": round(max([b-a for a, b in zip(times, times[1:])] or [0]) * 1000, 2),
           "pcm_sha256": hashlib.sha256(pcm).hexdigest()}
    if output:
        save_wav(output, pcm)
    return row


async def main(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    config_path = out / "server.json"
    AudioCppTtsServer.write_runtime_config(ROOT / ".local", config_path)
    config = json.loads(config_path.read_text("utf-8"))
    config["port"] = args.port
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    server = AudioCppTtsServer(ROOT / ".local", config_path, local_model="voxcpm2")
    if args.binary:
        binary = args.binary.resolve()
        server._server_binary = lambda: binary
    original_start = server._start_process
    server._start_process = lambda **kw: original_start(log_path=out / "native.log", **kw)
    client = AudioCppTtsClient(server, inference_steps_by_model={"voxcpm2": 10})
    ref = ROOT / "config/resources/assets/tts/default.wav"
    transcript = json.loads(ref.with_suffix(".json").read_text("utf-8"))["reference_text"]
    report = {"stream": [], "continuation": [], "samples": [], "fields": {
        "stream": "交替预热与测量的逐请求对照", "continuation": "同事务多段声学续接对照",
        "reference": "仅使用音色参考的基线", "prompt": "音色参考、提示音频与准确转写的完整配方",
        "samples": "30 步设计及混合样音的两种解码方式", "case": "参考配方或创建方式",
        "round": "交替测量轮次", "text_index": "正文编号", "first_audio_ms": "请求至首块音频毫秒",
        "total_ms": "请求总毫秒", "audio_ms": "生成音频时长毫秒", "rtf": "实时因子（合成耗时除以音频时长）",
        "chunks": "真实网络音频事件数", "first_before_done": "首音频在音频完成事件前至少 20 毫秒到达",
        "max_gap_ms": "相邻音频事件最大间隔毫秒", "pcm_sha256": "原始音频摘要",
        "full_decode": "生成结束后统一完整解码", "summary": "预热后中位数与采用门槛",
        "latency_pass": "新配方首音频增量不超过 150 毫秒或基线的 25%（取较大者）",
        "rtf_pass": "新配方实时因子不超过基线的 115% 加 0.05", "cold": "两种配方首次请求，包含提示编码但不含模型加载"}}
    def flush():
        (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        await client.ensure_ready()
        report["native_capabilities"] = getattr(server, "capabilities", {})
        report["native_sha256"] = hashlib.sha256(server._server_binary().read_bytes()).hexdigest()
        report["fields"].update(native_capabilities="被测原生程序实际公布的能力", native_sha256="被测程序完整摘要")
        assert report["native_capabilities"].get("reference_prompt"), "被测程序未公布提示音频能力"
        base = {"model": "voxcpm2-stream", "response_format": "pcm", "stream_format": "sse",
                "voice_ref": str(ref), "reference_text": transcript,
                "options": {"retry_badcase": False, "num_inference_steps": 10, "seed": 1234, "max_tokens": 512}}
        async with httpx.AsyncClient(timeout=180) as http:
            report["cold"] = {}
            for name in (() if args.samples_only else ("reference", "prompt")):
                body = {**base, "input": TEXTS[1]}
                if name == "prompt":
                    body["prompt_audio"] = str(ref)
                report["cold"][name] = await stream(http, server.base_url, body)
                flush()
            for turn in range(0 if args.samples_only else args.rounds):
                for index, text in enumerate(TEXTS):
                    order = ("reference", "prompt") if (turn + index) % 2 == 0 else ("prompt", "reference")
                    for name in order:
                        body = {**base, "input": text}
                        if name == "prompt":
                            body["prompt_audio"] = str(ref)
                        # 两种配方交替占用生产的单槽缓存；分别预热后测量，避免把缓存换入当作稳态。
                        await stream(http, server.base_url, body)
                        row = await stream(http, server.base_url, body, out / f"{name}-{turn}-{index}.wav")
                        row.update(case=name, round=turn, text_index=index)
                        report["stream"].append(row)
                        print(json.dumps(row, ensure_ascii=False), flush=True)
                        flush()
            for name in (() if args.samples_only else ("reference", "prompt")):
                identity = uuid4().hex
                try:
                    for index, text in enumerate(TEXTS):
                        body = {**base, "input": text, "continuation_id": identity}
                        if name == "prompt":
                            body["prompt_audio"] = str(ref)
                        row = await stream(http, server.base_url, body, out / f"continuation-{name}-{index}.wav")
                        row.update(case=name, text_index=index)
                        report["continuation"].append(row)
                        flush()
                finally:
                    response = await http.post(server.base_url + "/v1/audio/speech/contexts/release",
                        json={"model": "voxcpm2-stream", "continuation_id": identity})
                    response.raise_for_status()
                    assert response.json().get("released") is True
            if not args.skip_samples:
                for mixed in (False, True):
                    for full_decode in (False, True):
                        request = {"text": f"({DESCRIPTION}){SAMPLE}", "options": {
                            "retry_badcase": "false", "num_inference_steps": "30", "seed": "1234",
                            "max_tokens": "512", "voxcpm2.full_decode": str(full_decode).lower()}}
                        if mixed:
                            # 混合创作只用隔离音色参考；转写配方留给随后正式克隆。
                            request.update(voice_ref=str(ref))
                        started = time.perf_counter()
                        response = await http.post(server.base_url + "/v1/tasks/run",
                            json={"model": "voxcpm2-stream", "request": request})
                        response.raise_for_status()
                        result = response.json()
                        audio = result["audio"]
                        if isinstance(audio, dict):
                            audio = audio.get("data") or audio.get("base64")
                        wav = base64.b64decode(audio)
                        name = f"{'mixed' if mixed else 'design'}-full{full_decode}"
                        (out / f"{name}.wav").write_bytes(wav)
                        with wave.open(io.BytesIO(wav)) as source:
                            assert source.getframerate() == 48000 and source.getnchannels() == 1 and source.getsampwidth() == 2
                            duration = source.getnframes() / source.getframerate() * 1000
                            assert 0 < duration <= 15000, "样音为空、超长或格式错误"
                        row = {"case": name, "full_decode": full_decode, "audio_ms": duration,
                               "total_ms": round((time.perf_counter() - started) * 1000, 2)}
                        report["samples"].append(row)
                        print(json.dumps(row, ensure_ascii=False), flush=True)
                        flush()
        if args.samples_only:
            report["summary"] = {"sample_validation_pass": len(report["samples"]) == 4}
            report["fields"]["sample_validation_pass"] = "四种样音均通过格式及有效时长校验"
            flush()
            assert report["summary"]["sample_validation_pass"]
            return
        summary = {}
        for name in ("reference", "prompt"):
            rows = [row for row in report["stream"] if row["case"] == name]
            summary[name] = {key: round(statistics.median(row[key] for row in rows), 4)
                             for key in ("first_audio_ms", "rtf", "total_ms")}
        before, after = summary["reference"], summary["prompt"]
        summary["latency_pass"] = after["first_audio_ms"] <= before["first_audio_ms"] + max(150, before["first_audio_ms"] * .25)
        summary["rtf_pass"] = after["rtf"] <= before["rtf"] * 1.15 + .05
        summary["true_streaming"] = all(row["chunks"] > 1 and row["first_before_done"] for row in report["stream"] + report["continuation"])
        summary["conditioning_effective"] = all(
            next(row["pcm_sha256"] for row in report["stream"] if row["case"] == "reference" and row["round"] == 0 and row["text_index"] == index)
            != next(row["pcm_sha256"] for row in report["stream"] if row["case"] == "prompt" and row["round"] == 0 and row["text_index"] == index)
            for index in range(len(TEXTS)))
        report["fields"].update(true_streaming="所有请求均收到多个音频事件，首音频早于完整响应结束",
                                conditioning_effective="相同正文和随机种子下，提示音频实际改变输出")
        report["summary"] = summary
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        flush()
        assert all(summary[key] for key in ("latency_pass", "rtf_pass", "true_streaming", "conditioning_effective")), "质量配方对照未通过门槛，详见完整报告"
    finally:
        await client.close()
        flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="binary（待验证原生程序）")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/cache/reports/tts-quality-20261016", help="output（隔离报告目录）")
    parser.add_argument("--port", type=int, default=18886, help="port（隔离服务端口）")
    parser.add_argument("--rounds", type=int, default=3, help="rounds（交替测量轮次）")
    parser.add_argument("--skip-samples", action="store_true", help="skip-samples（只测流式，不生成设计样音）")
    parser.add_argument("--samples-only", action="store_true", help="samples-only（只对照30步样音制作方式）")
    asyncio.run(main(parser.parse_args()))

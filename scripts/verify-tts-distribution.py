"""验证分发安装出的本地 TTS（语音合成）：隔离端口、默认参考音频、双模型与取消交接。"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.providers.audio_cpp_tts import AudioCppTtsClient, AudioCppTtsServer
from voice_terminal.tts.voices import ModelVoiceLibraries


async def verify(install: Path, resources: Path) -> dict:
    """verify（真实推理验证）：复用只读权重，将配置、音色及音频全部写入隔离安装目录。"""
    install, resources = install.resolve(), resources.resolve()
    if not install.is_relative_to(ROOT / ".local/cache/reports/tts-distribution"):
        raise ValueError("原生分发验证只能使用本轮隔离报告目录。")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 18884))
    config_path = install / "config/audiocpp/server.json"
    AudioCppTtsServer.write_runtime_config(install, config_path)
    config = json.loads(config_path.read_text("utf-8"))
    config["port"] = 18884
    for row in config["models"]:
        row["path"] = str(resources / Path(row["path"]).relative_to(install))
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    libraries = ModelVoiceLibraries(install / "data/tts")
    seed = ROOT / "config/resources/assets/tts"
    transcript = json.loads((seed / "default.json").read_text("utf-8"))["reference_text"]
    for library in libraries.libraries.values():
        library.register_default((seed / "default.wav").read_bytes(), transcript)
    server = AudioCppTtsServer(install, config_path, local_model="voxcpm1")
    client = AudioCppTtsClient(server, voice_library=libraries,
                              active_voice_provider=lambda model: libraries.for_model(model).active_selection,
                              gpu_max_performance=False)
    report = {"models": [], "fields": {"models": "按顺序实际加载和合成的模型记录", "model": "本地模型",
              "repeated_generation": "二代模型相同正文复用计算图的真实回归",
              "pcm_bytes": "完整原始音频字节数", "completed_segments": "完整正文块回执数", "elapsed_ms": "加载与合成总毫秒",
              "cancel_handoff": "实际取消并确认原生执行锁释放后发起新请求的记录"}}
    spec = importlib.util.spec_from_file_location("tts_verification", ROOT / "scripts/verify-tts-integration.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    try:
        for index, model in enumerate(("voxcpm1", "voxcpm2", "voxcpm1")):
            started = time.perf_counter()
            await client.switch_model(model)
            await client.ensure_ready()
            session = client.session(fresh=True)
            try:
                await session.start()
                await session.send_text("本地语音分发验证，参考音频与模型切换正常。")
                await session.finish()
                chunks = [chunk async for chunk in session.audio_chunks()]
                pcm = b"".join(chunks)
                if not pcm or session.completed_segments != 1:
                    raise AssertionError("原生运行程序未返回完整音频或正文完成回执。")
                with wave.open(str(install / f"sample-{index}-{model}.wav"), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(client.sample_rate)
                    output.writeframes(pcm)
                report["models"].append({"model": model, "pcm_bytes": len(pcm), "completed_segments": session.completed_segments,
                                         "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
                print(json.dumps(report["models"][-1], ensure_ascii=False), flush=True)
            finally:
                await session.close()
            if model == "voxcpm2":
                report["repeated_generation"] = await helper.repeated_generation_case(client)
        report["cancel_handoff"] = await helper.cancellation_case(client, before_audio=False)
        print(json.dumps(report["cancel_handoff"], ensure_ascii=False), flush=True)
        return report
    finally:
        await client.close()
        (install / "inference-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path, default=ROOT / ".local/cache/reports/tts-distribution/native-install", help="install（隔离的原生分发安装目录）")
    parser.add_argument("--resources", type=Path, default=ROOT / ".local", help="resources（既有只读模型资源根目录）")
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(verify(args.install, args.resources), timeout=360))

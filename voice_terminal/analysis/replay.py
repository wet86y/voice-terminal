"""Paced PCM through the real v0.2 controller; playback is an in-memory sink."""
import argparse
import asyncio
import json
import logging
from dataclasses import replace
from math import gcd
from pathlib import Path
import time

from ..asr.client import AsrWorkerClient
from ..config import load_config
from ..controller.realtime_controller import RealtimeController
from ..controller.turn_workspace import AudioFrame
from ..core.state import RuntimeState
from ..core.event_bus import EventBus
from ..server.history import InMemoryHistory
from ..asr.worker import AsrWorkerEngine
from ..audio.player import PcmPlayer
from ..vad.detector import FsmnVadDetector
from ..providers.deepseek import DeepSeekClient
from ..providers.minimax_tts import MiniMaxTtsClient


class ReplayAsr:
    connected = True
    on_result = None
    def __init__(self, engine):
        self.engine = engine
    async def send_audio(self, chunk):
        result = await self.engine.infer(chunk.turn_id, chunk.pcm, chunk.is_final)
        if result.emitted or chunk.is_final:
            await self.on_result({"type": "asr.final" if chunk.is_final else "asr.partial", "turn_id": chunk.turn_id, "text": result.text})
    async def send_finish(self, turn_id):
        self.engine._clear_turn(turn_id)
    async def health(self):
        return {"type": "health", "status": "READY", "model_loaded": True, "mode": "in-process"}
    async def close(self):
        pass


async def run(args):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    root = Path(__file__).resolve().parents[2]
    default_path = root / "config" / "default.yaml"
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_config(default_path, config_path)
    if args.analysis_url:
        config = replace(config, v02=replace(config.v02, analysis_url=args.analysis_url))

    engine = None
    if args.asr_mode == "in-process":
        engine = AsrWorkerEngine(model_dir=config.asr_model_dir, device=config.resolved_asr_device)
        await asyncio.to_thread(engine.load)
        asr = ReplayAsr(engine)
    else:
        asr = AsrWorkerClient(args.asr_url or config.asr.worker_url)
        await asr.connect()
    asr_health = await asr.health()
    vad = FsmnVadDetector(model_dir=config.vad_model_dir, end_silence_ms=300)
    provider = DeepSeekClient(base_url=config.llm.base_url, model=config.llm.model,
                               api_key=config.api_key(config.llm.api_key_env), thinking="disabled")
    tts = MiniMaxTtsClient(base_url=config.tts.base_url, websocket_path=config.tts.websocket_path,
                           model=config.tts.model, api_key=config.api_key(config.tts.api_key_env),
                           output_format=config.tts.output_format, sample_rate=config.tts.sample_rate,
                           voice_id=config.tts.voice_id, timeout_ms=config.tts.timeout_ms,
                           transport=config.tts.transport, scheduling=config.tts.scheduling,
                           subtitle_enable=config.tts.subtitle_enable, subtitle_type=config.tts.subtitle_type,
                           audio_cache_max_bytes=config.tts.audio_cache_max_bytes,
                           cancel_timeout_ms=config.tts.cancel_timeout_ms,
                           keepalive_seconds=config.tts.keepalive_seconds)
    player = PcmPlayer(sample_rate=config.tts.sample_rate, channels=1, output_device=None)
    c = RealtimeController(options=config.v02, state=RuntimeState(), bus=EventBus(), history=InMemoryHistory(),
                            capture=None, kws=None, vad=vad, asr=asr, llm=provider, tts=tts, player=player,
                            wake_enabled=False, wake_keyword=config.wake.keyword, wake_preroll_ms=1200,
                            vad_min_speech_ms=250, vad_end_silence_ms=700, max_turn_ms=60000,
                            bridge_min_chars=8, bridge_max_chars=36, bridge_max_wait_ms=350, bridge_breaks="。！？；，、")
    asr_messages = []
    asr_started_at = time.monotonic()
    controller_callback = asr.on_result

    async def record_asr_message(message):
        if isinstance(message, dict):
            asr_messages.append({
                "type": message.get("type"),
                "turn_id": message.get("turn_id"),
                "code": message.get("code"),
                "elapsed_ms": round((time.monotonic() - asr_started_at) * 1000, 1),
            })
        if controller_callback is not None:
            await controller_callback(message)

    asr.on_result = record_asr_message
    await c.initialize()
    # The replay intentionally has no native capture device, but the ASR
    # transport itself has already passed its application-level health check.
    # Reflect that distinction in the snapshot used by the report.
    c.state.services["asr"] = "READY"
    c.state.listening = True
    c._asr_sender = asyncio.create_task(c._send_asr_loop())
    samples = []
    for path in args.audio:
        audio, rate = sf.read(path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        factor = gcd(rate, 16000)
        audio = resample_poly(audio, 16000 // factor, rate // factor)
        if samples:
            samples.append(np.zeros(int(args.pause_ms * 16), dtype=np.float32))
        samples.append(audio)
    audio = np.concatenate(samples)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    events = []
    played_bytes = 0

    async def monitor():
        async for item in c.bus.subscribe():
            if item.type in {"turn.started", "turn.resumed", "turn.finalizing", "turn.committed", "turn.completed", "error",
                             "session.state", "transcript.segment", "transcript.refined", "transcript.final", "speech.emotion",
                             "agent.prediction.requested", "agent.prediction", "agent.prediction.discarded", "agent.tool",
                             "assistant.delta", "llm.context", "tts.status", "tts.progress", "latency.snapshot"}:
                payload = {k: v for k, v in item.payload.items() if k != "workspace"}
                events.append({"type": item.type, "timestamp": item.timestamp, "turn_id": item.turn_id, "payload": payload})

    async def playback_sink():
        nonlocal played_bytes
        while True:
            block = bytearray(config.tts.sample_rate // 50 * 2)
            before = player.buffered_ms
            player._callback(block, len(block) // 2, None, None)
            if before:
                played_bytes += len(block)
            await asyncio.sleep(.02)

    monitor_task = asyncio.create_task(monitor())
    sink = asyncio.create_task(playback_sink())
    await asyncio.sleep(0)
    start = time.monotonic()
    max_asr_queue = 0
    max_workspace_pcm = 0
    try:
        # Keep silence flowing while the final cloud response and PCM drain.
        for offset in range(0, len(pcm) + 120 * 32000, 640):
            await asyncio.sleep(max(0, start + offset / 32000 - time.monotonic()))
            chunk = pcm[offset:offset + 640].ljust(640, b"\x00")
            await c.handle_frame(AudioFrame(chunk, offset // 2, time.monotonic()))
            max_asr_queue = max(max_asr_queue, c._asr_queue.qsize())
            if c.workspace is not None:
                max_workspace_pcm = max(max_workspace_pcm, len(c.workspace.audio.pcm))
            if offset >= len(pcm) and c.workspace is None and c.history.records:
                break
        try:
            await asyncio.wait_for(c._asr_queue.join(), 5)
        except asyncio.TimeoutError:
            events.append({"type": "error", "payload": {"code": "ASR_QUEUE_DRAIN_TIMEOUT"}})
        prediction_events = [e for e in events if e["type"].startswith("agent.prediction")]
        tool_events = [e for e in events if e["type"] == "agent.tool"]
        partial_count = sum(m.get("type") == "asr.partial" for m in asr_messages)
        final_count = sum(m.get("type") == "asr.final" for m in asr_messages)
        disconnect_count = sum(m.get("code") == "ASR_WORKER_DISCONNECTED" for m in asr_messages)
        report = {"kind": "paced synthetic replay with real models/cloud providers; in-memory playback, not physical speaker",
                  "asr_mode": args.asr_mode,
                  "asr_url": args.asr_url or (config.asr.worker_url if args.asr_mode == "worker" else None),
                  "asr_health": asr_health,
                  "asr_partial_count": partial_count,
                  "asr_final_count": final_count,
                  "disconnects": disconnect_count,
                  "asr_message_summary": {
                      "partial": partial_count,
                      "final": final_count,
                      "errors": sum(m.get("type") == "error" for m in asr_messages),
                      "first_partial_ms": next((m["elapsed_ms"] for m in asr_messages if m.get("type") == "asr.partial"), None),
                      "first_final_ms": next((m["elapsed_ms"] for m in asr_messages if m.get("type") == "asr.final"), None),
                  },
                  "max_asr_queue": max_asr_queue,
                  "workspace_max_pcm_bytes": max_workspace_pcm,
                  "input_duration_s": len(pcm) / 32000, "sink_pcm_bytes": played_bytes,
                  "prediction_summary": {
                      "requested": sum(e["type"] == "agent.prediction.requested" for e in prediction_events),
                      "accepted": sum(e["type"] == "agent.prediction" and e["payload"].get("decision") in {"LISTEN", "ACT", "READY"} for e in prediction_events),
                      "discarded": sum(e["type"] == "agent.prediction.discarded" for e in prediction_events),
                      "discard_reasons": [e["payload"].get("reason") for e in prediction_events if e["type"] == "agent.prediction.discarded"],
                  },
                  "tool_summary": {"events": len(tool_events),
                                   "prefetch_done": sum(bool(e["payload"].get("speculative")) and e["payload"].get("status") == "DONE" for e in tool_events),
                                   "final_reused": sum(bool(e["payload"].get("reused")) and not bool(e["payload"].get("speculative")) for e in tool_events)},
                  "events": events, "history": c.history.messages,
                  "records": [r.to_payload() for r in c.history.records], "last_snapshot": c.state.snapshot()}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"turns": len(c.history.records), "resumes": sum(e["type"] == "turn.resumed" for e in events),
                          "errors": [e for e in events if e["type"] == "error"], "sink_pcm_bytes": played_bytes}, ensure_ascii=True))
        if not c.history.records or not played_bytes or any(e["type"] == "error" for e in events):
            raise RuntimeError("replay did not complete cleanly; inspect report")
    finally:
        monitor_task.cancel()
        sink.cancel()
        await c.close()
        await asyncio.gather(monitor_task, sink, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", nargs="+", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config/v0.2.yaml"), help="配置文件路径")
    parser.add_argument("--asr-mode", choices=("in-process", "worker"), default="in-process")
    parser.add_argument("--asr-url", default=None, help="仅测试时覆盖配置中的 ASR Worker URL")
    parser.add_argument("--analysis-url", default=None, help="仅测试时覆盖配置中的 Analysis Worker URL")
    parser.add_argument("--pause-ms", type=int, default=800)
    parser.add_argument("--output", type=Path, default=Path("logs/evaluation/replay.json"))
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

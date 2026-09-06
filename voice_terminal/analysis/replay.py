"""Paced PCM through v0.2/v0.3; playback is an in-memory sink."""
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
from ..controller.episode_controller import EpisodeController
from ..controller.turn_workspace import AudioFrame
from ..core.state import RuntimeState
from ..core.event_bus import EventBus
from ..server.history import InMemoryHistory
from ..asr.worker import AsrWorkerEngine
from ..audio.player import PcmPlayer
from ..vad.detector import FsmnVadDetector
from ..providers.deepseek import DeepSeekClient
from ..providers.minimax_tts import MiniMaxTtsClient


def _v03_summary(events, controller, *, timed_out=False):
    traces = [item["payload"] for item in events if item["type"] == "agent.trace"]
    completed = [item["payload"] for item in events if item["type"] == "speech.completed"]
    trace_counts = {}
    for trace in traces:
        kind = trace.get("kind")
        if isinstance(kind, str):
            trace_counts[kind] = trace_counts.get(kind, 0) + 1
    return {
        "wake_reasons": {key: trace_counts.get(key, 0) for key in (
            "USER_SEGMENT_UPDATED", "USER_INPUT_FINALIZED", "USER_INPUT_REFINED",
            "ENDPOINT_CANDIDATE", "SILENCE_TIMEOUT", "TOOL_COMPLETED") if trace_counts.get(key, 0)},
        "model_requests": trace_counts.get("AGENT_INVOKED", 0),
        "max_request_concurrency": getattr(controller.episode, "max_request_concurrency", 1),
        "wait": trace_counts.get("WAIT", 0),
        "speak": {
            "accepted": trace_counts.get("SPEAK_ACCEPTED", 0),
            "blocked": trace_counts.get("SPEAK_BLOCKED", 0),
            "dropped": trace_counts.get("STALE_ACTION_DROPPED", 0),
        },
        "tools": {
            "started": trace_counts.get("TOOL_STARTED", 0),
            "completed": trace_counts.get("TOOL_COMPLETED", 0),
            "reused": sum(1 for item in traces if item.get("kind") == "TOOL_STARTED" and item.get("reused")),
            "discarded": trace_counts.get("TOOL_RESULT_DISCARDED", 0),
        },
        "outputs": {
            "completed": sum(item.get("status") == "completed" for item in completed),
            "failed": sum(item.get("status") == "failed" for item in completed),
            "interrupted": sum(item.get("status") in {"interrupted", "stale", "stopped"} for item in completed),
        },
        "trace_counts": trace_counts,
        "timed_out": timed_out,
    }


def _safe_event_summary(events):
    """Keep only bounded event kinds/counts in the default report."""
    counts = {}
    errors = []
    for item in events:
        kind = item.get("type")
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
        if kind == "error":
            payload = item.get("payload") or {}
            if isinstance(payload, dict):
                errors.append({key: str(payload[key])[:200] for key in ("code", "recoverable") if key in payload})
    return {"counts": counts, "errors": errors[-20:]}


def _record_summaries(records):
    return [{"turn_id": record.turn_id, "episode_id": record.episode_id,
             "status": record.delivery.get("termination") if record.delivery else None,
             "delivery_status": record.delivery.get("status") if record.delivery else None,
             "latency": dict(record.latency)} for record in records]


def _check_expectations(report, expected):
    failures = []
    if not expected:
        return failures
    summary = report.get("v03_summary", {})
    if "min_history" in expected and report.get("turns", 0) < int(expected["min_history"]):
        failures.append("expected_min_history")
    if expected.get("require_pcm") and report.get("sink_pcm_bytes", 0) <= 0:
        failures.append("expected_pcm")
    if expected.get("allow_no_pcm") is False and report.get("sink_pcm_bytes", 0) <= 0:
        failures.append("unexpected_no_pcm")
    if "max_model_requests" in expected and summary.get("model_requests", 0) > int(expected["max_model_requests"]):
        failures.append("expected_request_budget")
    if "max_concurrency" in expected and summary.get("max_request_concurrency", 0) > int(expected["max_concurrency"]):
        failures.append("expected_concurrency")
    if "min_speak_accepted" in expected and summary.get("speak", {}).get("accepted", 0) < int(expected["min_speak_accepted"]):
        failures.append("expected_speak")
    return failures


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
    expected = {}
    if args.expect:
        expected = json.loads(Path(args.expect).read_text(encoding="utf-8"))
        if not isinstance(expected, dict):
            raise ValueError("--expect must contain a JSON object")
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
    controller_type = EpisodeController if config.v03.enabled else RealtimeController
    extra = {"agent_options": config.v03} if config.v03.enabled else {}
    c = controller_type(**extra, options=config.v02, state=RuntimeState(), bus=EventBus(), history=InMemoryHistory(),
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
                             "assistant.delta", "llm.context", "tts.status", "tts.progress", "latency.snapshot",
                             "agent.episode", "agent.trace", "speech.progress", "speech.completed"}:
                payload = {k: v for k, v in item.payload.items() if k != "workspace"}
                if config.v03.enabled and item.type == "llm.context":
                    payload = {k: v for k, v in payload.items() if k != "messages"}
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
    timed_out = False
    try:
        # Keep silence flowing while the final cloud response and PCM drain.
        for offset in range(0, len(pcm) + 120 * 32000, 640):
            await asyncio.sleep(max(0, start + offset / 32000 - time.monotonic()))
            chunk = pcm[offset:offset + 640].ljust(640, b"\x00")
            await c.handle_frame(AudioFrame(chunk, offset // 2, time.monotonic()))
            max_asr_queue = max(max_asr_queue, c._asr_queue.qsize())
            if c.workspace is not None:
                max_workspace_pcm = max(max_workspace_pcm, len(c.workspace.audio.pcm))
            agent_idle = not config.v03.enabled or (not c.output.active and c.episode.active_request is None
                         and not c.episode.pending and offset >= len(pcm) + config.v03.silence_ms * 32 + 32000)
            if offset >= len(pcm) and c.workspace is None and c.history.records and agent_idle:
                break
        else:
            timed_out = True
        # A final provider/PCM cleanup can outlive the last audio frame.  Do
        # not classify a report as successful while any Agent or output task
        # is still settling.
        settle_deadline = time.monotonic() + 5
        while time.monotonic() < settle_deadline:
            agent_idle = (not config.v03.enabled or
                          (not c.output.active and not c.output.tasks and
                           c.episode.active_request is None and not c.episode.pending))
            if c.workspace is None and agent_idle:
                break
            await asyncio.sleep(.02)
        else:
            timed_out = True
            events.append({"type": "replay.timeout", "payload": {"stage": "settlement"}})
        try:
            await asyncio.wait_for(c._asr_queue.join(), 5)
        except asyncio.TimeoutError:
            timed_out = True
            events.append({"type": "replay.timeout", "payload": {"stage": "asr_queue"}})
        prediction_events = [e for e in events if e["type"].startswith("agent.prediction")]
        tool_events = [e for e in events if e["type"] == "agent.tool"]
        partial_count = sum(m.get("type") == "asr.partial" for m in asr_messages)
        final_count = sum(m.get("type") == "asr.final" for m in asr_messages)
        disconnect_count = sum(m.get("code") == "ASR_WORKER_DISCONNECTED" for m in asr_messages)
        v03_summary = _v03_summary(events, c, timed_out=timed_out) if config.v03.enabled else None
        event_summary = _safe_event_summary(events)
        internal_failures = list(event_summary["errors"])
        for trace in events:
            if trace["type"] == "agent.trace" and trace["payload"].get("kind") in {"AGENT_ERROR", "SPEECH_ERROR"}:
                internal_failures.append({"kind": trace["payload"].get("kind"),
                                          "error_type": "agent_failure"})
        report = {"kind": "paced synthetic replay with real models/cloud providers; in-memory playback, not physical speaker",
                  "config_summary": {"v02_enabled": config.v02.enabled, "v03_enabled": config.v03.enabled,
                                     "decision_mode": config.v03.decision_mode if config.v03.enabled else None,
                                     "tts_transport": config.tts.transport, "asr_mode": args.asr_mode},
                  "asr_mode": args.asr_mode,
                  "asr_health": {key: asr_health.get(key) for key in ("status", "model_loaded", "mode") if key in asr_health},
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
                  "v03_summary": v03_summary,
                  "event_summary": event_summary,
                  "records": _record_summaries(c.history.records),
                  "turns": len(c.history.records),
                  "timed_out": timed_out,
                  "internal_failures": internal_failures}
        report["expectation_failures"] = _check_expectations(report, expected)
        requires_pcm = bool(expected.get("require_pcm", not config.v03.enabled))
        report["passed"] = not timed_out and not internal_failures and not report["expectation_failures"] \
            and bool(c.history.records) and (played_bytes > 0 or not requires_pcm)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "turns": len(c.history.records),
                          "resumes": sum(e["type"] == "turn.resumed" for e in events),
                          "internal_failures": internal_failures,
                          "timed_out": timed_out, "sink_pcm_bytes": played_bytes}, ensure_ascii=True))
        if not report["passed"]:
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
    parser.add_argument("--expect", type=Path, default=None,
                        help="JSON expectation object; failures are reported separately from legal waits/blocks")
    parser.add_argument("--output", type=Path, default=Path("logs/evaluation/replay.json"))
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

"""Real LLM/TTS comparison with synthetic input and an in-memory PCM sink.

No microphone, device playback, external tool execution, or raw responses saved.
Run: python -m voice_terminal.analysis.smoke_v03 --output logs/evaluation/v03-smoke.json
"""
import argparse
import asyncio
from dataclasses import replace
import json
import logging
from pathlib import Path
import time

from ..config import load_config
from ..providers.deepseek import DeepSeekClient
from ..providers.minimax_tts import MiniMaxTtsClient
from ..controller.realtime_controller import RealtimeController
from ..controller.episode_controller import EpisodeController
from ..controller.turn_workspace import TurnWorkspace
from ..core.event_bus import EventBus
from ..core.state import RuntimeState
from ..server.history import InMemoryHistory
from ..audio.player import PcmPlayer
from ..agent.episode import AgentEpisodeRuntime
from ..agent.speculative_tools import SpeculativeTools
from ..agent.tool_registry import FileTools


class MeasuredProvider:
    def __init__(self, client):
        self.client = client
        self.deltas = 0
        self.finished_at = None
        self.started_at = None

    async def stream_events(self, messages, **kwargs):
        self.started_at = time.monotonic()
        async for item in self.client.stream_events(messages, **kwargs):
            if item["type"] in {"content", "tool_delta"}:
                self.deltas += 1
            if item["type"] == "finish":
                self.finished_at = time.monotonic()
            yield item


async def run_case(config, version):
    provider = MeasuredProvider(DeepSeekClient(base_url=config.llm.base_url, model=config.llm.model,
        api_key=config.api_key(config.llm.api_key_env), thinking=config.llm.thinking, timeout_ms=config.llm.timeout_ms))
    tts = MiniMaxTtsClient(base_url=config.tts.base_url, websocket_path=config.tts.websocket_path,
        model=config.tts.model, api_key=config.api_key(config.tts.api_key_env), output_format=config.tts.output_format,
        sample_rate=config.tts.sample_rate, voice_id=config.tts.voice_id, timeout_ms=config.tts.timeout_ms,
        transport=config.tts.transport, scheduling=config.tts.scheduling,
        subtitle_enable=config.tts.subtitle_enable, subtitle_type=config.tts.subtitle_type,
        audio_cache_max_bytes=config.tts.audio_cache_max_bytes, cancel_timeout_ms=config.tts.cancel_timeout_ms,
        keepalive_seconds=config.tts.keepalive_seconds)
    player = PcmPlayer(sample_rate=config.tts.sample_rate, channels=1, output_device=None)
    kind = EpisodeController if version == "v03" else RealtimeController
    extra = {"agent_options": replace(config.v03, decision_mode="conservative")} if version == "v03" else {}
    c = kind(**extra, options=replace(config.v02, tools=False), analysis=None,
        state=RuntimeState(), bus=EventBus(), history=InMemoryHistory(), capture=None, kws=None, vad=None, asr=None,
        llm=provider, tts=tts, player=player, wake_enabled=False, wake_keyword="hei claw", wake_preroll_ms=1200,
        vad_min_speech_ms=250, vad_end_silence_ms=700, max_turn_ms=60000,
        bridge_min_chars=8, bridge_max_chars=36, bridge_max_wait_ms=350, bridge_breaks="。！？；，、")
    c._loop = asyncio.get_running_loop()
    player.on_playback_started = c._on_playback_started
    first_playback = None
    async def sink():
        nonlocal first_playback
        while True:
            before = player.buffered_ms
            frames = config.tts.sample_rate // 50
            player._callback(bytearray(frames * 2), frames, None, None)
            if before and first_playback is None:
                first_playback = time.monotonic()
            await asyncio.sleep(.02)
    task = asyncio.create_task(sink())
    prompt = "请现在用四句完整中文解释双流式语音的工作过程，约一百字，直接回答，不需要查询资料。"
    start = time.monotonic()
    try:
        async with asyncio.timeout(90):
            if version == "v03":
                c.state.listening = True
                await c.episode.speech_activity(False)
                await c.episode.observe("USER_INPUT_FINALIZED", turn_id="synthetic-input", text=prompt,
                                        input_finished_at=time.monotonic())
                while c.episode.pending or c.episode.active_request or c.output.active or c.output.tasks:
                    await asyncio.sleep(.02)
                records = list(c.episode.outputs)
                record = records[-1] if records else {}
                status = record.get("status", "no_speech")
                latency = record.get("latency", {})
                requests = c.episode.request_count
                first_tts_ms = latency.get("llm_request_to_first_tts_text_ms")
            else:
                turn = c.turns.begin()
                ws = TurnWorkspace(turn, committed=True, final_text=prompt)
                c.workspace = ws
                await c._answer(ws, ws.token)
                times = ws.timings
                first_tts_ms = round((times["first_tts_text"] - times["llm_request"]) * 1000) if "first_tts_text" in times else None
                latency = {"llm_request_to_first_tts_text_ms": first_tts_ms}
                status = c.state.current.get("tts_status")
                requests = 1
            first_tts_at = provider.started_at + first_tts_ms / 1000 if first_tts_ms is not None else None
            return {"version": version, "status": status, "requests": requests,
                    "provider_deltas": provider.deltas, "latency": latency,
                    "total_ms": round((time.monotonic() - start) * 1000),
                    "first_pcm_played_ms": round((first_playback - start) * 1000) if first_playback else None,
                    "tts_before_model_finish": bool(first_tts_at and provider.finished_at and first_tts_at < provider.finished_at),
                    "pcm_before_model_finish": bool(first_playback and provider.finished_at and first_playback < provider.finished_at)}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await c.close()
        await tts.close()


async def main(args):
    config = load_config(Path("config/default.yaml"), Path(args.config))
    if args.semantic_only:
        report = await semantic_case(config)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if not report["passed"]:
            raise SystemExit(1)
        return
    report = {"kind": "real providers, synthetic text, in-memory PCM; no physical device evidence", "results": []}
    for attempt in range(1, args.runs + 1):
        for version in ("v02", "v03"):
            try:
                result = await run_case(config, version)
                result["run"] = attempt
                result["passed"] = bool(result.get("status") in {"FINISHED", "completed"}
                                        and result.get("pcm_before_model_finish"))
                if not result["passed"]:
                    result["failure_category"] = "no_speech" if result.get("status") == "no_speech" else "provider_or_runtime"
            except Exception as exc:
                # Never save raw provider bodies or credentials.
                result = {"version": version, "run": attempt, "status": "failed",
                          "passed": False, "failure_category": "service_error",
                          "stage": "run_case", "error_type": type(exc).__name__}
            report["results"].append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["passed"] = bool(report["results"]) and all(r.get("passed") for r in report["results"])
    if not report["passed"]:
        raise SystemExit(1)


async def semantic_case(config):
    """Check semantic waiting with a real model and an unvoiced speech probe."""
    provider = DeepSeekClient(base_url=config.llm.base_url, model=config.llm.model,
        api_key=config.api_key(config.llm.api_key_env), thinking=config.llm.thinking)
    registry = SpeculativeTools(FileTools(config.v02.allowed_roots))
    class Probe:
        active = False
        status = "IDLE"
        speech_id = "probe"
        count = 0
        chars = 0
        def invalidate(self, reason):
            pass
        async def begin(self, identity, turn_id):
            self.count += 1
            return self
        async def feed(self, text):
            self.chars += len(text)
        async def finish(self):
            pass
        async def close(self):
            pass
    probe = Probe()
    events = []
    async def emit(kind, payload):
        if kind == "agent.trace":
            events.append(payload["kind"])
    runtime = AgentEpisodeRuntime(provider, registry, probe, emit, options=config.v03, tools_enabled=False)
    rows = []
    try:
        await runtime.speech_activity(False)
        for index, text in enumerate(("你先别回答，我慢慢说，等我说好了你再回答。",
                                      "首先语音输出要成为工具，SenseVoice 的精修信息也继续利用。我还没说完。",
                                      "好了，你怎么看？请用两句话回答。")):
            before = probe.count
            await runtime.observe("USER_INPUT_FINALIZED", turn_id=str(index), text=text)
            async with asyncio.timeout(45):
                while runtime.pending or runtime.active_request or not runtime.queue.empty():
                    await asyncio.sleep(.01)
            rows.append({"input": index + 1, "new_speak_calls": probe.count - before,
                         "requests_total": runtime.request_count})
        passed = [r["new_speak_calls"] for r in rows] == [0, 0, 1]
        return {"kind": "real model semantic wait; synthetic text; no TTS", "stages": rows,
                "wait_actions": events.count("WAIT"), "speech_chars": probe.chars,
                "passed": passed,
                **({} if passed else {"failure_category": "model_semantic_variance"})}
    finally:
        await runtime.stop()
        registry.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/v0.3.yaml")
    parser.add_argument("--output", default="logs/evaluation/v03-smoke.json")
    parser.add_argument("--semantic-only", action="store_true", help="real-model wait/wait/speak test without TTS")
    parser.add_argument("--runs", type=int, default=1, help="v0.2/v0.3 comparison groups (validation uses 3)")
    logging.disable(logging.CRITICAL)
    asyncio.run(main(parser.parse_args()))

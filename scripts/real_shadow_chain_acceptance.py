"""Live, serial acceptance harness for the production shadow-task bridge.

This intentionally starts ``SessionRuntime`` through its normal production
assembly.  Capture/ASR/TTS/PCM are replaced *only in this harness* so no
microphone is required and no audio is emitted; model calls still use the
configured OpenCode Go DeepSeek endpoint. Web search remains a read-only
external capability; task prompts combine document work with factual search
requests so the harness can observe the configured external path.

The harness is deliberately not a load test.  It executes one semantic turn
at a time and writes an auditable JSON report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

# Running this file directly makes ``scripts`` the import root on Windows.
# Keep the production package importable without asking operators to set
# PYTHONPATH manually.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from voice_terminal.config import AppConfig, load_config


class _FakeCapture:
    def __init__(self, *args, **kwargs) -> None:
        self.started = False
        self._never = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False
        self._never.set()

    async def read_frame(self):
        await self._never.wait()
        raise asyncio.CancelledError


class _FakeVad:
    def __init__(self, *args, **kwargs) -> None: pass
    def reset(self) -> None: pass
    def feed(self, _pcm): return SimpleNamespace(is_speech=False, started=False, ended=False)


class _FakeAsr:
    def __init__(self, *args, **kwargs) -> None:
        self.connected = False
        self.on_result = None

    async def connect(self) -> None: self.connected = True
    async def health(self) -> None: return None
    async def close(self) -> None: self.connected = False
    async def send_audio(self, _pcm) -> None: return None


class _FakePreprocessor:
    enabled = False
    status = "DISABLED"
    error = None
    def __init__(self, *args, **kwargs) -> None: pass
    def reset(self) -> None: pass
    def close(self) -> None: pass
    def process(self, pcm): return pcm
    def push_reverse(self, _pcm) -> None: pass


class _FakeTts:
    def __init__(self, *args, **kwargs) -> None: pass
    async def close(self) -> None: return None


class _FakePlayer:
    running = False
    diagnostics: dict[str, Any] = {"mode": "acceptance_fake", "audio_emitted": False}
    on_playback_started = None
    def __init__(self, *args, **kwargs) -> None: pass
    def start(self) -> None: return None
    def stop(self) -> None: return None
    def block(self, *_args) -> None: return None
    def progress(self, _key=None):
        return {"remaining_ms": 0, "played_ms": 0, "output_estimated_ms": 0}


class _TextHandle:
    def __init__(self, owner, turn_id: str, emotion: str) -> None:
        self.owner = owner
        self.turn_id = turn_id
        self.speech_id = uuid4().hex
        self.text = ""
        self.workspace = SimpleNamespace(generation=owner.generation, timings={})
        self.emotion_applied = emotion

    async def feed(self, text: str) -> None:
        self.text += text
        self.owner.texts.append(text)

    async def finish(self) -> None:
        self.owner.finished.append(self.text)
        # Text interception is the TTS completion boundary.  Do not leave a
        # synthetic queued item that would suppress the next real scheduler
        # dispatch as if PCM were still playing.
        self.owner.handle = None


class _TextOnlyOutput:
    """TTS boundary replacement: collect text and retain HOLD state only."""
    def __init__(self) -> None:
        self.runtime = None
        self.generation = 0
        self.held = False
        self.texts: list[str] = []
        self.finished: list[str] = []
        self.handle = None

    @property
    def active(self) -> bool: return self.handle is not None
    @property
    def status(self) -> str: return "HELD" if self.held and self.handle else "IDLE"
    @property
    def speak_snapshot(self) -> dict[str, Any]:
        text = self.handle.text if self.handle else ""
        return {"queue_status": "empty" if not self.handle else "queued", "text": text,
                "speech_id": self.handle.speech_id if self.handle else None,
                "output_generation": self.generation, "text_only_queue": True}

    def hold_unplayed(self): self.held = True; return self.handle.speech_id if self.handle else None
    def release_held(self): self.held = False
    async def begin(self, _identity, turn_id, *, emotion="calm"):
        self.generation += 1
        self.handle = _TextHandle(self, turn_id, emotion)
        return self.handle
    async def replace_current(self, identity, turn_id, *, emotion="calm"):
        return await self.begin(identity, turn_id, emotion=emotion)
    async def append_successor(self, identity, turn_id, **kwargs):
        return await self.begin(identity, turn_id, emotion=kwargs.get("emotion", "calm"))
    def cancel_all(self, _reason): self.handle = None
    async def close(self): self.handle = None


@dataclass
class TurnResult:
    round: int
    mode: str
    input: str
    speaking_at_dispatch: bool
    llm_requests: int = 0
    receipts: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    tts_text: list[str] = field(default_factory=list)
    tts_held: bool = False
    terminal_mailbox: bool = False
    failed: str | None = None


READ_ONLY_TURNS = [
    ("balanced", "请先简要说明你会如何帮助我核对 docs 里的架构说明。"),
    ("balanced", "我想了解今天 OpenAI 官方是否发布了与 Responses API 有关的新公告，请核对公开资料后给我一句结论和来源。"),
    ("balanced", "请帮我从 docs/architecture.md 找到影子 Agent 的职责边界，并用一句话说明。"),
    ("balanced", "请查看文档中 task 的 request、modify、cancel 语义，整理成简短说明。"),
    ("balanced", "请帮我确认今天香港的天气预报是否有台风或暴雨警告，给我一个简短的出行判断。"),
    ("balanced", "我需要知道 stop 与 reset 对影子任务分别有什么影响，请根据已有文档说明。"),
    ("balanced", "请从项目文档里确认影子 Agent 目前有哪些只读能力。"),
    ("balanced", "我想知道今天比特币现货价格大约是多少，请核对公开实时资料后只给范围和时间点。"),
    ("balanced", "我在准备验收，请从 docs 中找一下影子任务事件的几种状态。"),
    ("balanced", "请对照文档说明为什么影子任务回传内容需要被当作不可信数据。"),
    ("deep", "请帮我查证本周 OpenAI 官方文档是否更新了 Agents 或 Responses 的使用说明，并说明值得注意的一点。"),
    ("deep", "请根据 docs 的说明，分析 task receipt 与 terminal event 的不同作用。"),
    ("deep", "请查阅文档，解释为什么 task 可以在用户仍在讲话时受理。"),
    ("deep", "我需要了解最近一天英伟达的公开股价表现，请依据公开市场资料概括涨跌和数据时间。"),
    ("deep", "请查看文档，给出当前 external 与 internal 能力域的区别。"),
    ("deep", "请查一下今天北京到上海是否有大范围航班延误的公开信息，并说明信息是否足够可靠。"),
    ("deep", "请根据当前架构文档说明，终态事件为什么不能直接请求主 LLM。"),
    ("deep", "我需要复核 reset 后的任务 ID 行为，请从项目文档中确认。"),
    ("deep", "请查阅 docs，概括 debug WebSocket sandbox 与生产任务的隔离边界。"),
    ("deep", "请基于项目文档，说明未来加入有副作用工具时需要增加什么边界。"),
]


async def _wait_for(predicate, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(): return True
        await asyncio.sleep(.1)
    return predicate()


async def run(report_path: Path, turns: int) -> int:
    # These only replace physical I/O.  SessionRuntime, EpisodeController,
    # AgentDecisionScheduler, real main/shadow LLM providers, and the shadow
    # tool executor remain the production instances.
    import voice_terminal.server.websocket as ws
    ws.AudioCapture = _FakeCapture
    ws.FsmnVadDetector = _FakeVad
    ws.AsrWorkerClient = _FakeAsr
    ws.AudioPreprocessor = _FakePreprocessor
    ws.MiniMaxTtsClient = _FakeTts
    ws.PcmPlayer = _FakePlayer

    # OpenCode Go is explicitly selected for both main and inherited shadow
    # provider. Network search remains enabled but read-only; no external
    # write, command, message, or account-facing capability is exposed.
    old_provider = os.environ.get("VOICE_LLM_PROVIDER")
    os.environ["VOICE_LLM_PROVIDER"] = "opencode_go"
    base = load_config(Path("config/default.yaml"), Path("config/local.yaml"))
    raw = {"llm": {"provider": "opencode_go", "transport": "responses"},
           "shadow_agent": {"enabled": True,
               "tools": {"files_enabled": True, "allowed_roots": ["docs"],
                 "web_search": {"enabled": True}}}}
    overrides = AppConfig.from_mapping(raw, base_dir=Path.cwd())
    # Preserve local timing / agent values while forcing safe provider/tool lane.
    config = replace(base, llm=overrides.llm, shadow_agent=overrides.shadow_agent)
    runtime = ws.SessionRuntime(config)
    report: dict[str, Any] = {"started_at": time.time(), "provider": config.llm.provider,
        "base_url": config.llm.base_url, "model": config.llm.model,
        "shadow_base_url": config.shadow_agent.provider.base_url,
        "web_search_enabled": config.shadow_agent.tools.web_search.enabled,
        "web_search_calls": [],
        "tts_boundary": "text-only fake; no PCM/TTS service calls", "rounds": []}
    try:
        if not config.api_key("OPENCODE_GO_API_KEY"):
            raise RuntimeError("OPENCODE_GO_API_KEY is absent")
        await runtime.initialize()
        if runtime.llm is None or runtime.shadow_session is None:
            raise RuntimeError(f"required providers unavailable: {runtime.state.services}")
        await runtime.start_listening()
        if not runtime.state.listening:
            raise RuntimeError(f"production session did not start: {runtime.state.services}")
        fake_output = _TextOnlyOutput()
        runtime.controller.output = fake_output
        runtime.controller.episode.output = fake_output
        fake_output.runtime = runtime.controller.episode
        # Preserve the real MiniMax backend while auditing only a safe result
        # summary.  No API key or full external page/search payload is stored.
        if runtime.shadow_search_tools is not None:
            original_search_execute = runtime.shadow_search_tools.execute

            def observed_search_execute(name, arguments):
                query = (str(arguments.get("query") or "")[:256]
                         if isinstance(arguments, dict) else "")
                try:
                    result = original_search_execute(name, arguments)
                except Exception as exc:
                    report["web_search_calls"].append({
                        "query": query, "ok": False,
                        "error": " ".join(str(exc).split())[:300],
                    })
                    raise
                organic = result.get("organic") if isinstance(result, dict) else []
                titles = []
                if isinstance(organic, list):
                    for item in organic[:3]:
                        if isinstance(item, dict) and isinstance(item.get("title"), str):
                            titles.append(item["title"][:180])
                report["web_search_calls"].append({
                    "query": query,
                    "organic_count": len(organic) if isinstance(organic, list) else 0,
                    "title_samples": titles,
                    "ok": isinstance(result, dict),
                })
                return result

            runtime.shadow_search_tools.execute = observed_search_execute
        request_count = 0
        original_stream = runtime.llm.stream_events
        async def counted_stream(*args, **kwargs):
            nonlocal request_count
            request_count += 1
            async for item in original_stream(*args, **kwargs):
                yield item
        runtime.llm.stream_events = counted_stream
        event_log: list[dict[str, Any]] = []
        original_accept = runtime.controller.accept_shadow_task_events
        async def observed_accept(events):
            for item in events:
                event_log.append({"id": item.id, "status": item.status, "text": item.text})
            await original_accept(events)
        runtime.controller.accept_shadow_task_events = observed_accept

        for index, (mode, text) in enumerate(READ_ONLY_TURNS[:turns], 1):
            result = TurnResult(index, mode, text, speaking_at_dispatch=(index % 2 == 1))
            before_requests, before_events, before_records, before_tts = request_count, len(event_log), len(runtime.history.records), len(fake_output.finished)
            try:
                await runtime.set_reasoning_mode(mode)
                controller = runtime.controller
                episode = controller.episode
                if result.speaking_at_dispatch:
                    await episode.speech_activity(True)
                # Use the production controller's turn allocation and semantic
                # transcript projection, while replacing only the PCM source.
                from voice_terminal.controller.turn_workspace import AudioFrame
                frame = AudioFrame(b"\x00\x00" * 160, index * 160, time.monotonic())
                await controller._new_turn(frame)
                turn_id = controller.workspace.turn_id
                await controller._detail("transcript.segment", text=text,
                                         changed_ids=[1])
                await asyncio.sleep(.65)  # Cross the real 400ms scheduler tick.
                result.tts_held = bool(fake_output.held)
                if result.speaking_at_dispatch:
                    await episode.speech_activity(False)
                await episode.observe("USER_INPUT_FINALIZED", turn_id=turn_id, text=text)
                await _wait_for(lambda: not episode.active_request and request_count > before_requests, 12)
                # A task response can outlive the voice decision.  Wait up to
                # 10 seconds for a terminal event, without adding synthetic work.
                await _wait_for(lambda: any(e["status"] in {"completed", "failed", "timed_out", "cancelled"}
                                             for e in event_log[before_events:]), 10)
                records = runtime.history.records[before_records:]
                for record in records:
                    if record.kind == "shadow_task_receipt": result.receipts.append(record.to_payload())
                result.events = event_log[before_events:]
                result.terminal_mailbox = any(e["status"] in {"completed", "failed", "timed_out", "cancelled"} for e in result.events)
                result.tts_text = fake_output.finished[before_tts:]
                if controller.workspace is not None:
                    await controller._cancel_turn(reason="completed", record=False)
            except Exception as exc:
                result.failed = f"{type(exc).__name__}: {str(exc)[:500]}"
            result.llm_requests = request_count - before_requests
            report["rounds"].append(asdict(result))
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            # Keep it serial: no new semantic round begins until all current
            # provider work has had a chance to settle.
            await asyncio.sleep(.2)
    finally:
        report["finished_at"] = time.time()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await runtime.close()
        if old_provider is None: os.environ.pop("VOICE_LLM_PROVIDER", None)
        else: os.environ["VOICE_LLM_PROVIDER"] = old_provider
    failures = [x for x in report["rounds"] if x.get("failed")]
    terminals = sum(1 for x in report["rounds"] if x.get("terminal_mailbox"))
    print(json.dumps({"rounds": len(report["rounds"]), "failures": len(failures), "terminals": terminals,
                      "report": str(report_path)}, ensure_ascii=False))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=20)
    parser.add_argument("--report", type=Path, default=Path("reports/live-shadow-chain-acceptance.json"))
    args = parser.parse_args()
    if args.turns < 20:
        parser.error("live acceptance requires at least 20 serial rounds")
    return asyncio.run(run(args.report, args.turns))


if __name__ == "__main__":
    raise SystemExit(main())

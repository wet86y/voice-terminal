"""Real serial-group acceptance of concurrent shadow tasks.

Each group enters production ``SessionRuntime`` through the normal controller
and scheduler, but substitutes only physical ASR/TTS/PCM boundaries.  The
main and shadow models are real OpenCode Go DeepSeek Responses calls.  All
enabled shadow capabilities are read-only (``docs`` files and public web
search); no write, command, message, or account-facing tool is exposed.
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_terminal.config import AppConfig, load_config
from real_shadow_chain_acceptance import (
    _FakeAsr, _FakeCapture, _FakePlayer, _FakePreprocessor, _FakeTts,
    _FakeVad, _TextOnlyOutput,
)


# These are ordinary user requests with several independent outcomes.  They
# name goals and evidence needs, never action names or function-call syntax.
GROUPS = [
    ("balanced", "我在整理发布前核对：请分别确认项目文档里影子任务的职责边界、task 的取消规则，以及今天 OpenAI 官方是否有与 Responses API 相关的新公告，最后汇总三点。"),
    ("balanced", "请帮我同时准备两项互不依赖的材料：从项目文档核对讲话中预计算的限制；再确认今天香港是否有台风或暴雨警告。请分别并行核对，让两项结果独立回来。"),
    ("balanced", "我需要并行核对架构和外部情况：项目里终态事件如何回到主调度；以及今天比特币现货价格大约在哪个范围。请分别给结论和时间。"),
    ("balanced", "请分别查清 docs 中 external 与 internal 能力域的区别，并核实本周 OpenAI 官方文档有没有 Agents 或 Responses 的更新；不要把两件事混在一起。"),
    ("balanced", "我准备做运行验收，有三项互不依赖的核对：影子任务事件状态、reset 后 ID 规则、今天北京到上海是否有大范围航班延误。请分别并行核对，不要等一项完成再开始另一项。"),
    ("balanced", "请把下面几件独立事情分别核对：文档中 receipt 和 terminal event 的不同；影子工具并发限制；以及最近一天英伟达公开股价的涨跌。"),
    ("balanced", "请分别完成文档核对和外部核对：stop 与 reset 对后台任务的差别；今天上海天气是否有明显出行风险。最后用分点给我。"),
    ("balanced", "我想同时确认两类事实：影子回传为什么必须视为不可信数据；今天公开资料里是否有重要的 OpenAI 产品更新。请各自说明来源。"),
    ("balanced", "请分别查阅项目文档中的 debug sandbox 隔离边界，以及今天美元兑人民币公开汇率的大致范围；不要用一项推断另一项。"),
    ("balanced", "请同时帮我核对 docs 里未来副作用工具需要的授权边界，以及今天国内主要指数的公开收盘表现；分别给简短判断。"),
    ("deep", "请深入但分别并行处理三项互不依赖的事情：影子组件和语音链的依赖方向；终态 mailbox 的 400ms 回流规则；以及本周 OpenAI 官方是否有 Responses 平台变更。每项独立回传。"),
    ("deep", "请分别研究项目文档里的 modify/revision 防迟到规则和讲话中 HELD 的语义，再核对今天香港天气警告；最后将三个结论分开写。"),
    ("deep", "我需要两份独立核查：影子 agent 首期只读工具有哪些、哪些尚未开放；以及今日比特币公开价格与数据时间。请不要把内部文档当作市场依据。"),
    ("deep", "请分别核对任务 30 秒截止与 checkpoint 的关系、WebSocket debug sandbox 的生命周期，以及最近一天英伟达股价公开表现。"),
    ("deep", "请同时完成以下三项独立调查并让它们并行推进：docs 中 session.stop 对影子任务的处理；session.reset 的处理；今天北京到上海航班延误的公开可靠信息。"),
    ("deep", "请分别解释主 LLM 为什么不直接拥有文件和搜索能力、影子 external/internal 的权限分层，并查证今天 OpenAI 官方动态是否与开发者 API 有关。"),
    ("deep", "我希望分别核验：影子任务 ID 的连续性和容量规则；事件缓冲的终态优先；以及今天上海天气的公开出行提示。"),
    ("deep", "请并行整理项目里 task receipt 不唤醒而 terminal 会唤醒的原因、TTS 在讲话中为何 HELD；再确认今天比特币价格公开范围。"),
    ("deep", "请分别调查 docs 中生产 manager 与 debug manager 的隔离、只读工具并发槽的限制，以及今天公开市场中美元兑人民币的大致水平。"),
    ("deep", "请做三项彼此独立的核对：未来外部副作用工具的确认边界、当前文件读取的安全范围、以及本周 OpenAI 官方开发者公告。"),
]


@dataclass
class Group:
    number: int
    mode: str
    input: str
    speaking_at_dispatch: bool
    actions: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    events_by_id: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    task_ids: list[int] = field(default_factory=list)
    terminal_ids: list[int] = field(default_factory=list)
    tts_held: bool = False
    tts_text: list[str] = field(default_factory=list)
    llm_requests: int = 0
    web_search_calls: list[dict[str, Any]] = field(default_factory=list)
    exception: str | None = None


async def wait_until(predicate, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(.1)
    return bool(predicate())


async def run(report_path: Path, *, groups=GROUPS) -> int:
    import voice_terminal.server.websocket as ws
    ws.AudioCapture, ws.FsmnVadDetector, ws.AsrWorkerClient = _FakeCapture, _FakeVad, _FakeAsr
    ws.AudioPreprocessor, ws.MiniMaxTtsClient, ws.PcmPlayer = _FakePreprocessor, _FakeTts, _FakePlayer
    old = os.environ.get("VOICE_LLM_PROVIDER")
    os.environ["VOICE_LLM_PROVIDER"] = "opencode_go"
    base = load_config(Path("config/default.yaml"), Path("config/local.yaml"))
    override = AppConfig.from_mapping({"llm": {"provider": "opencode_go", "transport": "responses"},
        "shadow_agent": {"enabled": True, "tools": {"files_enabled": True, "allowed_roots": ["docs"],
            "web_search": {"enabled": True}}}}, base_dir=Path.cwd())
    config = replace(base, llm=override.llm, shadow_agent=override.shadow_agent)
    report: dict[str, Any] = {"started_at": time.time(), "provider": config.llm.provider,
        "base_url": config.llm.base_url, "model": config.llm.model, "web_search_enabled": True,
        "tts_boundary": "text-only fake; no PCM/TTS", "groups": [], "web_search_calls": []}
    runtime = ws.SessionRuntime(config)
    try:
        if not config.api_key("OPENCODE_GO_API_KEY"):
            raise RuntimeError("OPENCODE_GO_API_KEY is absent")
        await runtime.initialize()
        if runtime.llm is None or runtime.shadow_session is None:
            raise RuntimeError(f"required providers unavailable: {runtime.state.services}")
        await runtime.start_listening()
        if not runtime.state.listening:
            raise RuntimeError(f"session failed to start: {runtime.state.services}")
        out = _TextOnlyOutput()
        runtime.controller.output = out
        runtime.controller.episode.output = out
        out.runtime = runtime.controller.episode
        request_count = 0
        original_stream = runtime.llm.stream_events
        async def counted(*args, **kwargs):
            nonlocal request_count
            request_count += 1
            async for item in original_stream(*args, **kwargs):
                yield item
        runtime.llm.stream_events = counted
        event_log: list[dict[str, Any]] = []
        original_accept = runtime.controller.accept_shadow_task_events
        async def accept(events):
            for item in events:
                event_log.append({"id": item.id, "status": item.status, "text": item.text[:2000]})
            await original_accept(events)
        runtime.controller.accept_shadow_task_events = accept
        if runtime.shadow_search_tools is not None:
            search = runtime.shadow_search_tools
            original_execute = search.execute
            def spy(name, arguments):
                query = str(arguments.get("query") or "")[:256] if isinstance(arguments, dict) else ""
                try:
                    value = original_execute(name, arguments)
                except Exception as exc:
                    report["web_search_calls"].append({"query": query, "ok": False,
                        "error": " ".join(str(exc).split())[:300]})
                    raise
                organic = value.get("organic") if isinstance(value, dict) else []
                summary = {"query": query, "ok": isinstance(value, dict),
                    "organic_count": len(organic) if isinstance(organic, list) else 0,
                    "title_samples": [str(x.get("title"))[:180] for x in organic[:3]
                                      if isinstance(x, dict) and isinstance(x.get("title"), str)]}
                report["web_search_calls"].append(summary)
                return value
            search.execute = spy

        action_log: list[dict[str, Any]] = []
        episode = runtime.controller.episode
        original_submit = episode._submit_task_actions
        async def submit(identity, turn_id, actions):
            for action in actions:
                args = dict(action.arguments) if isinstance(action.arguments, dict) else {}
                action_log.append({"turn_id": turn_id, "name": action.name,
                                   "id": args.get("id"), "state": args.get("state"),
                                   "text": str(args.get("text") or "")[:500]})
            return await original_submit(identity, turn_id, actions)
        episode._submit_task_actions = submit

        from voice_terminal.controller.turn_workspace import AudioFrame
        for number, (mode, text) in enumerate(groups, 1):
            item = Group(number, mode, text, speaking_at_dispatch=(number % 2 == 1))
            before_actions, before_events, before_entries = len(action_log), len(event_log), len(runtime.history.entries)
            before_tts, before_web, before_requests = len(out.finished), len(report["web_search_calls"]), request_count
            try:
                await runtime.set_reasoning_mode(mode)
                if item.speaking_at_dispatch:
                    await episode.speech_activity(True)
                frame = AudioFrame(b"\0\0" * 160, number * 160, time.monotonic())
                await runtime.controller._new_turn(frame)
                turn_id = runtime.controller.workspace.turn_id
                await runtime.controller._detail("transcript.segment", text=text, changed_ids=[1])
                await asyncio.sleep(.65)
                item.tts_held = bool(out.held)
                if item.speaking_at_dispatch:
                    await episode.speech_activity(False)
                await episode.observe("USER_INPUT_FINALIZED", turn_id=turn_id, text=text)
                await wait_until(lambda: not episode.active_request and request_count > before_requests, 15)
                # Include all natural task submissions resulting from the
                # first decision and immediate terminal follow-up decisions.
                await asyncio.sleep(.5)
                # First-decision actions determine the wait set.  A terminal
                # may itself cause another normal scheduler decision, whose
                # later task actions are retained in the final trace below.
                initial_actions = action_log[before_actions:]
                item.task_ids = sorted({int(x["id"]) for x in initial_actions if isinstance(x.get("id"), int)})
                await wait_until(lambda: not item.task_ids or all(any(
                    event["id"] == ident and event["status"] in {"completed", "cancelled", "timed_out", "failed"}
                    for event in event_log[before_events:]) for ident in item.task_ids), 34)
                for event in event_log[before_events:]:
                    item.events_by_id.setdefault(str(event["id"]), []).append(event)
                item.terminal_ids = sorted(int(key) for key, values in item.events_by_id.items()
                                           if any(e["status"] in {"completed", "cancelled", "timed_out", "failed"} for e in values))
                for entry in runtime.history.entries[before_entries:]:
                    if entry.kind == "shadow_task_receipt":
                        item.receipts.append(dict(entry.content))
                item.actions = action_log[before_actions:]
                item.tts_text = out.finished[before_tts:]
                item.web_search_calls = report["web_search_calls"][before_web:]
                if runtime.controller.workspace is not None:
                    await runtime.controller._cancel_turn(reason="completed", record=False)
            except Exception as exc:
                item.exception = f"{type(exc).__name__}: {str(exc)[:500]}"
            item.llm_requests = request_count - before_requests
            report["groups"].append(asdict(item))
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            await asyncio.sleep(.2)
    finally:
        report["finished_at"] = time.time()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await runtime.close()
        if old is None:
            os.environ.pop("VOICE_LLM_PROVIDER", None)
        else:
            os.environ["VOICE_LLM_PROVIDER"] = old
    print(json.dumps({"groups": len(report["groups"]), "web_calls": len(report["web_search_calls"]),
        "report": str(report_path)}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=Path("reports/live-shadow-parallel-acceptance.json"))
    parser.add_argument("--group", type=int, default=None,
                        help="Run one numbered acceptance group in an isolated recovery session.")
    args = parser.parse_args()
    selected = GROUPS
    if args.group is not None:
        if not 1 <= args.group <= len(GROUPS):
            parser.error(f"--group must be between 1 and {len(GROUPS)}")
        selected = [GROUPS[args.group - 1]]
    return asyncio.run(run(args.report, groups=selected))


if __name__ == "__main__":
    raise SystemExit(main())

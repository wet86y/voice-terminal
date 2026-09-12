"""Focused live OpenCode Go acceptance for clarification and task(reply).

This is intentionally only a two-turn supplement to the 20-round serial
acceptance.  It uses the production SessionRuntime/controller/episode and the
real OpenCode Go main and shadow Responses providers; only physical audio/TTS
boundaries are replaced by the local text harness.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_terminal.config import AppConfig, load_config

from real_shadow_chain_acceptance import (
    _FakeAsr, _FakeCapture, _FakePlayer, _FakePreprocessor, _FakeTts, _FakeVad,
    _TextOnlyOutput,
)


async def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return bool(predicate())


async def run(report_path: Path) -> int:
    import voice_terminal.server.websocket as ws
    from voice_terminal.controller.turn_workspace import AudioFrame

    ws.AudioCapture, ws.FsmnVadDetector, ws.AsrWorkerClient = _FakeCapture, _FakeVad, _FakeAsr
    ws.AudioPreprocessor, ws.MiniMaxTtsClient, ws.PcmPlayer = _FakePreprocessor, _FakeTts, _FakePlayer
    old_provider = os.environ.get("VOICE_LLM_PROVIDER")
    os.environ["VOICE_LLM_PROVIDER"] = "opencode_go"
    base = load_config(Path("config/default.yaml"), Path("config/local.yaml"))
    override = AppConfig.from_mapping({
        "llm": {"provider": "opencode_go", "transport": "responses"},
        "shadow_agent": {"enabled": True, "tools": {
            "files_enabled": True, "allowed_roots": ["docs"],
            "web_search": {"enabled": True},
        }},
    }, base_dir=Path.cwd())
    config = base.__class__(**{**base.__dict__, "llm": override.llm,
                               "shadow_agent": override.shadow_agent})
    runtime = ws.SessionRuntime(config)
    report: dict = {
        "started_at": time.time(), "provider": config.llm.provider,
        "base_url": config.llm.base_url, "model": config.llm.model,
        "shadow_base_url": config.shadow_agent.provider.base_url,
        "rounds": [], "actions": [], "events": [], "errors": [],
        "tts_boundary": "text-only fake; no PCM/TTS service calls",
    }
    request_count = 0
    try:
        if not config.api_key("OPENCODE_GO_API_KEY"):
            raise RuntimeError("OPENCODE_GO_API_KEY is absent")
        await runtime.initialize()
        if runtime.llm is None or runtime.shadow_session is None:
            raise RuntimeError(f"required providers unavailable: {runtime.state.services}")
        await runtime.start_listening()
        if not runtime.state.listening:
            raise RuntimeError(f"session failed to start: {runtime.state.services}")

        output = _TextOnlyOutput()
        runtime.controller.output = output
        runtime.controller.episode.output = output
        output.runtime = runtime.controller.episode
        episode = runtime.controller.episode

        original_stream = runtime.llm.stream_events

        async def counted_stream(*args, **kwargs):
            nonlocal request_count
            request_count += 1
            async for item in original_stream(*args, **kwargs):
                yield item

        runtime.llm.stream_events = counted_stream
        original_accept = runtime.controller.accept_shadow_task_events

        async def observed_accept(events):
            for item in events:
                report["events"].append({
                    "id": item.id, "status": item.status,
                    "question_id_present": bool(item.question_id),
                    "text": item.text[:1000],
                })
            await original_accept(events)

        runtime.controller.accept_shadow_task_events = observed_accept
        original_submit = episode._submit_task_actions

        async def observed_submit(identity, turn_id, actions):
            for action in actions:
                args = dict(action.arguments) if isinstance(action.arguments, dict) else {}
                report["actions"].append({
                    "turn_id": turn_id, "name": action.name,
                    "id": args.get("id"), "state": args.get("state"),
                    "question_id_present": bool(args.get("question_id")),
                    "text": str(args.get("text") or "")[:500],
                })
            return await original_submit(identity, turn_id, actions)

        episode._submit_task_actions = observed_submit

        prompts = [
            "请启动一个影子任务帮我查天气，但我暂时不提供城市和日期。城市和日期会实质改变结果，请让影子 Agent 先通过 clarification 问我，不要猜测。",
            "我选择北京，查明天的天气；请把这个自然语言回答转交给刚才等待中的影子任务继续完成。",
        ]
        from time import monotonic
        for number, text in enumerate(prompts, 1):
            before_requests = request_count
            before_events = len(report["events"])
            before_actions = len(report["actions"])
            before_tts = len(output.finished)
            try:
                frame = AudioFrame(b"\0\0" * 160, number * 160, monotonic())
                await runtime.controller._new_turn(frame)
                turn_id = runtime.controller.workspace.turn_id
                await runtime.controller._detail("transcript.segment", text=text, changed_ids=[1])
                await asyncio.sleep(0.65)
                await episode.observe("USER_INPUT_FINALIZED", turn_id=turn_id, text=text)
                if number == 1:
                    ok = await _wait_for(
                        lambda: any(event["status"] == "awaiting_user"
                                    for event in report["events"][before_events:]), 35,
                    )
                else:
                    ok = await _wait_for(
                        lambda: any(event["status"] in {"completed", "failed", "timed_out", "cancelled"}
                                    for event in report["events"][before_events:]), 35,
                    )
                report["rounds"].append({
                    "round": number, "input": text,
                    "llm_requests": request_count - before_requests,
                    "actions": report["actions"][before_actions:],
                    "events": report["events"][before_events:],
                    "tts_text": output.finished[before_tts:],
                    "settled": ok,
                })
                if not ok:
                    report["errors"].append({"round": number, "category": "timeout_waiting_for_expected_event"})
                if runtime.controller.workspace is not None:
                    await runtime.controller._cancel_turn(reason="completed", record=False)
            except Exception as exc:
                report["errors"].append({"round": number, "category": type(exc).__name__,
                                         "message": str(exc)[:500]})
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            await asyncio.sleep(0.2)
    except Exception as exc:
        report["errors"].append({"category": type(exc).__name__, "message": str(exc)[:500]})
    finally:
        report["finished_at"] = time.time()
        report["llm_requests"] = request_count
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await runtime.close()
        if old_provider is None:
            os.environ.pop("VOICE_LLM_PROVIDER", None)
        else:
            os.environ["VOICE_LLM_PROVIDER"] = old_provider
    summary = {"rounds": len(report["rounds"]), "llm_requests": request_count,
               "actions": len(report["actions"]), "events": len(report["events"]),
               "errors": len(report["errors"]), "report": str(report_path)}
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if report["errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path,
                        default=Path("reports/real-opencode-go-shadow-clarification-acceptance-20260912.json"))
    return asyncio.run(run(parser.parse_args().report))


if __name__ == "__main__":
    raise SystemExit(main())

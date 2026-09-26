from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.continuity import ContinuityManager
from voice_terminal.session.context_frames import ContextLane
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def test_continuity_replaces_document_with_bounded_result_and_clears_empty(tmp_path: Path):
    async def scenario() -> None:
        changed = asyncio.Event()
        results = ["甲" * 900, ""]

        async def summarize(_snapshot, _previous):
            return results.pop(0)

        manager = ContinuityManager(
            tmp_path / "continuity.json", summarize,
            on_document_changed=lambda _document: changed.set(),
            idle_seconds=0,
        )
        manager.document = "旧文档"
        manager.arm(time.time() - 1, lambda: [{"kind": "input", "content": "新内容"}])
        await asyncio.wait_for(changed.wait(), timeout=1)
        assert len(manager.document) == 800
        assert json.loads((tmp_path / "continuity.json").read_text(encoding="utf-8"))["document"] == manager.document

        changed.clear()
        manager.arm(time.time() - 1, lambda: [])
        await asyncio.wait_for(changed.wait(), timeout=1)
        assert manager.document == ""
        assert json.loads((tmp_path / "continuity.json").read_text(encoding="utf-8"))["document"] == ""
        await manager.close()

    asyncio.run(scenario())


def test_continuity_uses_pre_reset_snapshot_and_user_activity_cancels_summary(tmp_path: Path):
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        captured: list[dict] = []

        async def summarize(snapshot, _previous):
            captured.extend(snapshot)
            started.set()
            await release.wait()
            return "过期整理结果"

        manager = ContinuityManager(
            tmp_path / "continuity.json", summarize, idle_seconds=0,
        )
        manager.document = "仍有效"
        manager.arm(time.time() - 1, lambda: [{"content": "重置后的空窗口"}])
        manager.capture_before_reset([{"content": "窗口重置前快照"}])
        await asyncio.wait_for(started.wait(), timeout=1)
        assert captured == [{"content": "窗口重置前快照"}]

        manager.user_started()
        release.set()
        await asyncio.sleep(0)
        assert manager.document == "仍有效"
        await manager.close()

    asyncio.run(scenario())


def test_voice_request_includes_continuity_and_scheduled_wake_in_request_tail():
    class Registry:
        def action_definitions(self):
            return []

        def begin_turn(self, _episode_id):
            return None

        def cancel_turn(self, _episode_id):
            return None

    async def emit(*_args):
        return None

    async def close_output():
        return None

    async def scenario() -> None:
        runtime = AgentEpisodeRuntime(
            None, Registry(), SimpleNamespace(
                status="IDLE", speak_snapshot={"queue_status": "empty", "text_only_queue": True},
                cancel_all=lambda *_args: None,
                close=close_output,
            ), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
            tools_enabled=False,
            continuity_document_provider=lambda: "用户正在准备发布版本。",
        )
        await runtime.ensure_episode()
        messages = runtime.messages(
            {"SCHEDULED_WAKE"},
            force_context={"scheduled_wakes": [{"task_id": "wake-1", "text": "提醒用户检查发布状态"}]},
        )
        tail = [item for item in messages if item.get("context_lane") == ContextLane.REQUEST_TAIL]
        rendered = "\n".join(item["content"] for item in tail)
        assert "用户正在准备发布版本" in rendered
        assert "提醒用户检查发布状态" in rendered
        await runtime.stop()

    asyncio.run(scenario())


def test_kws_silence_anchor_callback_uses_playback_completion_wall_time():
    anchors: list[float] = []

    class Registry:
        def action_definitions(self):
            return []

        def begin_turn(self, _episode_id):
            return None

        def cancel_turn(self, _episode_id):
            return None

    async def emit(*_args):
        return None

    async def close_output():
        return None

    async def scenario() -> None:
        runtime = AgentEpisodeRuntime(
            None, Registry(), SimpleNamespace(
                status="IDLE", speak_snapshot={"queue_status": "empty", "text_only_queue": True},
                cancel_all=lambda *_args: None,
                close=close_output,
            ), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
            defer_silence_until_output=True,
            on_silence_anchor=anchors.append,
        )
        runtime.episode_id = runtime.conversation.conversation_id
        runtime.last_turn_id = "turn-a"
        runtime.inputs["turn-a"] = {"text": "你好", "final": False}
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="turn-a", text="你好")
        pending = runtime._pending_silence_anchor
        assert pending is not None
        pending["request_id"] = "request-a"
        expected_wall_time = time.time() + 30
        assert await runtime.playback_completed(
            turn_id="turn-a", conversation_id=runtime.episode_id,
            request_id="request-a",
            playback_completed_at=pending["user_silence_monotonic"] + 1,
            wall_time=expected_wall_time,
        )
        assert anchors == [expected_wall_time]
        await runtime.stop()

    asyncio.run(scenario())

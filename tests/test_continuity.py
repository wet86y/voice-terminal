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


def test_continuity_keeps_complete_document_and_clears_explicit_empty(tmp_path: Path):
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
        assert len(manager.document) == 900
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


def test_continuity_delegates_commit_and_rejects_activity_during_summary(tmp_path: Path):
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        committed: list[str] = []
        manager: ContinuityManager

        async def summarize(_snapshot, _previous):
            started.set()
            await release.wait()
            return "不应提交"

        async def commit(document: str, generation: int) -> bool:
            if manager.commit_document(document, generation):
                committed.append(document)
                return True
            return False

        manager = ContinuityManager(
            tmp_path / "continuity.json", summarize, on_organized=commit, idle_seconds=0,
        )
        manager.arm(time.time() - 1, list)
        await asyncio.wait_for(started.wait(), timeout=1)
        manager.user_started()
        release.set()
        await asyncio.sleep(0)
        assert committed == []
        assert not (tmp_path / "continuity.json").exists()
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
        document = ("已完成的背景资料。" * 120) + "\n发布尚未执行；用户要求等到再次明确确认后恢复。归档：D:/history/archives/task/voice.md"
        runtime = AgentEpisodeRuntime(
            None, Registry(), SimpleNamespace(
                status="IDLE", speak_snapshot={"queue_status": "empty", "text_only_queue": True},
                cancel_all=lambda *_args: None,
                close=close_output,
            ), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
            tools_enabled=False,
            continuity_document_provider=lambda: document,
        )
        await runtime.ensure_episode()
        messages = runtime.messages(
            {"SCHEDULED_WAKE"},
            force_context={"scheduled_wakes": [{"task_id": "wake-1", "text": "提醒用户检查发布状态"}]},
        )
        tail = [item for item in messages if item.get("context_lane") == ContextLane.REQUEST_TAIL]
        rendered = "\n".join(item["content"] for item in tail)
        # 整份交接进入请求；尾部的恢复条件和归档路径不能被前 800 字符裁掉。
        assert len(document) > 800 and document in rendered
        assert "历史摘要，不是本次用户发言" in rendered
        assert "提醒用户检查发布状态" in rendered
        static = [item for item in messages if item.get("context_lane") == ContextLane.STATIC_INSTRUCTIONS]
        assert all(document not in item["content"] for item in static)
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

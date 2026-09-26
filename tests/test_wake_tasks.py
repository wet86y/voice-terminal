from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from voice_terminal.wake_tasks import ScheduledWakeManager


def test_wake_creation_edit_and_reference_are_independent(tmp_path: Path):
    async def no_delivery(_task):
        return False

    async def scenario() -> None:
        manager = ScheduledWakeManager(
            tmp_path / "wake-tasks.json", deliver_voice=no_delivery, deliver_shadow=no_delivery,
        )
        created = await manager.create({
            "target": "shadow_agent",
            "when": (datetime.now().astimezone() + timedelta(hours=1)).isoformat(),
            "repeat": "1h",
            "text": "检查尚未完成的整理工作",
            "reference": '[{"role":"user","text":"冻结的原始上下文"}]',
        })
        updated = await manager.update(created["task_id"], {
            "target": "both", "text": "检查整理工作是否完成",
        })
        reference = await manager.read_reference(created["task_id"])
        assert updated["target"] == "both"
        assert updated["text"] == "检查整理工作是否完成"
        assert reference["reference"] == '[{"role":"user","text":"冻结的原始上下文"}]'
        cancelled = await manager.cancel(created["task_id"])
        assert cancelled["status"] == "cancelled"
        await manager.close()

    asyncio.run(scenario())


def test_both_wake_delivery_retries_only_unaccepted_target_and_archives(tmp_path: Path):
    async def scenario() -> None:
        voice_calls = 0
        shadow_calls = 0

        async def deliver_voice(_task):
            nonlocal voice_calls
            voice_calls += 1
            return voice_calls > 1

        async def deliver_shadow(_task):
            nonlocal shadow_calls
            shadow_calls += 1
            return True

        manager = ScheduledWakeManager(
            tmp_path / "wake-tasks.json", deliver_voice=deliver_voice, deliver_shadow=deliver_shadow,
        )
        created = await manager.create({
            "target": "both", "when": "30m", "text": "检查待办",
        })
        manager._rows[created["task_id"]]["scheduled_at"] -= 3600
        manager._write()

        await manager._deliver_one(created["task_id"])
        row = manager._rows[created["task_id"]]
        assert row["status"] == "pending"
        assert row["delivered_targets"] == ["shadow_agent"]

        await manager._deliver_one(created["task_id"])
        row = manager._rows[created["task_id"]]
        assert row["status"] == "archived"
        assert voice_calls == 2
        assert shadow_calls == 1
        await manager.close()

    asyncio.run(scenario())


def test_both_wake_targets_are_started_asynchronously(tmp_path: Path):
    async def scenario() -> None:
        started: set[str] = set()
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def deliver(task):
            started.add(task["target"])
            if len(started) == 2:
                both_started.set()
            await release.wait()
            return True

        manager = ScheduledWakeManager(
            tmp_path / "wake-tasks.json", deliver_voice=deliver, deliver_shadow=deliver,
        )
        created = await manager.create({"target": "both", "when": "30m", "text": "同步提醒"})
        manager._rows[created["task_id"]]["scheduled_at"] -= 3600
        delivery = asyncio.create_task(manager._deliver_one(created["task_id"]))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release.set()
        await delivery
        assert started == {"voice_llm", "shadow_agent"}
        assert manager._rows[created["task_id"]]["status"] == "archived"
        await manager.close()

    asyncio.run(scenario())


def test_restart_skips_missed_repeating_occurrences(tmp_path: Path):
    async def no_delivery(_task):
        return False

    async def scenario() -> None:
        path = tmp_path / "wake-tasks.json"
        manager = ScheduledWakeManager(path, deliver_voice=no_delivery, deliver_shadow=no_delivery)
        created = await manager.create({
            "target": "voice_llm", "when": "30d", "repeat": "1h", "text": "循环检查",
        })
        manager._rows[created["task_id"]]["scheduled_at"] = datetime.now().astimezone().timestamp() - 5 * 3600
        manager._write()
        await manager.close()

        restored = ScheduledWakeManager(path, deliver_voice=no_delivery, deliver_shadow=no_delivery)
        await restored.start()
        row = restored._rows[created["task_id"]]
        now = datetime.now().astimezone().timestamp()
        assert row["scheduled_at"] > now
        assert row["scheduled_at"] <= now + 3600
        assert row["occurrence"] == 1
        await restored.close()

    asyncio.run(scenario())


def test_one_shot_is_archived_after_destination_accepts_wake(tmp_path: Path):
    async def scenario() -> None:
        accepted = asyncio.Event()
        delivered = []

        async def deliver_voice(task):
            delivered.append(task)
            accepted.set()
            return True

        async def no_delivery(_task):
            return False

        manager = ScheduledWakeManager(
            tmp_path / "wake-tasks.json", deliver_voice=deliver_voice, deliver_shadow=no_delivery,
        )
        created = await manager.create({
            "target": "voice_llm",
            "when": (datetime.now().astimezone() - timedelta(seconds=1)).isoformat(),
            "text": "提醒用户继续检查",
        })
        await manager.start()
        await asyncio.wait_for(accepted.wait(), timeout=1)
        for _ in range(10):
            if manager._rows[created["task_id"]]["status"] == "archived":
                break
            await asyncio.sleep(0)
        assert manager._rows[created["task_id"]]["status"] == "archived"
        assert delivered[0]["task_id"] == created["task_id"]
        assert delivered[0]["target"] == "voice_llm"
        await manager.close()

    asyncio.run(scenario())

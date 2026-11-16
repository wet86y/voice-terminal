"""远端播放事务只使用固定设备与可查询的 JSON（结构化数据）回执。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_terminal.devices.contracts import PlaybackOutcomeUnknown
from voice_terminal.devices.playback import RemotePlaybackTransactions
from voice_terminal.devices.remote_session import RemoteDeviceSessionHub
from voice_terminal.devices.router import DeviceOutcomeUnknown, DeviceRouter
from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.devices.task_journal import DeviceTaskJournal


def snapshot_payload(*, key="playback-1", device_time_us=1_200_000):
    return {
        "playback_key": key,
        "enqueued_ms": 500,
        "played_ms": 300,
        "buffered_ms": 200,
        "output_estimated_ms": 250,
        "started": True,
        "device_time_us": device_time_us,
        "precision": "device_time",
    }


class Router:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(status="succeeded", result=result)


def test_remote_playback_transactions_keep_one_device_and_map_ticket_receipts():
    async def scenario():
        router = Router(
            {"replacement_id": "replace-1", "old_playback_key": "playback-1",
             "prepared_device_time_us": 1_000_000, "snapshot": snapshot_payload()},
            {"bound": True},
            {"replacement_id": "replace-1", "reason": "early_handoff",
             "started_device_time_us": 1_000_000, "finished_device_time_us": 1_200_000,
             "events_device_time_us": {"old_audio_silenced": 1_200_000},
             "attenuated_range_ms": [50, 250], "snapshot": snapshot_payload(device_time_us=1_200_000)},
            snapshot_payload(key=None),
        )
        remote = RemotePlaybackTransactions(router, "device-B")
        ticket = await remote.prepare_replacement_transaction("playback-1", max_ms=400)
        assert ticket.replacement_id == "replace-1"
        assert ticket.snapshot.device_time_us == 1_200_000
        assert await remote.bind_replacement_transaction(ticket, "playback-2")
        result = await remote.wait_replacement_transaction(ticket)
        assert result.duration_ms == 200
        assert result.events_device_time_us == {"old_audio_silenced": 1_200_000}
        stopped = await remote.stop_and_snapshot_confirmed(None)
        assert stopped.key is None
        assert {call["device_id"] for call in router.calls} == {"device-B"}
        assert [call["operation"] for call in router.calls] == [
            "playback.prepare_replacement", "playback.bind_replacement",
            "playback.wait_replacement", "playback.stop_and_snapshot",
        ]
        assert router.calls[0]["arguments"] == {"playback_key": "playback-1", "max_ms": 400}
        assert router.calls[2]["arguments"] == {"replacement_id": "replace-1"}

    asyncio.run(scenario())


def test_remote_pause_resume_require_matching_confirmed_state_and_keep_fixed_device():
    async def scenario():
        router = Router(
            {"playback_key": "speech-a", "paused": True},
            {"playback_key": "speech-a", "paused": False},
        )
        remote = RemotePlaybackTransactions(router, "device-pause")
        assert await remote.pause_confirmed("speech-a")
        assert await remote.resume_confirmed("speech-a")
        assert [call["operation"] for call in router.calls] == ["playback.pause", "playback.resume"]
        assert all(call["device_id"] == "device-pause" for call in router.calls)
        assert router.calls[0]["arguments"] == {"playback_key": "speech-a"}

        mismatched = Router({"playback_key": "speech-other", "paused": True})
        with pytest.raises(PlaybackOutcomeUnknown):
            await RemotePlaybackTransactions(mismatched, "device-pause").pause_confirmed("speech-a")
        assert len(mismatched.calls) == 1

        unknown = Router(DeviceOutcomeUnknown("worker did not acknowledge pause"))
        with pytest.raises(PlaybackOutcomeUnknown) as caught:
            await RemotePlaybackTransactions(unknown, "device-pause").pause_confirmed("speech-a")
        assert caught.value.operation == "pause"
        assert caught.value.request_id == unknown.calls[0]["request_id"]
        assert len(unknown.calls) == 1

    asyncio.run(scenario())


def test_unknown_remote_playback_result_exposes_original_request_and_never_retries():
    async def scenario():
        router = Router(DeviceOutcomeUnknown("transport timed out"))
        remote = RemotePlaybackTransactions(router, "device-C")
        with pytest.raises(PlaybackOutcomeUnknown) as caught:
            await remote.prepare_replacement_transaction("old", max_ms=600)
        assert caught.value.request_id == router.calls[0]["request_id"]
        assert caught.value.device_id == "device-C"
        assert caught.value.operation == "prepare_replacement"
        assert len(router.calls) == 1

    asyncio.run(scenario())


def test_invalid_remote_receipt_stays_unknown_and_binds_to_original_request():
    async def scenario():
        router = Router({"replacement_id": "replace-1", "old_playback_key": "old",
                         "prepared_device_time_us": -1, "snapshot": snapshot_payload()})
        remote = RemotePlaybackTransactions(router, "device-D")
        with pytest.raises(PlaybackOutcomeUnknown) as caught:
            await remote.prepare_replacement_transaction("old")
        assert caught.value.request_id == router.calls[0]["request_id"]
        assert len(router.calls) == 1

    asyncio.run(scenario())


def test_original_unknown_request_can_be_queried_without_reissuing_control():
    async def scenario():
        calls = []
        record = SimpleNamespace(status="unknown", request_id="request-1")

        class QueryOnlyRouter:
            async def query(self, **kwargs):
                calls.append(kwargs)
                return record

        remote = RemotePlaybackTransactions(QueryOnlyRouter(), "device-E")
        assert await remote.query_request("request-1") is record
        assert calls == [{"request_id": "request-1", "device_id": "device-E"}]

    asyncio.run(scenario())


def test_playback_original_task_query_recovers_a_late_receipt_after_timeout(tmp_path):
    async def scenario():
        registry = DeviceRegistry()
        registry.connect("local")
        journal = DeviceTaskJournal(tmp_path / "playback-late-receipt.sqlite3")
        router = DeviceRouter(registry, journal, run_id="playback-core")
        hub = RemoteDeviceSessionHub(router)
        connection = await hub.open("device-late", ["audio_stream"])
        remote = RemotePlaybackTransactions(router, "device-late", timeout_seconds=0.02)
        execution = asyncio.create_task(remote.prepare_replacement_transaction("speech-old", max_ms=300))
        task = await hub.poll("device-late", connection.connection_generation,
                              connection.executor_generation, wait_ms=500)
        assert task is not None and task.operation == "playback.prepare_replacement"
        with pytest.raises(PlaybackOutcomeUnknown) as caught:
            await execution
        assert caught.value.request_id == task.request_id
        assert (await remote.query_request(task.request_id)).status == "unknown"

        ticket_payload = {
            "replacement_id": task.task_id,
            "old_playback_key": "speech-old",
            "prepared_device_time_us": 9_000_000,
            "snapshot": snapshot_payload(key="speech-old", device_time_us=9_000_000),
        }
        assert await hub.complete(
            device_id=task.device_id, connection_generation=task.connection_generation,
            executor_generation=task.executor_generation, task_id=task.task_id,
            sequence=task.sequence, core_session_id=task.core_session_id,
            succeeded=True, result=ticket_payload,
        ) == "accepted"
        await hub.close(task.device_id, connection.connection_generation)

        recovered = await remote.query_request(task.request_id)
        assert recovered.status == "succeeded" and recovered.result == ticket_payload
        # Querying the original ID reads the receipt cache and ledger; it doesn't enqueue a second task.
        assert await remote.query_request(task.request_id) == recovered

    asyncio.run(scenario())


def test_remote_stop_snapshot_uses_the_real_task_hub_and_preserves_device_target(tmp_path):
    async def scenario():
        registry = DeviceRegistry()
        registry.connect("local")
        journal = DeviceTaskJournal(tmp_path / "playback-tasks.sqlite3")
        router = DeviceRouter(registry, journal, run_id="playback-core")
        hub = RemoteDeviceSessionHub(router)
        connection = await hub.open("device-a", ["audio_stream"])
        remote = RemotePlaybackTransactions(router, "device-a")

        execution = asyncio.create_task(remote.stop_and_snapshot_confirmed("speech-key"))
        task = await hub.poll("device-a", connection.connection_generation,
                              connection.executor_generation, wait_ms=500)
        assert task is not None
        assert task.operation == "playback.stop_and_snapshot"
        assert task.arguments == {"playback_key": "speech-key"}
        assert task.device_id == "device-a"
        assert task.core_session_id == "playback-core"

        receipt = await hub.complete(
            device_id=task.device_id,
            connection_generation=task.connection_generation,
            executor_generation=task.executor_generation,
            task_id=task.task_id,
            sequence=task.sequence,
            core_session_id=task.core_session_id,
            succeeded=True,
            result=snapshot_payload(key="speech-key", device_time_us=9_000_000),
        )
        assert receipt == "accepted"
        stopped = await execution
        assert stopped.key == "speech-key"
        assert stopped.device_time_us == 9_000_000
        record = journal.get(task.request_id)
        assert record is not None and record.status == "succeeded"
        assert record.device_id == "device-a"
        assert await hub.close("device-a", connection.connection_generation)

    asyncio.run(scenario())


def test_remote_pause_task_uses_the_real_task_hub_and_returns_device_state(tmp_path):
    async def scenario():
        registry = DeviceRegistry()
        registry.connect("local")
        journal = DeviceTaskJournal(tmp_path / "pause-tasks.sqlite3")
        router = DeviceRouter(registry, journal, run_id="pause-core")
        hub = RemoteDeviceSessionHub(router)
        connection = await hub.open("device-a", ["audio_stream"])
        remote = RemotePlaybackTransactions(router, "device-a")

        execution = asyncio.create_task(remote.pause_confirmed("speech-key"))
        task = await hub.poll("device-a", connection.connection_generation,
                              connection.executor_generation, wait_ms=500)
        assert task is not None
        assert task.operation == "playback.pause"
        assert task.arguments == {"playback_key": "speech-key"}
        assert task.device_id == "device-a"
        assert task.core_session_id == "pause-core"

        assert await hub.complete(
            device_id=task.device_id,
            connection_generation=task.connection_generation,
            executor_generation=task.executor_generation,
            task_id=task.task_id,
            sequence=task.sequence,
            core_session_id=task.core_session_id,
            succeeded=True,
            result={"playback_key": "speech-key", "paused": True},
        ) == "accepted"
        assert await execution is True
        record = journal.get(task.request_id)
        assert record is not None and record.status == "succeeded"
        assert record.device_id == "device-a"
        assert await hub.close("device-a", connection.connection_generation)

    asyncio.run(scenario())


def test_remote_replacement_prepare_bind_and_wait_round_trip_through_the_task_hub(tmp_path):
    async def scenario():
        registry = DeviceRegistry()
        registry.connect("local")
        journal = DeviceTaskJournal(tmp_path / "replacement-tasks.sqlite3")
        router = DeviceRouter(registry, journal, run_id="replacement-core")
        hub = RemoteDeviceSessionHub(router)
        connection = await hub.open("device-replace", ["audio_stream"])
        remote = RemotePlaybackTransactions(router, "device-replace")

        prepare = asyncio.create_task(remote.prepare_replacement_transaction("speech-old", max_ms=300))
        prepare_task = await hub.poll("device-replace", connection.connection_generation,
                                      connection.executor_generation, wait_ms=500)
        assert prepare_task is not None
        assert prepare_task.operation == "playback.prepare_replacement"
        assert prepare_task.arguments == {"playback_key":"speech-old","max_ms":300}
        ticket_payload = {
            "replacement_id":prepare_task.task_id, "old_playback_key":"speech-old",
            "prepared_device_time_us":9_000_000,
            "snapshot":snapshot_payload(key="speech-old", device_time_us=9_000_000),
        }
        assert await hub.complete(
            device_id=prepare_task.device_id, connection_generation=prepare_task.connection_generation,
            executor_generation=prepare_task.executor_generation, task_id=prepare_task.task_id,
            sequence=prepare_task.sequence, core_session_id=prepare_task.core_session_id,
            succeeded=True, result=ticket_payload,
        ) == "accepted"
        ticket = await prepare
        assert ticket.replacement_id == prepare_task.task_id

        binding = asyncio.create_task(remote.bind_replacement_transaction(ticket, "speech-new"))
        bind_task = await hub.poll("device-replace", connection.connection_generation,
                                   connection.executor_generation, wait_ms=500)
        assert bind_task is not None and bind_task.operation == "playback.bind_replacement"
        assert bind_task.arguments == {"replacement_id":ticket.replacement_id,"playback_key":"speech-new"}
        assert await hub.complete(
            device_id=bind_task.device_id, connection_generation=bind_task.connection_generation,
            executor_generation=bind_task.executor_generation, task_id=bind_task.task_id,
            sequence=bind_task.sequence, core_session_id=bind_task.core_session_id,
            succeeded=True, result={"bound":True},
        ) == "accepted"
        assert await binding is True

        waiting = asyncio.create_task(remote.wait_replacement_transaction(ticket))
        wait_task = await hub.poll("device-replace", connection.connection_generation,
                                  connection.executor_generation, wait_ms=500)
        assert wait_task is not None and wait_task.operation == "playback.wait_replacement"
        assert wait_task.arguments == {"replacement_id":ticket.replacement_id}
        final_payload = {
            "replacement_id":ticket.replacement_id, "reason":"early_handoff",
            "started_device_time_us":9_000_000, "finished_device_time_us":9_240_000,
            "events_device_time_us":{"cancel_accepted":9_000_000,"old_audio_silenced":9_240_000},
            "attenuated_range_ms":[120,300],
            "snapshot":snapshot_payload(key="speech-old", device_time_us=9_240_000),
        }
        assert await hub.complete(
            device_id=wait_task.device_id, connection_generation=wait_task.connection_generation,
            executor_generation=wait_task.executor_generation, task_id=wait_task.task_id,
            sequence=wait_task.sequence, core_session_id=wait_task.core_session_id,
            succeeded=True, result=final_payload,
        ) == "accepted"
        result = await waiting
        assert result.replacement_id == ticket.replacement_id
        assert result.reason == "early_handoff"
        assert result.events_device_time_us["old_audio_silenced"] == 9_240_000
        assert [journal.get(item.task_id).status for item in (prepare_task,bind_task,wait_task)] == [
            "succeeded","succeeded","succeeded",
        ]
        assert await hub.close("device-replace", connection.connection_generation)

    asyncio.run(scenario())


def test_python_stop_task_response_matches_the_rust_client_shared_fixture(tmp_path):
    async def scenario():
        registry = DeviceRegistry()
        registry.connect("local")
        journal = DeviceTaskJournal(tmp_path / "playback-fixture.sqlite3")
        router = DeviceRouter(registry, journal, run_id="playback-core")
        hub = RemoteDeviceSessionHub(router)
        connection = await hub.open("device-a", ["audio_stream"])
        execution = asyncio.create_task(router.execute(
            request_id="request-1", device_id="device-a",
            operation="playback.stop_and_snapshot",
            arguments={"playback_key": "speech-key"}, timeout_seconds=3,
        ))
        task = await hub.poll("device-a", connection.connection_generation,
                              connection.executor_generation, wait_ms=500)
        assert task is not None
        response = {
            "protocol_version": 1, "result": "accepted", "device_id": task.device_id,
            "core_session_id": task.core_session_id,
            "connection_generation": task.connection_generation,
            "executor_generation": task.executor_generation,
            "task": task.payload(),
            "fields": {
                "protocol_version": "设备任务通道协议版本", "result": "轮询结果",
                "device_id": "固定轮询设备编号", "core_session_id": "当前权威核心运行编号",
                "connection_generation": "当前设备连接代次", "executor_generation": "当前设备执行器代次",
                "task": "原目标任务；为空时本轮没有待执行任务",
            },
        }
        fixture = json.loads((Path(__file__).parent / "fixtures" / "device-playback-stop-task.json")
                             .read_text(encoding="utf-8"))
        assert response == fixture
        await hub.complete(
            device_id=task.device_id,
            connection_generation=task.connection_generation,
            executor_generation=task.executor_generation,
            task_id=task.task_id,
            sequence=task.sequence,
            core_session_id=task.core_session_id,
            succeeded=True,
            result=snapshot_payload(key="speech-key"),
        )
        assert (await execution).status == "succeeded"
        await hub.close("device-a", connection.connection_generation)

    asyncio.run(scenario())

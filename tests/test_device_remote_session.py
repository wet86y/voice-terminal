from __future__ import annotations

import asyncio

import pytest

from voice_terminal.devices.remote_session import RemoteDeviceSessionHub
from voice_terminal.devices.router import DeviceOutcomeUnknown, DeviceRouter, DeviceUnavailable
from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.devices.task_journal import DeviceTaskJournal


def _runtime(tmp_path):
    registry = DeviceRegistry()
    registry.connect("local")
    journal = DeviceTaskJournal(tmp_path / "device-tasks.sqlite3")
    router = DeviceRouter(registry, journal, run_id="core-session-remote-test")
    return registry, journal, router, RemoteDeviceSessionHub(router)


def test_remote_session_accepts_client_tts_capability_without_granting_voice_lease(tmp_path) -> None:
    async def scenario() -> None:
        registry, _, _, hub = _runtime(tmp_path)

        connection = await hub.open("client-tts", ["audio_stream", "client_tts"])

        assert connection.capabilities == ("audio_stream", "client_tts")
        assert registry.active is None

    asyncio.run(scenario())


def test_playback_control_can_settle_while_the_screen_task_is_in_flight(tmp_path) -> None:
    async def scenario():
        _, _, router, hub = _runtime(tmp_path)
        connection = await hub.open("client-a", ["audio_stream", "file_transfer", "screen_capture"])
        device = (connection.device_id, connection.connection_generation, connection.executor_generation)
        screenshot = asyncio.create_task(router.execute(
            request_id="shot-1", device_id=device[0], operation="screen.capture_file", arguments={},
        ))
        stop = None
        try:
            shot_task = await hub.poll(*device, lane="general")
            assert shot_task is not None and shot_task.operation == "screen.capture_file"
            stop = asyncio.create_task(router.execute(
                request_id="stop-1", device_id=device[0], operation="playback.stop_and_snapshot", arguments={},
            ))
            playback = await hub.poll(*device, lane="playback")
            assert playback is not None and playback.operation == "playback.stop_and_snapshot"
            assert await hub.poll(*device, wait_ms=0, lane="general") is None
            assert await hub.complete(device_id=device[0], connection_generation=device[1],
                executor_generation=device[2], task_id=playback.task_id, sequence=playback.sequence,
                core_session_id=playback.core_session_id, succeeded=True, result={}) == "accepted"
            assert (await stop).status == "succeeded"
            assert not screenshot.done()
        finally:
            await hub.close(device[0], device[1])
            await asyncio.gather(*(task for task in (screenshot, stop) if task is not None),
                                 return_exceptions=True)

    asyncio.run(scenario())




def test_remote_session_dispatches_fixed_task_and_deduplicates_completion(tmp_path) -> None:
    async def scenario() -> None:
        registry, journal, router, hub = _runtime(tmp_path)
        connection = await hub.open("client-a", ["desktop_control"])
        assert connection.connected
        assert connection.capabilities == ("desktop_control",)
        assert registry.active is None

        execution = asyncio.create_task(router.execute(
            request_id="windows-remote-1", device_id="client-a", operation="windows",
            arguments={}, timeout_seconds=3,
        ))
        task = await hub.poll("client-a", connection.connection_generation,
                              connection.executor_generation, wait_ms=500)
        assert task is not None
        assert task.device_id == "client-a"
        assert task.core_session_id == router.run_id
        assert task.task_id == task.request_id == "windows-remote-1"
        assert task.connection_generation == connection.connection_generation
        assert task.executor_generation == connection.executor_generation
        assert task.sequence == 1

        with pytest.raises(DeviceUnavailable):
            await hub.complete(device_id="client-a", connection_generation=connection.connection_generation,
                executor_generation=connection.executor_generation, task_id=task.task_id,
                sequence=task.sequence + 1, core_session_id=task.core_session_id,
                succeeded=True, result=[])

        result = [{"windowId": 17, "processId": 29}]
        receipt = await hub.complete(device_id="client-a", connection_generation=connection.connection_generation,
            executor_generation=connection.executor_generation, task_id=task.task_id,
            sequence=task.sequence, core_session_id=task.core_session_id, succeeded=True, result=result)
        assert receipt == "accepted"
        record = await execution
        assert record.status == "succeeded" and record.result == result
        assert journal.get("windows-remote-1") == record

        duplicate = await hub.complete(device_id="client-a", connection_generation=connection.connection_generation,
            executor_generation=connection.executor_generation, task_id=task.task_id,
            sequence=task.sequence, core_session_id=task.core_session_id, succeeded=True, result=result)
        assert duplicate == "already_completed"
        assert await hub.poll("client-a", connection.connection_generation,
                              connection.executor_generation, wait_ms=0) is None
        assert await router.execute(request_id="windows-remote-1", device_id="client-a",
            operation="windows", arguments={}, timeout_seconds=1) == record

    asyncio.run(scenario())


def test_late_receipt_resolves_the_original_unknown_request_without_redispatch(tmp_path) -> None:
    async def scenario() -> None:
        registry, journal, router, hub = _runtime(tmp_path)
        connection = await hub.open("client-late", ["audio_stream"])
        execution = asyncio.create_task(router.execute(
            request_id="playback-prepare-late", device_id="client-late",
            operation="playback.prepare_replacement",
            arguments={"playback_key":"speech-old","max_ms":300}, timeout_seconds=0.02,
        ))
        task = await hub.poll("client-late", connection.connection_generation,
                              connection.executor_generation, wait_ms=500)
        assert task is not None and task.task_id == "playback-prepare-late"
        with pytest.raises(DeviceOutcomeUnknown):
            await execution
        assert journal.get(task.task_id).status == "unknown"
        assert (await router.query(request_id=task.task_id, device_id=task.device_id)).status == "unknown"

        result = {"replacement_id":task.task_id,"prepared_device_time_us":9_000_000}
        receipt = await hub.complete(
            device_id=task.device_id, connection_generation=task.connection_generation,
            executor_generation=task.executor_generation, task_id=task.task_id,
            sequence=task.sequence, core_session_id=task.core_session_id,
            succeeded=True, result=result,
        )
        assert receipt == "accepted"
        assert journal.get(task.task_id).status == "succeeded"
        assert await hub.close("client-late", connection.connection_generation)
        recovered = await router.query(request_id=task.task_id, device_id=task.device_id)
        assert recovered.status == "succeeded" and recovered.result == result
        # Repeating the original request reads its settled row and does not enqueue again.
        assert await router.execute(
            request_id=task.task_id, device_id=task.device_id,
            operation="playback.prepare_replacement",
            arguments={"playback_key":"speech-old","max_ms":300},
        ) == recovered

    asyncio.run(scenario())


def test_reconnect_invalidates_old_executor_and_keeps_unknown_task_on_original_target(tmp_path) -> None:
    async def scenario() -> None:
        registry, journal, router, hub = _runtime(tmp_path)
        old = await hub.open("client-b", ["desktop_control"])
        execution = asyncio.create_task(router.execute(
            request_id="action-remote-1", device_id="client-b", operation="click",
            arguments={"x": 5, "y": 9}, timeout_seconds=3,
        ))
        old_task = await hub.poll("client-b", old.connection_generation, old.executor_generation, wait_ms=500)
        assert old_task is not None

        current = await hub.open("client-b", ["desktop_control"])
        assert current.connection_generation == old.connection_generation + 1
        assert current.executor_generation == old.executor_generation + 1
        with pytest.raises(DeviceOutcomeUnknown):
            await execution
        record = journal.get("action-remote-1")
        assert record is not None and record.status == "unknown" and record.device_id == "client-b"
        assert registry.active is None
        assert (await router.query(request_id="action-remote-1", device_id="client-b")).status == "unknown"

        with pytest.raises(DeviceUnavailable):
            await hub.complete(device_id="client-b", connection_generation=old.connection_generation,
                executor_generation=old.executor_generation, task_id=old_task.task_id,
                sequence=old_task.sequence, core_session_id=old_task.core_session_id,
                succeeded=True, result={"ok": True})
        assert await hub.poll("client-b", current.connection_generation,
                              current.executor_generation, wait_ms=0) is None
        await hub.close("client-b", current.connection_generation)
        assert registry.connection("client-b").connected is False

    asyncio.run(scenario())


def test_remote_session_requires_supported_capability_and_disconnect_does_not_migrate(tmp_path) -> None:
    async def scenario() -> None:
        registry, journal, router, hub = _runtime(tmp_path)
        with pytest.raises(ValueError):
            await hub.open("client-c", [])
        with pytest.raises(ValueError):
            await hub.open("client-c", ["terminal"])
        connection = await hub.open("client-c", ["desktop_control"])
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="offline-device-1", device_id="client-c",
                operation="windows", arguments={}, timeout_seconds=1)
        record = journal.get("offline-device-1")
        assert record is not None and record.status == "unknown" and record.device_id == "client-c"
        assert registry.connection("local").connected is True
        assert await hub.poll("client-c", connection.connection_generation,
                              connection.executor_generation, wait_ms=0) is None
        assert await hub.close("client-c", connection.connection_generation)

    asyncio.run(scenario())


def test_remote_session_can_register_authenticated_audio_stream_capability(tmp_path) -> None:
    async def scenario() -> None:
        registry, _journal, _router, hub = _runtime(tmp_path)
        connection = await hub.open("client-audio", ["audio_stream"])
        assert connection.capabilities == ("audio_stream",)
        assert connection.connection_generation == 1
        assert registry.active is None

    asyncio.run(scenario())

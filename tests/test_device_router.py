"""设备路由：重复、断线、取消和迟到回执都不能重做副作用。"""
import asyncio
import threading
from dataclasses import replace

import pytest

from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.devices.router import DeviceOutcomeUnknown, DeviceResponse, DeviceRouter
from voice_terminal.devices.task_journal import DeviceTaskJournal, TaskConflict


def _response(request, **changes):
    response = DeviceResponse(request.request_id, request.device_id, request.core_session_id,
                              request.task_id, request.connection_generation,
                              request.executor_generation, True, {"ok": True})
    return replace(response, **changes)


def test_repeated_requests_return_saved_result_without_second_action(tmp_path):
    async def scenario():
        router = DeviceRouter(DeviceRegistry(), DeviceTaskJournal(tmp_path / "tasks.sqlite"))

        class Transport:
            calls = 0

            async def execute(self, request):
                self.calls += 1
                return _response(request)

        transport = Transport()
        await router.connect("B", transport)
        first = await router.execute(request_id="r", device_id="B", operation="write", arguments={"x": 1})
        second = await router.execute(request_id="r", device_id="B", operation="write", arguments={"x": 1})
        assert first.status == second.status == "succeeded" and second.result == {"ok": True}
        assert transport.calls == 1
        with pytest.raises(TaskConflict):
            await router.execute(request_id="r", device_id="B", operation="write", arguments={"x": 2})
        generation = router.registry.snapshot()[0].connection_generation
        assert await router.disconnect("B", generation)
        assert (await router.execute(request_id="r", device_id="B", operation="write", arguments={"x": 1})).status == "succeeded"
        with pytest.raises(TaskConflict):
            await router.execute(request_id="r", device_id="B", operation="write", arguments={"x": 2})

    asyncio.run(scenario())


def test_read_only_task_query_never_reissues_and_rejects_other_device(tmp_path):
    async def scenario():
        router = DeviceRouter(DeviceRegistry(), DeviceTaskJournal(tmp_path / "query.sqlite"))

        class Transport:
            calls = 0

            async def execute(self, request):
                self.calls += 1
                return _response(request)

        transport = Transport()
        await router.connect("B", transport)
        original = await router.execute(request_id="r", device_id="B", operation="write", arguments={})
        queried = await router.query(request_id="r", device_id="B")
        assert queried == original
        assert transport.calls == 1
        with pytest.raises(TaskConflict):
            await router.query(request_id="r", device_id="C")

    asyncio.run(scenario())


def test_connection_replacement_rejects_old_receipt_and_preserves_unknown(tmp_path):
    async def scenario():
        router = DeviceRouter(DeviceRegistry(), DeviceTaskJournal(tmp_path / "tasks.sqlite"))

        class Transport:
            async def execute(self, request):
                await router.connect("B", self)
                return _response(request)

        transport = Transport()
        original = await router.connect("B", transport)
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="r", device_id="B", operation="write", arguments={})
        assert router.journal.get("r").status == "unknown"
        assert not await router.disconnect("B", original.connection_generation)
        duplicate = await router.execute(request_id="r", device_id="B", operation="write", arguments={})
        assert duplicate.status == "unknown"

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("core_session_id", "old-core"), ("task_id", "other-task")])
def test_response_must_match_core_session_and_persistent_task(tmp_path, field, value):
    async def scenario():
        router = DeviceRouter(DeviceRegistry(), DeviceTaskJournal(tmp_path / f"tasks-{field}.sqlite"),
                              run_id="core-current")

        class Transport:
            async def execute(self, request):
                return _response(request, **{field: value})

        await router.connect("B", Transport())
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="request-1", device_id="B", operation="read", arguments={})
        assert router.journal.get("request-1").status == "unknown"

    asyncio.run(scenario())


def test_timeout_and_cancellation_do_not_readmit_requests(tmp_path):
    async def scenario():
        router = DeviceRouter(DeviceRegistry(), DeviceTaskJournal(tmp_path / "tasks.sqlite"))
        entered = asyncio.Event()

        class Transport:
            calls = 0

            async def execute(self, request):
                self.calls += 1
                entered.set()
                await asyncio.Event().wait()

        transport = Transport()
        await router.connect("B", transport)
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="r", device_id="B", operation="delete", arguments={}, timeout_seconds=.01)
        assert (await router.execute(request_id="r", device_id="B", operation="delete", arguments={})).status == "unknown"
        task = asyncio.create_task(router.execute(request_id="r2", device_id="B", operation="delete", arguments={}))
        while transport.calls < 2:
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert router.journal.get("r2").status == "unknown"
        assert transport.calls == 2

    asyncio.run(scenario())


def test_mismatched_receipt_cannot_settle_task(tmp_path):
    async def scenario():
        router = DeviceRouter(DeviceRegistry(), DeviceTaskJournal(tmp_path / "tasks.sqlite"))

        class Transport:
            async def execute(self, request):
                return _response(request, device_id="C")

        await router.connect("B", Transport())
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="r", device_id="B", operation="write", arguments={})
        assert router.journal.get("r").status == "unknown"

    asyncio.run(scenario())


def test_executor_restart_invalidates_in_flight_receipt(tmp_path):
    async def scenario():
        registry = DeviceRegistry()
        router = DeviceRouter(registry, DeviceTaskJournal(tmp_path / "tasks.sqlite"))

        class Transport:
            async def execute(self, request):
                registry.restart_executor(request.device_id, request.connection_generation)
                return _response(request)

        await router.connect("B", Transport())
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="r", device_id="B", operation="write", arguments={})
        assert router.journal.get("r").status == "unknown"
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="new", device_id="B", operation="write", arguments={})
        assert router.journal.get("new").status == "unknown"

    asyncio.run(scenario())


def test_router_restarts_executor_without_replaying_old_task(tmp_path):
    async def scenario():
        registry = DeviceRegistry()
        router = DeviceRouter(registry, DeviceTaskJournal(tmp_path / "tasks.sqlite"))

        class Replacement:
            async def execute(self, request):
                return _response(request, result={"generation": request.executor_generation})

        class Original:
            async def execute(self, request):
                await router.restart_executor("B", request.connection_generation, Replacement())
                return _response(request)

        original = await router.connect("B", Original())
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="old", device_id="B", operation="write", arguments={})
        assert (await router.execute(request_id="old", device_id="B", operation="write", arguments={})).status == "unknown"
        new = await router.execute(request_id="new", device_id="B", operation="write", arguments={})
        assert new.result == {"generation": original.executor_generation + 1}

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["begin", "start"])
def test_cancellation_during_journal_write_preserves_unknown(tmp_path, phase):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        class Journal(DeviceTaskJournal):
            def begin(self, **kwargs):
                if phase == "begin":
                    entered.set()
                    assert release.wait(5)
                return super().begin(**kwargs)

            def start(self, request_id, run_id):
                if phase == "start":
                    entered.set()
                    assert release.wait(5)
                return super().start(request_id, run_id)

        class Transport:
            async def execute(self, request):
                raise AssertionError("取消后不能发送设备动作")

        router = DeviceRouter(DeviceRegistry(), Journal(tmp_path / "tasks.sqlite"))
        await router.connect("B", Transport())
        task = asyncio.create_task(router.execute(request_id="r", device_id="B", operation="write", arguments={}))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert router.journal.get("r").status == "unknown"
        assert (await router.execute(request_id="r", device_id="B", operation="write", arguments={})).status == "unknown"

    asyncio.run(scenario())


def test_start_journal_failure_does_not_dispatch_or_readmit(tmp_path):
    async def scenario():
        class Journal(DeviceTaskJournal):
            def start(self, request_id, run_id):
                super().start(request_id, run_id)
                raise OSError("模拟状态落盘后发生错误")

        class Transport:
            async def execute(self, request):
                raise AssertionError("台账写入失败不能派发设备动作")

        router = DeviceRouter(DeviceRegistry(), Journal(tmp_path / "tasks.sqlite"))
        await router.connect("B", Transport())
        with pytest.raises(DeviceOutcomeUnknown):
            await router.execute(request_id="r", device_id="B", operation="write", arguments={})
        assert router.journal.get("r").status == "unknown"

    asyncio.run(scenario())

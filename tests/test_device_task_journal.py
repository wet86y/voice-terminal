"""设备任务台账：回执丢失和并发请求不得造成副作用重放。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from voice_terminal.devices.task_journal import DeviceTaskJournal, TaskConflict


def test_duplicate_request_is_not_readmitted_after_restart(tmp_path):
    path = tmp_path / "device-tasks.sqlite"
    journal = DeviceTaskJournal(path)
    record, created = journal.begin(request_id="r1", device_id="B", operation="delete",
                                    arguments={"path": "demo.txt"}, run_id="run-1")
    assert created and record.status == "accepted"
    assert journal.start("r1", "run-1")
    assert not journal.start("r1", "run-1")
    assert journal.mark_unknown("r1", "run-1")
    restarted = DeviceTaskJournal(path)
    duplicate, created = restarted.begin(request_id="r1", device_id="B", operation="delete",
                                         arguments={"path": "demo.txt"}, run_id="run-2")
    assert not created and duplicate.status == "unknown" and duplicate.run_id == "run-1"
    assert not restarted.start("r1", "run-2")
    assert not restarted.resolve_unknown("r1", device_id="C", request_digest=record.request_digest,
                                         succeeded=True, result={"deleted": True})
    assert restarted.resolve_unknown("r1", device_id="B", request_digest=record.request_digest,
                                     succeeded=True, result={"deleted": True})
    assert restarted.get("r1").result == {"deleted": True}


def test_request_id_cannot_move_between_devices_or_change_arguments(tmp_path):
    journal = DeviceTaskJournal(tmp_path / "device-tasks.sqlite")
    journal.begin(request_id="r", device_id="B", operation="write", arguments={"x": 1}, run_id="a")
    for device, arguments in (("C", {"x": 1}), ("B", {"x": 2})):
        with pytest.raises(TaskConflict):
            journal.begin(request_id="r", device_id=device, operation="write", arguments=arguments, run_id="b")
    assert journal.get("r").status == "accepted"


def test_parallel_same_request_has_one_owner_and_one_result(tmp_path):
    journal = DeviceTaskJournal(tmp_path / "device-tasks.sqlite")

    def submit(run):
        return journal.begin(request_id="r", device_id="B", operation="write", arguments={"a": 1}, run_id=run)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, ("run-1", "run-2")))
    assert sum(created for _, created in outcomes) == 1
    winner = next(record for record, created in outcomes if created)
    assert journal.start("r", winner.run_id)
    assert journal.finish("r", winner.run_id, succeeded=True, result={"ok": True})
    assert not journal.mark_unknown("r", winner.run_id)
    assert not journal.finish("r", winner.run_id, succeeded=False, result={"ok": False})
    assert journal.get("r").status == "succeeded"


def test_invalid_values_never_create_or_settle_a_task(tmp_path):
    journal = DeviceTaskJournal(tmp_path / "device-tasks.sqlite")
    with pytest.raises(ValueError):
        journal.begin(request_id="r", device_id="B", operation="write", arguments={"x": float("nan")}, run_id="a")
    assert journal.get("r") is None
    journal.begin(request_id="r", device_id="B", operation="write", arguments={}, run_id="a")
    assert journal.start("r", "a")
    with pytest.raises(ValueError):
        journal.finish("r", "a", succeeded=True, result="x" * (256 * 1024 + 1))
    assert journal.get("r").status == "running"


def test_recover_stopped_owner_only_marks_that_owners_unfinished_tasks(tmp_path):
    journal = DeviceTaskJournal(tmp_path / "tasks.sqlite")
    for request_id, owner in (("accepted", "old"), ("running", "old"),
                              ("complete", "old"), ("other", "live")):
        journal.begin(request_id=request_id, device_id="B", operation="write", arguments={}, run_id=owner)
    assert journal.start("running", "old")
    assert journal.start("complete", "old")
    assert journal.finish("complete", "old", succeeded=True, result={})
    assert journal.recover_stopped_owner("old") == 2
    assert journal.get("accepted").status == journal.get("running").status == "unknown"
    assert journal.get("complete").status == "succeeded"
    assert journal.get("other").status == "accepted"
    assert journal.recover_stopped_owner("old") == 0


def test_remote_task_identity_survives_restart_and_rejects_generation_rebinding(tmp_path):
    path = tmp_path / "tasks.sqlite"
    journal = DeviceTaskJournal(path)
    journal.begin(request_id="remote-r1", device_id="B", operation="playback.stop",
                  arguments={"playback_key":"speech-a"}, run_id="core-a")
    assert journal.start("remote-r1", "core-a")
    identity = {
        "request_id":"remote-r1", "device_id":"B", "run_id":"core-a",
        "connection_generation":4, "executor_generation":2, "sequence":19,
    }
    assert journal.bind_remote_identity(**identity)
    assert journal.bind_remote_identity(**identity)
    restarted = DeviceTaskJournal(path)
    assert restarted.remote_identity("remote-r1") == ("B", "core-a", 4, 2, 19)
    with pytest.raises(TaskConflict):
        restarted.bind_remote_identity(**{**identity, "connection_generation":5})
    assert restarted.remote_identity("remote-r1") == ("B", "core-a", 4, 2, 19)

"""日志容量预算：总量上限、最旧优先剔除与追加式日志轮转。"""

import logging
import os
import time
from pathlib import Path

import pytest

from voice_terminal import log_budget
from voice_terminal.logging_setup import _AUDIT_FILE_LIMIT, _DEFAULT_FILE_LIMIT, _FILE_LIMITS

MIB = 1024 * 1024


def _write(path: Path, size: int, *, age_seconds: float = 3600.0) -> Path:
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_evicts_oldest_files_until_within_budget(tmp_path):
    _write(tmp_path / "old.log", 2 * MIB, age_seconds=7200)
    _write(tmp_path / "middle.log.1", 2 * MIB, age_seconds=3600)
    _write(tmp_path / "new.log", 2 * MIB, age_seconds=600)

    result = log_budget.enforce_log_budget(tmp_path, budget_bytes=4 * MIB,
                                           min_age_seconds=0, quiet=True)

    assert result["removed"] == ["old.log"]
    assert (tmp_path / "new.log").is_file()
    assert (tmp_path / "middle.log.1").is_file()
    assert result["over_budget"] is False
    assert result["total_after"] <= 4 * MIB


def test_under_budget_removes_nothing(tmp_path):
    _write(tmp_path / "a.log", 64 * 1024)

    result = log_budget.enforce_log_budget(tmp_path, budget_bytes=MIB,
                                           min_age_seconds=0, quiet=True)

    assert result["removed"] == []
    assert result["skipped"] == []
    assert result["over_budget"] is False
    assert (tmp_path / "a.log").is_file()


def test_disabled_budget_is_a_no_op(tmp_path):
    _write(tmp_path / "a.log", 4 * MIB)

    result = log_budget.enforce_log_budget(tmp_path, budget_bytes=0,
                                           min_age_seconds=0, quiet=True)

    assert result["disabled"] is True
    assert result["removed"] == []
    assert (tmp_path / "a.log").is_file()


def test_protected_file_is_never_removed(tmp_path, monkeypatch):
    # 受保护文件排在最旧位置，剔除循环必须先尝试它再继续。
    active = _write(tmp_path / "voice-server.log", 2 * MIB, age_seconds=10800)
    _write(tmp_path / "older.log", 2 * MIB, age_seconds=3600)

    result = log_budget.enforce_log_budget(tmp_path, budget_bytes=2 * MIB,
                                           protect=[active], min_age_seconds=0, quiet=True)

    assert active.is_file()
    assert "voice-server.log" in result["skipped"]
    assert (tmp_path / "older.log").exists() is False


def test_file_held_open_by_another_process_is_skipped(tmp_path, monkeypatch):
    blocked = _write(tmp_path / "blocked.log", 2 * MIB, age_seconds=7200)
    _write(tmp_path / "removable.log", 2 * MIB, age_seconds=3600)
    original = Path.unlink

    def guarded(self, *args, **kwargs):
        if self.name == blocked.name:
            raise PermissionError(32, "文件被其它进程占用")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded)
    result = log_budget.enforce_log_budget(tmp_path, budget_bytes=2 * MIB,
                                           min_age_seconds=0, quiet=True)

    assert blocked.is_file()
    assert "blocked.log" in result["skipped"]
    assert (tmp_path / "removable.log").exists() is False


def test_recent_files_are_kept_until_min_age(tmp_path):
    fresh = _write(tmp_path / "fresh.log", 2 * MIB, age_seconds=5)
    stale = _write(tmp_path / "stale.log", 2 * MIB, age_seconds=600)

    result = log_budget.enforce_log_budget(tmp_path, budget_bytes=MIB,
                                           min_age_seconds=60, quiet=True)

    assert fresh.is_file()
    assert "fresh.log" in result["skipped"]
    assert stale.is_file() is False
    # 只剩下的新文件仍超预算，但它是被 min_age 保护的，不算失败。
    assert result["over_budget"] is True


def test_non_log_files_are_untouched(tmp_path):
    _write(tmp_path / "voice-server.log", 2 * MIB, age_seconds=7200)
    handoff = tmp_path / "startup-handoff-error.json"
    handoff.write_text("{}", encoding="utf-8")
    notes = tmp_path / "notes.txt"
    notes.write_text("keep", encoding="utf-8")

    assert sorted(p.name for p in log_budget.iter_log_files(tmp_path)) == ["voice-server.log"]
    log_budget.enforce_log_budget(tmp_path, budget_bytes=1024, min_age_seconds=0, quiet=True)

    assert handoff.is_file()
    assert notes.is_file()


@pytest.mark.parametrize("value,expected_mb", [("", 100.0), ("0", 0.0), ("-5", 0.0),
                                               ("abc", 100.0), ("25", 25.0), ("1.5", 1.5)])
def test_budget_env_parsing(monkeypatch, value, expected_mb):
    monkeypatch.setenv("VOICE_LOG_BUDGET_MB", value)
    assert log_budget.log_budget_bytes() == int(expected_mb * MIB)


def test_watchdog_can_be_disabled(monkeypatch):
    monkeypatch.setenv("VOICE_LOG_BUDGET_WATCHDOG", "0")
    assert log_budget.log_budget_watchdog_enabled() is False
    monkeypatch.setenv("VOICE_LOG_BUDGET_WATCHDOG", "1")
    assert log_budget.log_budget_watchdog_enabled() is True


def test_rotate_oversized_log_keeps_one_backup(tmp_path):
    target = tmp_path / "audio-cpp-tts.log"
    _write(target, 5 * MIB)
    (tmp_path / "audio-cpp-tts.log.1").write_bytes(b"previous")

    assert log_budget.rotate_oversized_log(target, max_bytes=4 * MIB, backups=1) is True
    assert target.exists() is False
    assert (tmp_path / "audio-cpp-tts.log.1").read_bytes() == b"x" * (5 * MIB)


def test_rotate_oversized_log_leaves_small_file_alone(tmp_path):
    target = _write(tmp_path / "audio-cpp-tts.log", 1024)

    assert log_budget.rotate_oversized_log(target, max_bytes=4 * MIB, backups=1) is False
    assert target.is_file()


def test_budgeted_handler_sweeps_after_rollover(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_LOG_BUDGET_MB", "3")
    handler = log_budget.BudgetedRotatingFileHandler(
        tmp_path / "voice-server.log", maxBytes=1024, backupCount=1, encoding="utf-8")
    try:
        _write(tmp_path / "ancient.log", 2 * MIB, age_seconds=7200)
        _write(tmp_path / "older.log", 2 * MIB, age_seconds=3600)
        record = logging.LogRecord("t", logging.INFO, "budget-test", 1, "x" * 2048, (), None)
        handler.emit(record)   # 第一次写入把文件从 0 撑过上限
        handler.emit(record)   # 第二次写入触发轮转，并在轮转后执行预算剔除

        assert (tmp_path / "voice-server.log.1").is_file()
        assert (tmp_path / "ancient.log").exists() is False
        assert (tmp_path / "voice-server.log").is_file()
    finally:
        handler.close()


def test_eviction_is_global_across_every_log_kind(tmp_path):
    """剔除只按全局时间排序，不按类别配额：最旧的审计 jsonl 先走，最新的主日志最后走。"""
    _write(tmp_path / "voice-server-llm-context.jsonl.2", 2 * MIB, age_seconds=40000)
    _write(tmp_path / "asr-worker.log.1", 2 * MIB, age_seconds=30000)
    _write(tmp_path / "application-voice.log.3", 2 * MIB, age_seconds=20000)
    _write(tmp_path / "audio-cpp-tts.log.1", 2 * MIB, age_seconds=10000)
    _write(tmp_path / "speaker-worker.err.log", 2 * MIB, age_seconds=5000)
    _write(tmp_path / "voice-server.log", 2 * MIB, age_seconds=10)

    result = log_budget.enforce_log_budget(tmp_path, budget_bytes=8 * MIB,
                                           min_age_seconds=0, quiet=True)

    # 10 MiB → 8 MiB：按修改时间最旧的两个跨类别出清，不挑类别。
    assert result["removed"] == ["voice-server-llm-context.jsonl.2", "asr-worker.log.1"]
    for survivor in ("application-voice.log.3", "audio-cpp-tts.log.1",
                     "speaker-worker.err.log", "voice-server.log"):
        assert (tmp_path / survivor).is_file(), survivor


def test_eviction_does_not_prefer_any_kind_when_ages_match(tmp_path):
    """同龄文件按文件名稳定排序，仍然不会只清一类。"""
    kinds = ("analysis-worker.log", "application-asr.log", "asr-worker-llm-context.jsonl",
             "audio-cpp-tts.log", "desktop-service.log", "voice-server.log")
    for name in kinds:
        _write(tmp_path / name, MIB, age_seconds=3600)

    result = log_budget.enforce_log_budget(tmp_path, budget_bytes=3 * MIB,
                                           min_age_seconds=0, quiet=True)

    assert result["removed"] == ["analysis-worker.log", "application-asr.log", "asr-worker-llm-context.jsonl"]
    assert sum(1 for name in kinds if (tmp_path / name).is_file()) == 3


def test_registered_open_file_is_protected_for_every_sweep(tmp_path):
    """本进程登记过的文件，即使不显式传 protect 也不会被删除。"""
    held = _write(tmp_path / "audio-cpp-tts.log", 2 * MIB, age_seconds=7200)
    _write(tmp_path / "older.log", 2 * MIB, age_seconds=3600)
    log_budget.protect_log_files(held)
    try:
        result = log_budget.enforce_log_budget(tmp_path, budget_bytes=2 * MIB,
                                               min_age_seconds=0, quiet=True)
    finally:
        log_budget.unprotect_log_files(held)

    assert held.is_file()
    assert "audio-cpp-tts.log" in result["skipped"]
    assert (tmp_path / "older.log").exists() is False


def test_open_file_limits_leave_room_for_the_total_budget():
    # 被存活进程打开的文件在 Windows 上无法回收，因此它们之和必须远小于总预算。
    open_set = sum(max_bytes for max_bytes, _ in _FILE_LIMITS.values())
    open_set += _DEFAULT_FILE_LIMIT[0] + _AUDIT_FILE_LIMIT[0]
    open_set += 4 * 3 * MIB  # application-<key>.log（监督器手工轮转，最多三份并存）
    assert open_set < log_budget.DEFAULT_BUDGET_MB * MIB

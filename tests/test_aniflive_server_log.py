"""AnifLive（本地语音）日志容量、写入故障与管道排空回归。"""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from voice_terminal import process_log
from voice_terminal.providers.aniflive_server import AnifLiveTtsServer


def test_process_log_rotates_with_a_fixed_number_of_files(tmp_path):
    path = tmp_path / "service.log"
    sink = process_log.AsyncProcessLog(path, max_bytes=256, backups=2, queue_size=128)
    for index in range(60):
        sink.write(f"{index:02d} " + "x" * 80 + "\n")
    sink.close()
    logs = list(tmp_path.glob("service.log*"))
    assert len(logs) == 3
    assert all(file.stat().st_size <= 256 for file in logs)
    assert "59 " in path.read_text("utf-8")
    assert sink.dropped_lines == 0


@pytest.mark.parametrize("failure", ["open", "write"])
def test_file_failure_does_not_prevent_ready_or_stdout_drain(tmp_path, monkeypatch, failure):
    if failure == "open":
        monkeypatch.setattr(process_log, "_ProcessLogHandler", lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘不可写")))
    else:
        monkeypatch.setattr(process_log._ProcessLogHandler, "emit", lambda *a: (_ for _ in ()).throw(OSError("磁盘已满")))
    async def scenario():
        consumed = []
        async def stdout():
            yield b'ANIFLIVE_READY=' + json.dumps({"nonce": "n", "pid": 42, "port": 1234}).encode() + b'\n'
            for index in range(500):
                consumed.append(index)
                yield b'normal output\n'
        server = object.__new__(AnifLiveTtsServer)
        server.local, server._state = tmp_path, 'loading'
        server._process = SimpleNamespace(pid=42, stdout=stdout())
        ready = asyncio.get_running_loop().create_future()
        await server._read(ready, 'n')
        assert ready.result() == 'http://127.0.0.1:1234'
        assert len(consumed) == 500
        assert server._state == 'loading'
    asyncio.run(scenario())


def test_slow_log_writer_cannot_delay_ready_or_backpressure_model_stdout(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = process_log._ProcessLogHandler.emit
    def slow_emit(handler, record):
        entered.set()
        release.wait(3)
        original(handler, record)
    monkeypatch.setattr(process_log._ProcessLogHandler, "emit", slow_emit)
    async def scenario():
        consumed = []
        async def stdout():
            yield b'first log\n'
            yield b'ANIFLIVE_READY=' + json.dumps({"nonce": "n", "pid": 42, "port": 1234}).encode() + b'\n'
            for index in range(1000):
                consumed.append(index)
                yield b'normal log\n'
        server = object.__new__(AnifLiveTtsServer)
        server.local, server._state = tmp_path, 'loading'
        server._process = SimpleNamespace(pid=42, stdout=stdout())
        ready = asyncio.get_running_loop().create_future()
        reader = asyncio.create_task(server._read(ready, 'n'))
        try:
            assert await asyncio.wait_for(ready, 1) == 'http://127.0.0.1:1234'
            assert len(consumed) == 1000
            assert await asyncio.to_thread(entered.wait, 1)
        finally:
            release.set()
            await reader
    asyncio.run(scenario())


def test_stuck_file_io_does_not_create_unbounded_writers_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(process_log, "_WRITER_SLOTS", threading.BoundedSemaphore(1))
    entered, release = threading.Event(), threading.Event()
    original = process_log._ProcessLogHandler.emit
    def blocked_emit(handler, record):
        entered.set()
        release.wait(5)
        original(handler, record)
    monkeypatch.setattr(process_log._ProcessLogHandler, "emit", blocked_emit)
    first = process_log.AsyncProcessLog(tmp_path / 'first.log')
    try:
        first.write('blocked\n')
        assert entered.wait(1)
        first.close()  # 有界等待返回；未退出的文件调用继续占用唯一写线程额度。
        for index in range(8):
            restarted = process_log.AsyncProcessLog(tmp_path / f'restarted-{index}.log')
            restarted.write('无需为同一磁盘故障继续增加线程\n')
            restarted.close()
        assert not list(tmp_path.glob('restarted-*.log'))
    finally:
        release.set()
        first.close()
    resumed = process_log.AsyncProcessLog(tmp_path / 'resumed.log')
    resumed.write('磁盘恢复后重新获得写入额度\n')
    resumed.close()
    assert '磁盘恢复' in (tmp_path / 'resumed.log').read_text('utf-8')

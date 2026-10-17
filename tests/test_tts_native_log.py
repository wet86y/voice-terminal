"""原生 stdout 转发：时间戳前缀与故障降级。"""

import asyncio
import io
import logging
import os
import re
import threading
import time

from voice_terminal.providers.audio_cpp_tts import AudioCppTtsServer

STAMPED = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ")


class _BrokenStream:
    """第一次读取后失败，用于验证转发线程不会中断管道排空。"""

    def __init__(self, first: bytes) -> None:
        self._first = first
        self._raised = False
        self.drained = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if not self._raised:
            self._raised = True
            return self._first
        self.drained += 1
        raise OSError("模拟读取失败")

    def close(self) -> None:
        self.closed = True


def _detached_server(log_path):
    server = AudioCppTtsServer.__new__(AudioCppTtsServer)
    server._log_file = log_path.open("ab", buffering=0)
    server._log_pump_thread = None
    server._log_pump_done = None
    return server


def test_native_log_pump_prefixes_every_line(tmp_path):
    log_path = tmp_path / "audio-cpp-tts.log"
    server = _detached_server(log_path)
    stream = io.BytesIO("第一行\nsecond line\n未换行结尾".encode("utf-8"))
    try:
        server._start_log_pump(stream)
        assert isinstance(server._log_pump_thread, threading.Thread)
        server._log_pump_thread.join(timeout=5)
        assert not server._log_pump_thread.is_alive()
    finally:
        server._log_file.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert all(STAMPED.match(line) for line in lines)
    assert lines[0].endswith("第一行")
    assert lines[1].endswith("second line")
    assert lines[2].endswith("未换行结尾")


def test_native_log_pump_degrades_to_drain_without_raising(tmp_path, caplog):
    log_path = tmp_path / "audio-cpp-tts.log"
    server = _detached_server(log_path)
    stream = _BrokenStream(b"first line\n")
    try:
        with caplog.at_level(logging.WARNING, logger="voice_terminal.providers.audio_cpp_tts"):
            server._start_log_pump(stream)
            server._log_pump_thread.join(timeout=5)
            assert not server._log_pump_thread.is_alive()
        assert stream.closed is True
    finally:
        server._log_file.close()

    assert "native_log_pump_failed" in caplog.text
    assert log_path.read_text(encoding="utf-8").startswith("20")


class _FakeProcess:
    pid = 4321
    stdout = None
    returncode = 0

    def poll(self):
        return 0


def _start_process_fixture(tmp_path, monkeypatch) -> tuple[AudioCppTtsServer, dict]:
    """构造一个不启动真实原生程序的 _start_process 调用点。"""
    (tmp_path / "model_specs").mkdir(parents=True, exist_ok=True)
    server = AudioCppTtsServer.__new__(AudioCppTtsServer)
    server.runtime_root = tmp_path
    server.config_path = tmp_path / "server-tts-evaluation.json"
    server.config_path.write_text("{}", encoding="utf-8")
    server.local_model = "voxcpm2"
    server._log_file = None
    server._log_pump_thread = None
    server._log_pump_done = None
    server._owns_process = False
    server._process = None
    server._lock = asyncio.Lock()
    server._management_http = None
    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr("voice_terminal.providers.audio_cpp_tts.subprocess.Popen", fake_popen)
    monkeypatch.setattr(AudioCppTtsServer, "runtime_directory",
                        classmethod(lambda cls, root: tmp_path))
    monkeypatch.setattr(AudioCppTtsServer, "_server_binary", lambda self: tmp_path / "audiocpp_server.exe")
    monkeypatch.setattr(AudioCppTtsServer, "_validate_model_assets", lambda self, model: {"id": model})
    monkeypatch.setattr(AudioCppTtsServer, "_runtime_update_error", classmethod(lambda cls, root, **kwargs: None))
    monkeypatch.setattr(AudioCppTtsServer, "_start_log_pump", lambda self, stream: None)
    return server, captured


def test_native_trace_switch_adds_native_stage_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TTS_NATIVE_TRACE", "1")
    server, captured = _start_process_fixture(tmp_path, monkeypatch)
    try:
        server._start_process()
    finally:
        server._log_file.close()

    command = captured["command"]
    assert "--log" in command and "--log-file" in command
    assert str(tmp_path / "cache" / "logs" / "audiocpp-trace.log") in command


def test_native_log_pump_writes_small_line_before_pipe_closes(tmp_path):
    """真实活管道中一行不足4096字节，也必须在子进程退出之前落盘。"""
    log_path = tmp_path / "native.log"
    server = _detached_server(log_path)
    read_fd, write_fd = os.pipe()
    stream = io.BufferedReader(os.fdopen(read_fd, "rb", buffering=0))
    writer = os.fdopen(write_fd, "wb", buffering=0)
    try:
        server._start_log_pump(stream)
        writer.write(b"warmup complete\n")
        deadline = time.monotonic() + 2
        while log_path.stat().st_size == 0 and time.monotonic() < deadline:
            time.sleep(.01)
        assert "warmup complete" in log_path.read_text("utf-8")
        assert server._log_pump_thread.is_alive(), "测试期间写端仍打开，不能依赖进程退出刷新"
    finally:
        writer.close()
        server._log_pump_thread.join(2)
        server._log_file.close()


def test_started_runtime_identity_hashes_actual_binary(tmp_path, monkeypatch, caplog):
    """启动日志记录实际程序字节摘要和对应目录收据，不使用程序内嵌旧版本串。"""
    import hashlib
    import json
    server, _ = _start_process_fixture(tmp_path, monkeypatch)
    binary = tmp_path / "cuda/audiocpp_server.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"actual-native-binary")
    (tmp_path / "resource-install.json").write_text(json.dumps({"version": "fixed-runtime", "files": [
        {"path": "cuda/audiocpp_server.exe", "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()},
    ]}), "utf-8")
    monkeypatch.setattr(AudioCppTtsServer, "_server_binary", lambda self: binary)
    try:
        with caplog.at_level(logging.INFO, logger="voice_terminal.providers.audio_cpp_tts"):
            server._start_process()
    finally:
        server._log_file.close()
    assert f"binary_path（实际程序路径）={binary}" in caplog.text
    assert hashlib.sha256(binary.read_bytes()).hexdigest() in caplog.text
    assert "installed_version（安装版本）=fixed-runtime" in caplog.text
    assert "binary_matches_receipt（程序与收据一致）=True" in caplog.text
    assert "target_version（目标原生版本）=" in caplog.text
    assert "binary_matches_target（程序与当前目标一致）=False" in caplog.text


def test_native_trace_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_TTS_NATIVE_TRACE", raising=False)
    server, captured = _start_process_fixture(tmp_path, monkeypatch)
    try:
        server._start_process()
    finally:
        server._log_file.close()

    assert "--log-file" not in captured["command"]
    assert not (tmp_path / "cache" / "logs" / "audiocpp-trace.log").exists()


def test_native_pump_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TTS_NATIVE_LOG_PUMP", "0")
    server, captured = _start_process_fixture(tmp_path, monkeypatch)
    try:
        server._start_process()
        assert captured["kwargs"]["stdout"] is server._log_file
    finally:
        server._log_file.close()


def test_native_pump_pipes_stdout_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_TTS_NATIVE_LOG_PUMP", raising=False)
    server, captured = _start_process_fixture(tmp_path, monkeypatch)
    try:
        server._start_process()
        import subprocess
        assert captured["kwargs"]["stdout"] is subprocess.PIPE
    finally:
        server._log_file.close()


def test_native_log_is_protected_while_open_and_released_on_close(tmp_path, monkeypatch):
    from voice_terminal import log_budget

    server, _ = _start_process_fixture(tmp_path, monkeypatch)
    server._start_process()
    log_path = tmp_path / "cache" / "logs" / "audio-cpp-tts.log"
    assert log_budget._key(log_path) in log_budget._global_protect

    asyncio.run(server.close())

    assert log_budget._key(log_path) not in log_budget._global_protect
    assert server._log_file is None

"""Process host（项目工作进程）：真实进程身份、运行库兼容与整树回收。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from voice_terminal.process_host import interpreter_command


def test_unmanaged_interpreter_command_is_unchanged(monkeypatch):
    monkeypatch.delenv("VOICE_PROCESS_HOST", raising=False)
    assert interpreter_command("external-python", "-I", "-c", "print('中文')") == [
        "external-python", "-I", "-c", "print('中文')"]


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows（操作系统）产品宿主")
def test_managed_command_preserves_selected_runtime_and_all_arguments(monkeypatch, tmp_path):
    host = tmp_path / "程序 空格" / "VoiceTerminalDesktop.exe"
    host.parent.mkdir()
    host.touch()
    python = tmp_path / "显卡 运行库" / "python.exe"
    python.parent.mkdir()
    python.touch()
    python.with_name("python312.dll").touch()
    monkeypatch.setenv("VOICE_PROCESS_HOST", str(host))
    arguments = ("-I", "-X", "utf8", "entry.py", "带空格的参数", '含"引号')
    assert interpreter_command(str(python), *arguments) == [
        str(host), "--worker-host", str(python), *arguments]
    # 外部解释器没有相邻动态库时不改变原有启动能力。
    python.with_name("python312.dll").unlink()
    assert interpreter_command(str(python), *arguments) == [str(python), *arguments]


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows（操作系统）产品宿主")
def test_invalid_managed_host_does_not_start_an_unrelated_program(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_PROCESS_HOST", str(tmp_path / "other.exe"))
    with pytest.raises(RuntimeError, match="宿主路径无效"):
        interpreter_command(sys.executable, "-c", "pass")


@pytest.fixture
def native_host():
    # 必须显式指定本轮成功构建的程序，防止旧二进制把工作参数误当成界面启动请求。
    value = os.environ.get("VOICE_TEST_PROCESS_HOST")
    if os.name != "nt" or not value:
        pytest.skip("需要 VOICE_TEST_PROCESS_HOST（本轮已构建宿主）进行原生集成验证")
    host = Path(value)
    assert host.is_absolute() and host.is_file()
    assert Path(sys.executable).with_name("python312.dll").is_file()
    return host


IDENTITY_CODE = '''
import ctypes, json, os, sys
import _socket, _ssl, sqlite3, numpy
image = ctypes.create_unicode_buffer(32768)
ctypes.windll.kernel32.GetModuleFileNameW(None, image, len(image))
identity = ctypes.c_void_p()
result = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(identity))
appid = ctypes.wstring_at(identity) if result == 0 else None
if identity.value:
    ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.ole32.CoTaskMemFree(identity)
print(json.dumps({"image": image.value, "python": sys.executable, "prefix": sys.prefix,
    "appid": appid, "isolated": sys.flags.isolated, "pid": os.getpid()}), flush=True)
'''


def test_native_host_retains_runtime_paths_and_native_extensions(native_host):
    result = subprocess.run([str(native_host), "--worker-host", sys.executable,
        "-I", "-B", "-X", "utf8", "-c", IDENTITY_CODE], capture_output=True, text=True,
        encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert Path(value["image"]).resolve() == native_host.resolve()
    assert Path(value["python"]).resolve() == Path(sys.executable).resolve()
    assert Path(value["prefix"]).resolve() == Path(sys.prefix).resolve()
    assert value["appid"] == "com.voiceterminal.desktop"
    assert value["isolated"] == 1


def test_native_host_preserves_pipe_encoding_and_exit_code(native_host):
    source = 'import sys;print("收到："+sys.stdin.readline().strip());print("诊断中文",file=sys.stderr);sys.exit(7)'
    result = subprocess.run([str(native_host), "--worker-host", sys.executable,
        "-I", "-B", "-X", "utf8", "-c", source], input="管道 空格\n", capture_output=True,
        text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 7
    assert result.stdout.strip() == "收到：管道 空格"
    assert result.stderr.strip() == "诊断中文"


def test_native_host_failure_returns_without_opening_a_window(native_host, tmp_path):
    result = subprocess.run([str(native_host), "--worker-host", str(tmp_path / "python.exe")],
        capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 1
    assert "解释器" in result.stderr


def test_managed_module_entry_runs_in_product_host_without_loading_a_model(native_host, monkeypatch):
    from voice_terminal.paths import python_command

    monkeypatch.setenv("VOICE_PROCESS_HOST", str(native_host))
    root = Path(__file__).resolve().parents[1]
    command = python_command(sys.executable, "voice_terminal.model_process", "--help", root=root)
    assert Path(command[0]).resolve() == native_host.resolve()
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert "--model-options" in result.stdout and "模型专用配置" in result.stdout


def test_managed_host_descendants_exit_with_their_owner(native_host, tmp_path):
    from voice_terminal.application.windows import Job, ManagedProcess

    root = Path(__file__).resolve().parents[1]
    record = tmp_path / "子进程身份.json"
    child_code = "import time;time.sleep(60)"
    source = (f"import sys;sys.path.insert(0,{str(root)!r});"
              "import json,subprocess,time;from pathlib import Path;"
              "from voice_terminal.process_host import interpreter_command;"
              f"child=subprocess.Popen(interpreter_command(sys.executable,'-I','-B','-c',{child_code!r}));"
              f"Path({str(record)!r}).write_text(json.dumps({{'pid':child.pid}}),encoding='utf-8');"
              "time.sleep(60)")
    environment = {**os.environ, "VOICE_PROCESS_HOST": str(native_host)}
    command = [str(native_host), "--worker-host", sys.executable, "-I", "-B", "-X", "utf8", "-c", source]
    outer = Job()
    process = None
    identities = []
    try:
        process = ManagedProcess(command, cwd=root, env=environment, output=tmp_path / "进程.log",
            outer_job=outer, event_name=f"Local\\voice-host-test-{os.getpid()}-{time.time_ns()}", key="test")
        deadline = time.monotonic() + 15
        while not record.is_file() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        assert record.is_file(), (tmp_path / "进程.log").read_text("utf-8")
        child = json.loads(record.read_text("utf-8"))["pid"]
        identities = [psutil.Process(process.pid), psutil.Process(child)]
        assert all(Path(item.exe()).resolve() == native_host.resolve() for item in identities)
        assert identities[1].ppid() == process.pid
        outer.close()
        _, alive = psutil.wait_procs(identities, timeout=10)
        assert not alive, "外层作业关闭后不能留下宿主或模型后代进程"
    finally:
        if process is not None:
            process.close()
        outer.close()


def test_host_cannot_execute_python_before_start_gate_is_released(native_host, tmp_path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = f"Local\\voice-host-gate-test-{os.getpid()}-{time.time_ns()}"
    gate = kernel.CreateEventW(None, True, False, name)
    assert gate
    marker = tmp_path / "已执行.json"
    source = ("import os;assert 'VOICE_PROCESS_START_GATE' not in os.environ;"
              f"from pathlib import Path;Path({str(marker)!r}).write_text('ready')")
    process = None
    try:
        process = subprocess.Popen([str(native_host), "--worker-host", sys.executable, "-I", "-B", "-c", source],
            env={**os.environ, "VOICE_PROCESS_START_GATE": name}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        assert process.poll() is None and not marker.exists()
        assert kernel.SetEvent(gate)
        _, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        assert marker.read_text() == "ready"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        kernel.CloseHandle(gate)


AUDIO_CODE = '''
import ctypes as C, json, os, time, uuid
class GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]
def guid(value):
    return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)
ole = C.OleDLL("ole32")
ole.CoInitializeEx(None, 0)
ole.CoTaskMemFree.argtypes = [C.c_void_p]
def call(pointer, index, types, *values):
    table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
    result = C.WINFUNCTYPE(C.c_long, C.c_void_p, *types)(table[index])(pointer, *values)
    if result < 0:
        raise OSError("COM（组件对象模型）调用失败", hex(result & 0xffffffff))
def sessions():
    owned = []
    def output(pointer, index, types=(), values=()):
        value = C.c_void_p()
        call(pointer, index, (*types, C.POINTER(C.c_void_p)), *values, C.byref(value))
        owned.append(value)
        return value
    def count(pointer):
        value = C.c_uint()
        call(pointer, 3, (C.POINTER(C.c_uint),), C.byref(value))
        return value.value
    def string(pointer, index):
        value = C.c_void_p()
        call(pointer, index, (C.POINTER(C.c_void_p),), C.byref(value))
        try:
            return C.wstring_at(value)
        finally:
            ole.CoTaskMemFree(value)
    enumerator = C.c_void_p()
    clsid = guid("bcde0395-e52f-467c-8e3d-c4579291692e")
    iid = guid("a95664d2-9614-4f35-a746-de8db63617e6")
    ole.CoCreateInstance(C.byref(clsid), None, 23, C.byref(iid), C.byref(enumerator))
    owned.append(enumerator)
    rows = []
    try:
        devices = output(enumerator, 3, (C.c_int, C.c_uint), (2, 1))
        for number in range(count(devices)):
            device = output(devices, 4, (C.c_uint,), (number,))
            manager_id = guid("77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f")
            manager = output(device, 3, (C.POINTER(GUID), C.c_uint, C.c_void_p),
                (C.byref(manager_id), 23, None))
            items = output(manager, 5)
            for number in range(count(items)):
                session = output(items, 4, (C.c_int,), (number,))
                control_id = guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d")
                control = output(session, 0, (C.POINTER(GUID),), (C.byref(control_id),))
                pid = C.c_uint()
                call(control, 14, (C.POINTER(C.c_uint),), C.byref(pid))
                if pid.value == os.getpid():
                    group = GUID()
                    call(session, 8, (C.POINTER(GUID),), C.byref(group))
                    rows.append({"name": string(session, 4), "icon": string(session, 6),
                        "group": str(uuid.UUID(bytes_le=bytes(group)))})
        return rows
    finally:
        for pointer in reversed(owned):
            table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
            C.WINFUNCTYPE(C.c_ulong, C.c_void_p)(table[2])(pointer)
import sounddevice as sd
try:
    info = sd.query_devices(kind="output")
except sd.PortAudioError:
    raise SystemExit(77)
rate = int(info["default_samplerate"])
channels = min(2, int(info["max_output_channels"]))
results = []
for attempt in range(2):
    with sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16") as stream:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            stream.write(bytes(rate // 50 * channels * 2))
            rows = sessions()
            if rows and all(row["name"] == "Voice Terminal · 语音终端" and
                    row["icon"].lower().endswith("voiceterminaldesktop.exe") and
                    row["group"] == "6f0c6411-e33d-4e4d-b606-077f92c33cc9" for row in rows):
                results.append(rows)
                break
            time.sleep(.1)
        else:
            raise AssertionError("音频会话未取得项目身份：" + repr(rows))
print(json.dumps(results, ensure_ascii=False))
ole.CoUninitialize()
'''


def test_audio_sessions_use_project_identity_after_stream_recreation(native_host):
    result = subprocess.run([str(native_host), "--worker-host", sys.executable,
        "-I", "-B", "-X", "utf8", "-c", AUDIO_CODE],
        env={**os.environ, "VOICE_RESOURCE_ROLE": "voice"},
        capture_output=True, text=True, encoding="utf-8", timeout=25)
    if result.returncode == 77:
        pytest.skip("本机没有可用输出设备，无法建立静音音频会话")
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 2


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_host_keeps_selected_tensor_runtime_and_can_compute(native_host, device):
    root = Path(__file__).resolve().parents[1]
    source = f'''
import json, sys
sys.path.insert(0, {str(root)!r})
from voice_terminal.tensor_runtime import activate_tensor_runtime
activate_tensor_runtime({device!r})
import torch, onnxruntime
if {device!r} == "cuda" and not torch.version.cuda:
    raise SystemExit(77)
actual = "cuda" if {device!r} == "cuda" and torch.cuda.is_available() else "cpu"
value = torch.ones((2, 2), device=actual)
assert (value @ value).sum().item() == 8
print(json.dumps({{"module": torch.__file__, "cuda_build": bool(torch.version.cuda),
    "device": actual, "providers": onnxruntime.get_available_providers()}}))
'''
    result = subprocess.run([str(native_host), "--worker-host", sys.executable,
        "-I", "-B", "-X", "utf8", "-c", source], capture_output=True,
        text=True, encoding="utf-8", timeout=60)
    baseline = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", source],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    if baseline.returncode == 77:
        assert result.returncode == 77
        pytest.skip("本机运行环境没有显卡张量构建")
    assert baseline.returncode == 0, baseline.stderr
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    # 进程宿主必须保留原环境的真实能力；不能假设每个显卡张量环境同时安装了显卡 ONNX（推理库）。
    assert value == json.loads(baseline.stdout)
    if device == "cpu":
        assert value["device"] == "cpu"
        record = Path(sys.executable).with_name("runtime.json")
        runtime = json.loads(record.read_text("utf-8")) if record.is_file() else {}
        if runtime.get("gpu_profile") or runtime.get("compatible_gpu") or runtime.get("profile") == "cu128":
            assert not value["cuda_build"] and "cpu-packages" in value["module"]
    else:
        assert value["cuda_build"]

"""Win32 lifecycle（Windows 生命周期）：作业、互斥量和正常退出事件。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as W
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

log = logging.getLogger(__name__)

if os.name == "nt":
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("length", W.DWORD), ("descriptor", W.LPVOID), ("inherit", W.BOOL)]

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
            ("title", W.LPWSTR), ("x", W.DWORD), ("y", W.DWORD), ("xsize", W.DWORD),
            ("ysize", W.DWORD), ("xchars", W.DWORD), ("ychars", W.DWORD),
            ("fill", W.DWORD), ("flags", W.DWORD), ("show", W.WORD),
            ("reserved2size", W.WORD), ("reserved2", ctypes.POINTER(ctypes.c_byte)),
            ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]

    class PROCESS_INFO(ctypes.Structure):
        _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE), ("pid", W.DWORD), ("tid", W.DWORD)]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", W.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
            ("active_limit", W.DWORD), ("affinity", ctypes.c_size_t),
            ("priority", W.DWORD), ("scheduling", W.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("basic", BASIC_LIMIT), ("io", IO_COUNTERS), ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    def signature(name, result, *arguments):
        fn = getattr(kernel, name)
        fn.restype, fn.argtypes = result, list(arguments)
        return fn

    close = signature("CloseHandle", W.BOOL, W.HANDLE)
    create_job = signature("CreateJobObjectW", W.HANDLE, W.LPVOID, W.LPCWSTR)
    set_job = signature("SetInformationJobObject", W.BOOL, W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD)
    assign_job = signature("AssignProcessToJobObject", W.BOOL, W.HANDLE, W.HANDLE)
    terminate_job = signature("TerminateJobObject", W.BOOL, W.HANDLE, W.UINT)
    create_mutex = signature("CreateMutexW", W.HANDLE, W.LPVOID, W.BOOL, W.LPCWSTR)
    create_event = signature("CreateEventW", W.HANDLE, W.LPVOID, W.BOOL, W.BOOL, W.LPCWSTR)
    open_event = signature("OpenEventW", W.HANDLE, W.DWORD, W.BOOL, W.LPCWSTR)
    set_event = signature("SetEvent", W.BOOL, W.HANDLE)
    wait_handle = signature("WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD)
    get_exit = signature("GetExitCodeProcess", W.BOOL, W.HANDLE, ctypes.POINTER(W.DWORD))
    resume_thread = signature("ResumeThread", W.DWORD, W.HANDLE)
    terminate_process = signature("TerminateProcess", W.BOOL, W.HANDLE, W.UINT)
    create_file = signature("CreateFileW", W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD,
        ctypes.POINTER(SECURITY_ATTRIBUTES), W.DWORD, W.DWORD, W.HANDLE)
    create_process = signature("CreateProcessW", W.BOOL, W.LPCWSTR, W.LPWSTR,
        W.LPVOID, W.LPVOID, W.BOOL, W.DWORD, W.LPVOID, W.LPCWSTR,
        ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFO))
    get_dll_directory = signature("GetDllDirectoryW", W.DWORD, W.DWORD, W.LPWSTR)
    set_dll_directory = signature("SetDllDirectoryW", W.BOOL, W.LPCWSTR)

    def checked(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value


class SingleInstance:
    def __init__(self, name: str) -> None:
        self.handle = None
        self.acquired = True
        if os.name == "nt":
            self.handle = checked(create_mutex(None, False, "Local\\" + name))
            self.acquired = ctypes.get_last_error() != 183

    def close(self) -> None:
        if self.handle:
            close(self.handle)
            self.handle = None


class ShutdownEvent:
    def __init__(self, name: str, *, existing: bool = False) -> None:
        self.name = name
        self.handle = None
        self._set = False
        if os.name == "nt":
            self.handle = checked(open_event(0x100000, False, name) if existing else create_event(None, True, False, name))

    def signal(self) -> None:
        self._set = True
        if self.handle:
            checked(set_event(self.handle))

    def signaled(self) -> bool:
        return wait_handle(self.handle, 0) == 0 if self.handle else self._set

    def close(self) -> None:
        if self.handle:
            close(self.handle)
            self.handle = None


class Job:
    def __init__(self) -> None:
        self.handle = None
        if os.name == "nt":
            self.handle = checked(create_job(None, None))
            limits = EXTENDED_LIMIT()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE（最后句柄关闭时回收进程树）。
            try:
                checked(set_job(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            except Exception:
                self.close()
                raise

    def terminate(self) -> None:
        if self.handle:
            checked(terminate_job(self.handle, 1))

    def close(self) -> None:
        if self.handle:
            close(self.handle)
            self.handle = None


class ManagedProcess:
    """Process handle（进程句柄）保留真实所有权，避免通过端口或 PID 猜测回收。"""
    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str],
                 output: Path, outer_job: Job, event_name: str, key: str = "") -> None:
        self.command = command
        self.key = key  # key（组件标识）：停止请求必须能写清是哪个组件、什么原因。
        self.created_at = time.time()
        self.event = ShutdownEvent(event_name)
        self.group = Job()
        self.handle = None
        self.process = None
        env = external_environment({**env, "VOICE_APP_SHUTDOWN_EVENT": event_name})
        output.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            with output.open("ab") as log:
                self.process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                    stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
            self.pid = self.process.pid
            return
        handles = []
        info = PROCESS_INFO()
        try:
            security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
            for path, access in ((str(output), 0x4), ("NUL", 0x80000000)):
                handle = create_file(path, access, 3, ctypes.byref(security), 4, 0x80, None)
                if handle == W.HANDLE(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                handles.append(handle)
            startup = STARTUPINFO()
            startup.cb, startup.flags = ctypes.sizeof(startup), 0x100
            startup.stdin, startup.stdout, startup.stderr = handles[1], handles[0], handles[0]
            environment = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0")
            # PyInstaller（打包工具）的 DLL（动态链接库）目录不能传给业务运行环境。
            previous_dll = ctypes.create_unicode_buffer(32768)
            reset_dll = bool(getattr(sys, "frozen", False))
            if reset_dll:
                get_dll_directory(len(previous_dll), previous_dll)
                checked(set_dll_directory(None))
            try:
                checked(create_process(None, ctypes.create_unicode_buffer(subprocess.list2cmdline(command)),
                    None, None, True, 0x4 | 0x8000000 | 0x400, environment, str(cwd),
                    ctypes.byref(startup), ctypes.byref(info)))
            finally:
                if reset_dll:
                    checked(set_dll_directory(previous_dll.value or None))
            # CREATE_SUSPENDED（挂起创建）保证子进程执行前已经进入两层作业。
            checked(assign_job(outer_job.handle, info.process))
            checked(assign_job(self.group.handle, info.process))
            if resume_thread(info.thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle, self.pid = info.process, int(info.pid)
        except Exception:
            if info.process:
                terminate_process(info.process, 1)
                close(info.process)
            self.group.close()
            self.event.close()
            raise
        finally:
            if info.thread:
                close(info.thread)
            for handle in handles:
                close(handle)

    def poll(self) -> int | None:
        if self.process:
            return self.process.poll()
        if self.handle and wait_handle(self.handle, 0) == 258:
            return None
        code = W.DWORD()
        if self.handle:
            checked(get_exit(self.handle, ctypes.byref(code)))
        return int(code.value)

    def request_stop(self, reason: str = "") -> None:
        """request_stop（请求受管退出）：记录原因，便于事后区分退出、重试与回收。"""
        log.info("请求停止工作进程：组件=%s，原因=%s，进程编号=%s", self.key or "unknown", reason or "未说明", self.pid)
        self.event.signal()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def kill(self) -> None:
        if self.process:
            import signal
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.group.terminate()

    def close(self) -> None:
        self.group.close()
        self.event.close()
        if self.handle:
            close(self.handle)
            self.handle = None


def external_environment(env: dict[str, str]) -> dict[str, str]:
    """PATH（可执行文件搜索路径）去除打包目录，隔离本机业务 Python 与 Node.js。"""
    bundle = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not bundle:
        return env
    result = dict(env)
    bundle = Path(bundle).resolve()
    for key in result:
        if key.upper() == "PATH":
            result[key] = os.pathsep.join(part for part in result[key].split(os.pathsep)
                if part and not Path(part).resolve().is_relative_to(bundle))
    return result

#!/usr/bin/env python3
"""Qualify workspace migration transactions（验收工作区迁移事务）.

This tool only uses dedicated D: test workspaces and a qualification build.
It does not read the user's default workspace locator, and it never stores
native command output that could contain private data.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid


TEST_ROOT = Path("D:/项目开发/测试").resolve(strict=False)
DESKTOP_EXE = "VoiceTerminalDesktop.exe"
LAUNCHER_EXE = "VoiceTerminal.exe"
UNMANAGED_LINK_NAME = "qualification-unmanaged-junction"


class QualificationError(RuntimeError):
    """Safe reportable failure（可安全写入报告的失败原因）."""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在 D 盘隔离目录验收工作区事务迁移")
    parser.add_argument("--source-workspace", type=Path, required=True,
                        help="已安装完整分发程序的旧测试工作区，必须位于 D:/项目开发/测试 内")
    parser.add_argument("--run-root", type=Path,
                        help="本轮新建的隔离验收目录；省略时在测试根下自动创建中文和空格目录")
    parser.add_argument("--copy-timeout-seconds", type=int, default=7200,
                        help="单次迁移或恢复最长等待秒数")
    parser.add_argument("--ready-timeout-seconds", type=int, default=360,
                        help="测试桌面写入 workspace-ready.json（就绪标记）的最长等待秒数")
    return parser.parse_args()


def _reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _inside(path: Path, parent: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(parent.resolve(strict=False))
    except (OSError, ValueError):
        return False


def _bounded(path: Path, label: str, *, must_exist: bool = False) -> Path:
    candidate = path.expanduser().absolute()
    resolved = candidate.resolve(strict=must_exist)
    root = TEST_ROOT.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        raise QualificationError(f"{label} 必须位于 D:/项目开发/测试 的独立子目录内。")
    cursor = candidate
    while cursor != root and root in cursor.parents:
        if _reparse(cursor):
            raise QualificationError(f"{label} 路径不能经过符号链接或目录联接。")
        cursor = cursor.parent
    return resolved


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError("验收所需 JSON（结构化数据）文件缺失或无效。") from exc
    if not isinstance(value, dict):
        raise QualificationError("验收所需 JSON（结构化数据）文件顶层必须为对象。")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_file(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise QualificationError(f"{label} 路径越出测试工作区。")
    cursor = path
    while cursor != root:
        if _reparse(cursor):
            raise QualificationError(f"{label} 路径经过不受管链接或目录联接。")
        cursor = cursor.parent
    if not resolved.is_file():
        raise QualificationError(f"{label} 必须是普通文件。")
    return resolved


def _safe_directory(path: Path, root: Path, label: str) -> Path:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)) or not resolved.is_dir():
        raise QualificationError(f"{label} 必须是工作区中的普通目录。")
    cursor = path
    while cursor != root:
        if _reparse(cursor):
            raise QualificationError(f"{label} 路径经过不受管链接或目录联接。")
        cursor = cursor.parent
    return resolved


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _psutil():
    try:
        import psutil
    except ImportError as exc:
        raise QualificationError("验收 Python（解释器）缺少 psutil（进程身份核验库）。") from exc
    return psutil


def _process_identity(pid: int, expected_exe: Path) -> tuple[object, float]:
    psutil = _psutil()
    try:
        process = psutil.Process(pid)
        created_at = float(process.create_time())
        executable = Path(process.exe()).resolve(strict=True)
    except (psutil.Error, OSError) as exc:
        raise QualificationError("无法核对测试进程的 PID（进程编号）、创建时间和执行文件。") from exc
    if os.path.normcase(str(executable)) != os.path.normcase(str(expected_exe.resolve(strict=True))):
        raise QualificationError("测试进程执行文件与预期路径不一致，拒绝操作该进程。")
    return process, created_at


def _verify_native_pid(record: dict, prefix: str, process_id: int,
                       expected_exe: Path, created_at: float) -> None:
    recorded_pid = record.get(f"{prefix}_pid")
    recorded_exe = record.get(f"{prefix}_executable")
    recorded_created = record.get(f"{prefix}_create_time")
    if recorded_pid != process_id or not isinstance(recorded_exe, str) or not isinstance(recorded_created, int):
        raise QualificationError("原生工作区事务缺少匹配的受测进程身份，拒绝操作其他进程。")
    if os.path.normcase(str(Path(recorded_exe).resolve(strict=False))) != os.path.normcase(str(expected_exe.resolve(strict=True))):
        raise QualificationError("原生工作区事务记录的执行文件不是本轮测试入口。")
    # Windows FILETIME（Windows 文件时间）使用 1601 年纪元和 100 纳秒刻度。
    recorded_unix = (recorded_created - 116444736000000000) / 10_000_000
    if abs(recorded_unix - created_at) > 2.0:
        raise QualificationError("原生工作区事务记录的进程创建时间与受测进程不匹配。")


def _kill_test_launcher(process: subprocess.Popen, expected_exe: Path,
                        launched_at: float, journal_record: dict | None = None) -> None:
    if process.poll() is not None:
        return
    tracked, current_created = _process_identity(process.pid, expected_exe)
    if abs(current_created - launched_at) > 1.0:
        raise QualificationError("测试入口 PID（进程编号）已复用，拒绝终止。")
    if journal_record is not None:
        _verify_native_pid(journal_record, "helper", process.pid, expected_exe, launched_at)
    # Popen.kill（终止本次启动句柄所绑定的进程）不会按全局名称搜索或批量杀进程。
    process.kill()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise QualificationError("受测启动入口在终止后仍未退出。") from exc
    del tracked


def _run_short(command: list[str], launcher: Path, timeout: int = 600) -> tuple[int, str]:
    process = subprocess.Popen(command, cwd=launcher.parent, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                               errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        identity, launched_at = _process_identity(process.pid, launcher)
    except QualificationError:
        # Very short commands may exit before the process snapshot; never terminate an unverified PID.
        identity = None
        launched_at = 0.0
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if identity is None:
            raise QualificationError("短原生命令超时，且无法验证测试进程身份。") from exc
        _kill_test_launcher(process, launcher, launched_at)
        raise QualificationError("原生工作区准备命令超过验收时限。") from exc
    return process.returncode, output


def _prepare_task(launcher: Path, config: Path, source: Path,
                  target: Path, kind: str) -> Path:
    if target.exists():
        raise QualificationError("每个迁移场景都必须使用新建的空目标目录。")
    target.mkdir(parents=True, exist_ok=False)
    command = [str(launcher), "--qualification-config", str(config),
               "--prepare-workspace-task", str(target), "--task-kind", kind]
    return_code, _ = _run_short(command, launcher)
    if return_code != 0:
        raise QualificationError("原生程序未能准备隔离的工作区事务。")
    record_path = source / "data/state/qualification-workspace-task.json"
    record = _json(record_path)
    journal_value = record.get("journal")
    if not isinstance(journal_value, str):
        raise QualificationError("原生验收结果缺少事务记录路径。")
    journal = _bounded(Path(journal_value), "workspace task journal（工作区事务记录）", must_exist=True)
    expected = (target / "tmp/workspace-task.json").resolve(strict=True)
    if journal != expected:
        raise QualificationError("原生验收返回的事务记录不属于本次目标工作区。")
    journal_record = _json(journal)
    if (journal_record.get("kind") != kind or journal_record.get("state") != "prepared"
            or journal_record.get("parent_pid") != 0):
        raise QualificationError("原生工作区事务没有以隔离的 prepared（已准备）状态创建。")
    return journal


def _read_progress_line(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _run_task(executable: Path, config: Path, journal: Path, *, timeout: int,
              normal_startup: bool = False, interrupt_copy: bool = False) -> dict:
    started = time.monotonic()
    command = [str(executable)]
    if not normal_startup:
        command += ["--workspace-task", str(journal)]
    command += ["--qualification-config", str(config)]
    process = subprocess.Popen(command, cwd=executable.parent, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                               errors="replace", bufsize=1,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _, launched_at = _process_identity(process.pid, executable)
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, name="workspace-transaction-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    phases: dict[str, dict] = {}
    notified_phase: str | None = None
    notified_at = 0.0
    unmanaged_link_error = False
    interrupted = False
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.25)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        if "迁移遇到非受管链接" in line:
            unmanaged_link_error = True
        value = _read_progress_line(line)
        if value is None:
            continue
        phase = value.get("phase")
        completed = value.get("completed_bytes")
        total = value.get("total_bytes")
        if isinstance(phase, str) and isinstance(completed, int) and isinstance(total, int):
            summary = phases.setdefault(phase, {"last_completed_bytes": 0, "total_bytes": total,
                                                "progress_events": 0})
            summary["last_completed_bytes"] = completed
            summary["total_bytes"] = total
            summary["progress_events"] += 1
            # progress（进度）只转发已提取的阶段及字节数，避免输出原生命令正文。
            now = time.monotonic()
            if phase != notified_phase or now - notified_at >= 30:
                print(json.dumps({
                    "event": "workspace_progress", "phase": phase,
                    "completed_bytes": completed, "total_bytes": total,
                    "fields": {"event": "工作区验收进度事件", "phase": "事务阶段",
                               "completed_bytes": "已复制或校验字节数", "total_bytes": "本阶段总字节数"},
                }, ensure_ascii=False), flush=True)
                notified_phase, notified_at = phase, now
            if (interrupt_copy and phase == "复制并校验工作区"
                    and completed >= 1024 * 1024 and 0 < total and completed < total):
                journal_record = _json(journal)
                _kill_test_launcher(process, executable, launched_at, journal_record)
                interrupted = True
                break
    if not interrupted and process.poll() is None:
        _kill_test_launcher(process, executable, launched_at)
        reader.join(timeout=5)
        raise QualificationError("原生工作区事务超过验收时限。")
    if not interrupted:
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired as exc:
            _kill_test_launcher(process, executable, launched_at)
            raise QualificationError("原生工作区事务未能正常结束。") from exc
    reader.join(timeout=5)
    return {"return_code": process.returncode, "interrupted_during_copy": interrupted,
            "unmanaged_link_error": unmanaged_link_error, "phases": phases,
            "elapsed_seconds": round(time.monotonic() - started, 2)}


def _read_ready(root: Path) -> dict | None:
    path = root / "data/state/workspace-ready.json"
    if not path.is_file():
        return None
    value = _json(path)
    pid = value.get("pid")
    workspace_id = value.get("workspace_id")
    expected_id = _json(root / "portable.json").get("workspace_id")
    if (not isinstance(pid, int) or pid <= 0 or value.get("ready") is not True
            or workspace_id != expected_id):
        return None
    return value


def _wait_ready(root: Path, timeout: int) -> tuple[dict, object, float]:
    expected_exe = _safe_file(root / "app" / DESKTOP_EXE, root, "VoiceTerminalDesktop.exe（桌面程序）")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = _read_ready(root)
        if ready is not None:
            try:
                process, created_at = _process_identity(ready["pid"], expected_exe)
                return ready, process, created_at
            except QualificationError:
                return ready, None, 0.0
        time.sleep(0.2)
    raise QualificationError("测试桌面未在验收时限内写入 workspace-ready.json（工作区就绪标记）。")


def _current_ready_matches(root: Path, expected: dict) -> bool:
    ready_path = root / "data/state/workspace-ready.json"
    if not ready_path.is_file():
        return False
    current = _json(ready_path)
    return (current.get("ready") is True
            and current.get("pid") == expected.get("pid")
            and current.get("workspace_id") == expected.get("workspace_id"))


def _remove_stale_ready_marker(root: Path, expected: dict,
                               expected_exe: Path, created_at: float | None) -> None:
    ready_path = root / "data/state/workspace-ready.json"
    if not ready_path.exists():
        return
    if not _current_ready_matches(root, expected):
        raise QualificationError("workspace-ready.json（就绪标记）已改为其他 PID（进程编号），拒绝清理。")
    psutil = _psutil()
    try:
        process = psutil.Process(expected["pid"])
        process_created = float(process.create_time())
        process_exe = Path(process.exe()).resolve(strict=True)
    except psutil.NoSuchProcess:
        if psutil.pid_exists(expected["pid"]) or not _current_ready_matches(root, expected):
            raise QualificationError("PID（进程编号）已复用或就绪记录已变化，拒绝清理标记。")
        ready_path.unlink(missing_ok=True)
        return
    except (psutil.Error, OSError) as exc:
        raise QualificationError("无法确认就绪标记对应的原进程是否已退出。") from exc
    if (created_at is None
            or abs(process_created - created_at) > 1.0
            or os.path.normcase(str(process_exe)) != os.path.normcase(str(expected_exe.resolve(strict=True)))):
        raise QualificationError("PID（进程编号）已被复用或执行文件变化，拒绝清理就绪标记。")
    raise QualificationError("原桌面进程仍在运行，拒绝清理 workspace-ready.json（就绪标记）。")


def _force_stop_verified_desktop(expected_exe: Path, pid: int,
                                 created_at: float, timeout: int) -> None:
    psutil = _psutil()
    current, current_created = _process_identity(pid, expected_exe)
    if abs(current_created - created_at) > 1.0 or current.pid != pid:
        raise QualificationError("ready PID（就绪进程编号）身份变化，拒绝强制结束。")
    try:
        current.terminate()
        current.wait(timeout=min(timeout, 10))
        return
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired:
        pass
    current_again, created_again = _process_identity(pid, expected_exe)
    if abs(created_again - created_at) > 1.0 or current_again.pid != pid:
        raise QualificationError("桌面 PID（进程编号）已复用，拒绝强制结束。")
    try:
        current_again.kill()
        current_again.wait(timeout=10)
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        raise QualificationError("失败清理时无法结束已核实的测试桌面。") from exc


def _stop_ready_desktop(root: Path, ready: dict, process: object,
                        created_at: float, config: Path, timeout: int = 30,
                        *, failure_cleanup: bool = False) -> dict:
    if process is None:
        raise QualificationError("没有可安全验证的 workspace-ready PID（就绪进程编号），拒绝停止未知进程。")
    psutil = _psutil()
    expected_exe = _safe_file(root / "app" / DESKTOP_EXE, root, "VoiceTerminalDesktop.exe（桌面程序）")
    qualification_config = _safe_file(config, TEST_ROOT, "qualification-config（验收配置）")
    ready_path = root / "data/state/workspace-ready.json"
    if ready_path.exists() and not _current_ready_matches(root, ready):
        raise QualificationError("workspace-ready.json（就绪标记）已改为其他 PID（进程编号），拒绝停止。")
    shutdown_requested = False
    forced_cleanup = False
    try:
        current, current_created = _process_identity(ready["pid"], expected_exe)
        if abs(current_created - created_at) > 1.0 or current.pid != ready["pid"]:
            raise QualificationError("ready PID（就绪进程编号）创建时间发生变化，拒绝停止。")
        # --qualification-exit（验收专用退出参数）由单实例桌面路由到 Desktop.shutdown（桌面退出流程）。
        request = [str(expected_exe), "--qualification-config", str(qualification_config),
                   "--qualification-exit"]
        try:
            request_code, _ = _run_short(request, expected_exe, timeout=min(timeout, 30))
            if request_code != 0:
                raise QualificationError("验收专用 Desktop.shutdown（桌面优雅退出）请求失败。")
            shutdown_requested = True
        except QualificationError:
            if not failure_cleanup:
                raise
            _force_stop_verified_desktop(expected_exe, ready["pid"], created_at, timeout)
            forced_cleanup = True
        else:
            try:
                current.wait(timeout=timeout)
            except psutil.TimeoutExpired as exc:
                if not failure_cleanup:
                    raise QualificationError("Desktop.shutdown（桌面优雅退出）请求后原进程仍未退出。") from exc
                _force_stop_verified_desktop(expected_exe, ready["pid"], created_at, timeout)
                forced_cleanup = True
    except psutil.NoSuchProcess:
        pass
    except psutil.Error as exc:
        raise QualificationError("无法安全停止本轮已就绪的测试桌面。") from exc
    deadline = time.monotonic() + timeout
    while ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if ready_path.exists():
        if not failure_cleanup:
            raise QualificationError("桌面退出后 workspace-ready.json（就绪标记）未清除。")
        _remove_stale_ready_marker(root, ready, expected_exe, created_at)
    if ready_path.exists():
        raise QualificationError("测试桌面退出后 workspace-ready.json（工作区就绪标记）未清除。")
    return {"shutdown_requested": shutdown_requested,
            "forced_failure_cleanup": forced_cleanup,
            "ready_marker_removed": not ready_path.exists()}


def _assert_inactive(root: Path) -> None:
    if (root / "data/state/workspace-ready.json").exists():
        raise QualificationError("源工作区已有 workspace-ready.json（桌面就绪标记）；拒绝迁移。")
    instance = root / "data/state/application/instance.json"
    if not instance.is_file():
        return
    value = _json(instance)
    pid = value.get("pid")
    created_at = value.get("created_at")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(created_at, (int, float)):
        raise QualificationError("源工作区的进程记录不可验证；拒绝迁移。")
    psutil = _psutil()
    try:
        process = psutil.Process(pid)
        if abs(float(process.create_time()) - float(created_at)) <= 2.0:
            raise QualificationError("源工作区仍有活跃 Voice Terminal（语音终端）实例；拒绝迁移。")
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise QualificationError("无法确认源工作区实例是否已退出。") from exc


def _assert_no_pending_workspace_task(source: Path, workspace_id: str) -> None:
    """Terminal journals（终态事务）可保留，未完成事务必须先恢复。"""
    for relative in ("tmp/workspace-task.json", "data/state/workspace-task.json"):
        marker = source / relative
        if not marker.exists():
            continue
        reference = _json(_safe_file(marker, source, "工作区事务记录"))
        target_text = reference.get("target_root")
        source_text = reference.get("source_root")
        if not isinstance(target_text, str) or not isinstance(source_text, str):
            raise QualificationError("已有工作区事务缺少来源或目标路径。")
        target = _bounded(Path(target_text), "已有事务的目标工作区", must_exist=True)
        previous = _bounded(Path(source_text), "已有事务的原工作区")
        journal = _json(_safe_file(target / "tmp/workspace-task.json", target, "已有事务的目标记录"))
        if (source not in (previous, target) or reference.get("schema") != 1
                or journal.get("schema") != 1 or journal.get("workspace_id") != workspace_id
                or any(reference.get(key) != journal.get(key)
                       for key in ("source_root", "target_root", "workspace_id", "kind"))):
            raise QualificationError("已有事务与当前工作区身份不符，拒绝覆盖。")
        if journal.get("state") not in ("completed", "failed"):
            raise QualificationError("源工作区已有未完成或待恢复事务；拒绝覆盖。")


def _preflight(source: Path) -> tuple[Path, Path]:
    if _reparse(source) or not source.is_dir():
        raise QualificationError("source workspace（源工作区）必须是 D 盘测试区中的普通目录。")
    portable = _json(source / "portable.json")
    if portable.get("schema") != 2 or portable.get("app") != "app" or portable.get("data_dir") != "data":
        raise QualificationError("源工作区不符合当前第二版 portable.json（可迁移布局清单）。")
    app_release = _json(source / "app/release.json")
    if app_release.get("channel") != "distribution":
        raise QualificationError("源工作区不是分发渠道安装。")
    launcher = _safe_file(source / LAUNCHER_EXE, source, "VoiceTerminal.exe（受管启动入口）")
    required = (
        source / "app" / DESKTOP_EXE,
        source / "app/release.json",
        source / "runtimes/python/python.exe",
        source / "runtimes/python/manifest.json",
        source / "runtimes/webview2/msedgewebview2.exe",
        source / "runtimes/webview2/manifest.json",
    )
    for path in required:
        _safe_file(path, source, path.name)
    _assert_no_pending_workspace_task(source, str(portable.get("workspace_id", "")))
    _assert_inactive(source)
    return launcher, source / "app"


def _make_qualification_config(run_root: Path, source: Path) -> Path:
    locator = run_root / "locator" / "workspace.json"
    config = run_root / "qualification.json"
    value = {
        "registry": str(locator),
        "instance_tag": f"workspace-qualification-{uuid.uuid4().hex[:12]}",
        "source_base": "http://127.0.0.1:1",
        "workspace_root": str(source),
        "headless": True,
        "fields": {
            "registry": "独立于系统默认定位记录的测试工作区记录路径",
            "instance_tag": "隔离验收实例编号",
            "source_base": "失败关闭的回环发布源；已完整安装时不会请求下载",
            "workspace_root": "仅用于本轮验收的旧工作区绝对路径",
            "headless": "原生准备与迁移助手是否使用无界面模式",
        },
    }
    _write_json(config, value)
    return config


def _seed_path_and_session(source: Path, run_root: Path, nonce: str) -> dict:
    history = _safe_directory(source / "data/history", source, "data/history（会话历史目录）")
    session = history / f"qualification-session-{nonce}.txt"
    text = (
        f"本机迁移验收会话文本。编号：{nonce}\n"
        "只用于检查普通数据在工作区迁移后仍保留。\n"
        f"讨论中的旧路径示例（普通文本，须原样保留）：{history}\n"
    )
    session.write_text(text, encoding="utf-8")

    local_yaml = source / "data/config/local.yaml"
    _safe_directory(local_yaml.parent, source, "data/config（配置目录）")
    try:
        import yaml
    except ImportError as exc:
        raise QualificationError("验收 Python（解释器）缺少 PyYAML（YAML 配置解析库）。") from exc
    if local_yaml.exists():
        try:
            settings = yaml.safe_load(local_yaml.read_text(encoding="utf-8-sig")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise QualificationError("测试 local.yaml（本机配置）格式无效；未继续修改。") from exc
    else:
        settings = {}
    if not isinstance(settings, dict):
        raise QualificationError("测试 local.yaml（本机配置）顶层必须是对象。")
    shadow = settings.setdefault("shadow_agent", {})
    if not isinstance(shadow, dict):
        raise QualificationError("测试 local.yaml 的 shadow_agent（影子代理）字段格式无效。")
    tools = shadow.setdefault("tools", {})
    if not isinstance(tools, dict):
        raise QualificationError("测试 local.yaml 的 tools（工具）字段格式无效。")
    roots = tools.get("allowed_roots", ["docs"])
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        raise QualificationError("测试 local.yaml 的 allowed_roots（文件工具允许目录）格式无效。")
    probe = str(history)
    if probe not in roots:
        roots.append(probe)
    tools["allowed_roots"] = roots
    local_yaml.write_text(yaml.safe_dump(settings, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"session_path": session, "session_sha256": _sha256(session),
            "local_yaml_path": local_yaml, "local_yaml_sha256": _sha256(local_yaml),
            "path_probe_source": probe, "path_probe_relative": "data/history"}


def _snapshot_source(source: Path, locator: Path,
                     probes: dict | list[dict]) -> dict[str, str]:
    if isinstance(probes, dict):
        probes = [probes]
    files = {
        "portable_sha256": source / "portable.json",
        "release_sha256": source / "app/release.json",
        "local_yaml_sha256": source / "data/config/local.yaml",
        "locator_sha256": locator,
    }
    for index, probe in enumerate(probes):
        files[f"session_{index}_sha256"] = source / "data/history" / Path(probe["session_path"]).name
    if not locator.is_file():
        raise QualificationError("原生准备后缺少 D 盘专用 locator（工作区定位记录）。")
    return {key: _sha256(path) for key, path in files.items()}


def _verify_snapshot(source: Path, locator: Path, probe: dict | list[dict],
                     snapshot: dict[str, str]) -> bool:
    try:
        actual = _snapshot_source(source, locator, probe)
    except (OSError, QualificationError):
        return False
    return actual == snapshot


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        left_path = Path(left).resolve(strict=False)
        right_path = Path(right).resolve(strict=False)
    except (OSError, ValueError):
        return False
    return os.path.normcase(os.path.normpath(str(left_path))) == os.path.normcase(os.path.normpath(str(right_path)))


def _verify_migrated(source: Path, target: Path, locator: Path,
                     probes: list[dict], timeout: int) -> tuple[dict, object, float]:
    ready, process, created_at = _wait_ready(target, timeout)
    if process is None or created_at <= 0:
        raise QualificationError("目标桌面就绪标记的 PID（进程编号）与执行文件身份无法核实。")
    target_id = _json(target / "portable.json").get("workspace_id")
    current_locator = _json(locator)
    if (not isinstance(current_locator.get("workspace_root"), str)
            or not _same_path(current_locator["workspace_root"], target)
            or current_locator.get("workspace_id") != target_id):
        raise QualificationError("迁移目标已就绪，但独立定位记录没有切换到新工作区。")
    for probe in probes:
        migrated_session = target / "data/history" / probe["session_path"].name
        if not migrated_session.is_file() or _sha256(migrated_session) != probe["session_sha256"]:
            raise QualificationError("普通会话文本没有原样保留到迁移后的工作区。")
    try:
        import yaml
        migrated_settings = yaml.safe_load((target / "data/config/local.yaml").read_text(encoding="utf-8"))
    except (ImportError, OSError, UnicodeError, ValueError) as exc:
        raise QualificationError("迁移后无法读取 local.yaml（本机配置）路径探针。") from exc
    if not isinstance(migrated_settings, dict):
        raise QualificationError("迁移后的 local.yaml（本机配置）顶层格式无效。")
    allowed_roots = migrated_settings.get("shadow_agent", {}).get("tools", {}).get("allowed_roots", [])
    expected_path = str(target / "data/history")
    if not isinstance(allowed_roots, list) or expected_path not in allowed_roots:
        raise QualificationError("已知配置路径没有重写到目标工作区。")
    if any(probe["path_probe_source"] in allowed_roots for probe in probes):
        raise QualificationError("迁移后的本机配置仍保留旧工作区路径。")
    managed_items = ("app", "runtimes", "models", "data", "cache", "tmp",
                     "backups", "releases", "portable.json", "VoiceTerminal.exe")
    managed_removed = not any((source / relative).exists() for relative in managed_items)
    if not managed_removed:
        raise QualificationError("迁移提交后，旧工作区的受管组件未按预期清理。")
    return ready, process, created_at


def _create_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        raise QualificationError("未受管 Windows Junction（目录联接）验收只支持 Windows。")
    if link.exists() or _reparse(link):
        raise QualificationError("本轮要创建的目录联接路径已存在。")
    target.mkdir(parents=True, exist_ok=False)
    (target / "sentinel.txt").write_text("只属于迁移拒绝验收的外部目标。\n", encoding="utf-8")
    script = (
        "$ErrorActionPreference='Stop';"
        "$link=[WildcardPattern]::Escape($env:VQ_LINK);"
        "$target=[WildcardPattern]::Escape($env:VQ_TARGET);"
        "New-Item -ItemType Junction -Path $link -Target $target | Out-Null"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    env = os.environ.copy()
    env["VQ_LINK"] = str(link)
    env["VQ_TARGET"] = str(target)
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        raise QualificationError("测试 Windows 环境缺少 PowerShell，无法创建真实目录联接。")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    if result.returncode != 0 or not link.is_junction() or link.resolve() != target.resolve():
        raise QualificationError("未能创建指向 D 盘测试目录的 Junction（目录联接）。")


def _remove_own_junction(link: Path, target: Path) -> None:
    if not link.exists() and not _reparse(link):
        return
    if not link.is_junction() or link.resolve(strict=True) != target.resolve(strict=True):
        raise QualificationError("拒绝移除目标不匹配的目录联接。")
    # os.rmdir（移除联接本身）不会递归删除联接目标目录。
    os.rmdir(link)
    sentinel = target / "sentinel.txt"
    if not sentinel.is_file():
        raise QualificationError("移除目录联接后，外部测试哨兵文件缺失。")


def _wait_and_stop(root: Path, timeout: int, config: Path) -> dict:
    ready, process, created_at = _wait_ready(root, timeout)
    pid = ready["pid"]
    result = _stop_ready_desktop(root, ready, process, created_at, config, timeout)
    return {"pid": pid, "workspace_id": ready["workspace_id"],
            "stopped_by_exact_ready_identity": True, **result}


def _stop_ready_if_present(root: Path, timeout: int, config: Path) -> bool:
    if not root.is_dir():
        return False
    ready_path = root / "data/state/workspace-ready.json"
    if not ready_path.exists():
        return False
    ready = _read_ready(root)
    if ready is None:
        raise QualificationError("存在无法验证的工作区就绪记录；拒绝停止未知进程。")
    expected_exe = _safe_file(root / "app" / DESKTOP_EXE, root, "VoiceTerminalDesktop.exe（桌面程序）")
    try:
        process, created_at = _process_identity(ready["pid"], expected_exe)
    except QualificationError:
        psutil = _psutil()
        if psutil.pid_exists(ready["pid"]):
            raise
        _remove_stale_ready_marker(root, ready, expected_exe, None)
        return True
    _stop_ready_desktop(root, ready, process, created_at, config, timeout,
                         failure_cleanup=True)
    return True


def _journal_snapshot(journal: Path) -> dict:
    record = _json(journal)
    return {key: record.get(key) for key in (
        "state", "kind", "source_root", "target_root", "helper_pid",
        "helper_create_time", "helper_executable", "target_pid", "cleanup_finished",
    )}


def _validate_source_inputs(source: Path) -> None:
    if not source.is_absolute():
        raise QualificationError("source-workspace（源工作区）必须是绝对路径。")
    _bounded(source, "source-workspace（源工作区）", must_exist=True)
    if source.name == ".local":
        raise QualificationError("不能把源码仓库 .local（本机资源目录）作为测试工作区。")


def _run(args: argparse.Namespace) -> tuple[dict, int]:
    if os.name != "nt":
        raise QualificationError("工作区事务验收依赖 Windows 桌面和目录联接。")
    if args.copy_timeout_seconds <= 0 or args.ready_timeout_seconds <= 0:
        raise QualificationError("超时秒数必须是正整数。")
    python_exe = _bounded(Path(sys.executable), "qualification Python（验收解释器）", must_exist=True)
    if not python_exe.is_file():
        raise QualificationError("验收 Python（解释器）必须是 D 盘测试目录中的普通文件。")
    _validate_source_inputs(args.source_workspace)
    source = args.source_workspace.resolve(strict=True)
    launcher, program = _preflight(source)
    if not program.is_relative_to(TEST_ROOT):
        raise QualificationError("app（程序目录）必须位于 D:/项目开发/测试 子目录内。")
    if args.run_root:
        run_root = _bounded(args.run_root, "run-root（本轮验收目录）")
    else:
        run_root = _bounded(TEST_ROOT / f"工作区迁移验收 中文和空格 {uuid.uuid4().hex[:12]}",
                            "run-root（本轮验收目录）")
    if run_root == source or run_root.is_relative_to(source) or source.is_relative_to(run_root):
        raise QualificationError("run-root（验收目录）必须与源工作区互不包含。")
    if run_root.exists():
        raise QualificationError("run-root（本轮验收目录）必须是新建目录；不会覆盖已有验收数据。")
    run_root.mkdir(parents=True, exist_ok=False)
    config = _make_qualification_config(run_root, source)
    locator = run_root / "locator/workspace.json"
    outcomes: list[dict] = []
    pending: list[dict] = [{"test_id": "official_dsh", "status": "pending",
                            "reason": "工作区事务脚本不解包固定官方 DSH；缺少官方样本时保持待验收。"}]
    cleanup_junction: tuple[Path, Path] | None = None
    test_workspace_roots: list[Path] = [source]
    test_workspace_configs: dict[Path, Path] = {source: config}
    current_case = "unmanaged_junction_rejected"
    exit_code = 0

    def passed(test_id: str, details: dict) -> None:
        outcomes.append({"test_id": test_id, "status": "passed", "details": details})
        print(json.dumps({"event": "workspace_case", "test_id": test_id, "status": "passed",
                          "fields": {"event": "工作区验收步骤结果", "test_id": "验收步骤编号",
                                     "status": "本步骤结果"}}, ensure_ascii=False), flush=True)

    try:
        nonce = uuid.uuid4().hex[:12]
        probe = _seed_path_and_session(source, run_root, nonce)

        # First prove the collector refuses an unmanaged junction before cleanup or locator handoff.
        failure_target = run_root / "junction 拒绝目标"
        test_workspace_roots.append(failure_target)
        test_workspace_configs[failure_target] = config
        failure_journal = _prepare_task(launcher, config, source, failure_target, "migrate")
        failure_helper = _safe_file(failure_target / "tmp/workspace-helper.exe", failure_target,
                                    "workspace-helper.exe（受测事务辅助程序）")
        junction = source / "data/history" / UNMANAGED_LINK_NAME
        outside = run_root / "junction 外部目标"
        cleanup_junction = (junction, outside)
        _create_junction(junction, outside)
        failure_snapshot = _snapshot_source(source, locator, probe)
        failure_run = _run_task(failure_helper, config, failure_journal,
                                timeout=min(args.copy_timeout_seconds, 1800))
        failed_journal = _journal_snapshot(failure_journal)
        if (failure_run["return_code"] == 0 or failed_journal["state"] != "failed"
                or not failure_run["unmanaged_link_error"]):
            raise QualificationError("含未受管目录联接的迁移未在复制收集阶段按预期拒绝。")
        rollback_stopped = _wait_and_stop(source, args.ready_timeout_seconds, config)
        if not _verify_snapshot(source, locator, probe, failure_snapshot):
            raise QualificationError("目录联接拒绝后，源工作区数据或独立定位记录发生变化。")
        _remove_own_junction(junction, outside)
        cleanup_junction = None
        passed("unmanaged_junction_rejected", {
            "failed_before_commit": True,
            "source_data_preserved": True,
            "locator_preserved": True,
            "rollback_desktop": rollback_stopped,
            "external_sentinel_preserved": True,
            "elapsed_seconds": failure_run["elapsed_seconds"],
        })

        # Complete one clean migrate transaction and verify its normal commit and cleanup.
        _assert_inactive(source)
        current_case = "clean_migration_committed"
        success_target = run_root / "干净迁移目标 中文路径"
        test_workspace_roots.append(success_target)
        test_workspace_configs[success_target] = config
        success_journal = _prepare_task(launcher, config, source, success_target, "migrate")
        success_helper = _safe_file(success_target / "tmp/workspace-helper.exe", success_target,
                                    "workspace-helper.exe（受测事务辅助程序）")
        success_run = _run_task(success_helper, config, success_journal,
                                timeout=args.copy_timeout_seconds)
        success_record = _journal_snapshot(success_journal)
        if success_run["return_code"] != 0 or success_record["state"] != "completed":
            raise QualificationError("干净工作区迁移未能完成事务提交。")
        _verify_migrated(source, success_target, locator, [probe], args.ready_timeout_seconds)
        success_stop = _wait_and_stop(success_target, args.ready_timeout_seconds, config)
        passed("clean_migration_committed", {
            "journal_state": success_record["state"],
            "workspace_ready": True,
            "locator_switched": True,
            "known_path_rewritten": True,
            "ordinary_session_text_preserved": True,
            "old_managed_workspace_cleaned": True,
            "desktop_stopped_by_exact_ready_identity": success_stop,
            "progress_phases": success_run["phases"],
        })

        # Make a second isolated locator, interrupt a real copy, then recover through normal startup.
        _assert_inactive(success_target)
        recovery_run_root = run_root / "恢复验收 中文和空格"
        recovery_run_root.mkdir(parents=True, exist_ok=False)
        recovery_config = _make_qualification_config(recovery_run_root, success_target)
        test_workspace_configs[success_target] = recovery_config
        recovery_locator = recovery_run_root / "locator/workspace.json"
        recovery_nonce = uuid.uuid4().hex[:12]
        recovery_probe = _seed_path_and_session(success_target, recovery_run_root, recovery_nonce)
        probes = [probe, recovery_probe]

        current_case = "copy_interrupted_safely"
        recovery_target = run_root / "复制恢复目标 中文路径"
        test_workspace_roots.append(recovery_target)
        test_workspace_configs[recovery_target] = recovery_config
        recovery_launcher = _safe_file(success_target / LAUNCHER_EXE, success_target,
                                       "VoiceTerminal.exe（受管启动入口）")
        recovery_journal = _prepare_task(recovery_launcher, recovery_config, success_target,
                                         recovery_target, "migrate")
        recovery_helper = _safe_file(recovery_target / "tmp/workspace-helper.exe", recovery_target,
                                     "workspace-helper.exe（受测事务辅助程序）")
        recovery_before = _snapshot_source(success_target, recovery_locator, probes)
        interrupted_run = _run_task(recovery_helper, recovery_config, recovery_journal,
                                    timeout=args.copy_timeout_seconds, interrupt_copy=True)
        interrupted_journal = _journal_snapshot(recovery_journal)
        if not interrupted_run["interrupted_during_copy"]:
            raise QualificationError("没有在原生复制阶段中断受测 helper（辅助进程）。")
        if interrupted_journal["state"] != "copying" or interrupted_journal["target_pid"] not in (None, 0):
            raise QualificationError("中断点不在复制阶段，或目标桌面已提前启动。")
        if not _verify_snapshot(success_target, recovery_locator, probes, recovery_before):
            raise QualificationError("中断复制后，源工作区数据或独立定位记录发生变化。")
        passed("copy_interrupted_safely", {
            "interrupted_during_copy": True,
            "source_data_preserved": True,
            "locator_preserved": True,
            "journal_state": interrupted_journal["state"],
            "progress_phases": interrupted_run["phases"],
        })

        # Invoke a normal thin startup (无额外事务参数) so native pending_task recovery is exercised.
        current_case = "resumed_migration_committed"
        # 使用移到工作区之外的单独入口，验证旧终态记录不会遮挡下一次待恢复事务，
        # 也避免入口自身的 Windows 文件句柄占用待清理的旧目录。
        resumed_entry_dir = recovery_run_root / "移动后的单 EXE 入口"
        resumed_entry_dir.mkdir(parents=True, exist_ok=False)
        resumed_entry = resumed_entry_dir / LAUNCHER_EXE
        shutil.copyfile(recovery_launcher, resumed_entry)
        _safe_file(resumed_entry, TEST_ROOT, "VoiceTerminal.exe（移出的恢复入口）")
        resumed = _run_task(resumed_entry, recovery_config, recovery_journal,
                            timeout=args.copy_timeout_seconds, normal_startup=True)
        final_journal = _journal_snapshot(recovery_journal)
        if resumed["return_code"] != 0 or final_journal["state"] != "completed":
            raise QualificationError("正常启动未能恢复并提交中断的工作区迁移。")
        _verify_migrated(success_target, recovery_target, recovery_locator,
                         probes, args.ready_timeout_seconds)
        stop_info = _wait_and_stop(recovery_target, args.ready_timeout_seconds, recovery_config)
        passed("resumed_migration_committed", {
            "resumed_by_normal_startup": True,
            "resume_entry_outside_workspaces": True,
            "journal_state": final_journal["state"],
            "workspace_ready": True,
            "locator_switched": True,
            "known_path_rewritten": True,
            "ordinary_session_text_preserved": True,
            "old_managed_workspace_cleaned": True,
            "desktop_stopped_by_exact_ready_identity": stop_info,
            "progress_phases": resumed["phases"],
        })
    except QualificationError as exc:
        exit_code = 1
        outcomes.append({"test_id": current_case, "status": "failed", "error": str(exc)})
    except Exception as exc:
        exit_code = 1
        outcomes.append({"test_id": current_case, "status": "failed",
                         "error": "验收步骤异常；未输出路径、配置正文或原生输出。",
                         "error_type": type(exc).__name__})
    finally:
        if cleanup_junction is not None:
            try:
                _remove_own_junction(*cleanup_junction)
            except Exception:
                exit_code = 1
                outcomes.append({"test_id": "test_cleanup", "status": "failed",
                                 "error": "无法安全清理本轮创建的目录联接。"})
        for candidate_root in test_workspace_roots:
            try:
                _stop_ready_if_present(candidate_root, args.ready_timeout_seconds,
                                       test_workspace_configs[candidate_root])
            except Exception:
                exit_code = 1
                outcomes.append({"test_id": "test_cleanup", "status": "failed",
                                 "error": "未能按 ready PID（就绪进程编号）、执行文件和创建时间回收受测桌面。"})

    official_status = {"status": "pending",
                       "reason": "本脚本只验收工作区事务；固定官方 DSH 安装器样本缺失时继续待验收。"}
    report = _report(run_root, source, program, python_exe, config, locator,
                     outcomes, pending, official_status)
    _write_json(run_root / "workspace-qualification.json", report)
    return report, exit_code


def _report(run_root: Path, source: Path, program: Path, python_exe: Path, config: Path,
            locator: Path, outcomes: list[dict], pending: list[dict],
            official_dsh: dict) -> dict:
    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "test_root": str(TEST_ROOT),
        "run_root": str(run_root),
        "source_workspace": str(source),
        "program_root": str(program),
        "qualification_python": str(python_exe),
        "qualification_config": str(config),
        "locator_path": str(locator),
        "summary": {"passed": sum(item["status"] == "passed" for item in outcomes),
                    "failed": sum(item["status"] == "failed" for item in outcomes),
                    "pending_count": len(pending)},
        "test_cases": outcomes,
        "pending": pending,
        "official_dsh": official_dsh,
        "fields": {
            "schema_version": "验收报告格式版本",
            "created_at_utc": "报告生成时间（UTC）",
            "test_root": "固定 D 盘验收根目录",
            "run_root": "本轮独立测试数据目录",
            "source_workspace": "只用于本轮验收的旧工作区",
            "program_root": "测试工作区内的 app 程序目录",
            "qualification_python": "位于 D 盘测试目录内的验收 Python（解释器）",
            "qualification_config": "仅本轮使用的无界面原生验收配置",
            "locator_path": "位于测试目录内的独立工作区定位记录",
            "summary": "通过、失败和待验收步骤计数",
            "passed": "通过步骤数",
            "failed": "失败步骤数",
            "pending_count": "待验收步骤数",
            "pending": "尚未运行的验收步骤明细",
            "test_cases": "迁移场景及结果",
            "test_id": "固定验收步骤编号",
            "status": "步骤状态：passed（通过）、failed（失败）或 pending（待验收）",
            "details": "不含凭据的结构化检查结果",
            "error": "不含路径正文或原生命令输出的失败摘要",
            "error_type": "异常类型名称，不含异常正文",
            "official_dsh": "固定官方 DSH（代理执行宿主）样本状态",
            "reason": "待验收原因说明",
            "unmanaged_junction_rejected": "未受管目录联接被安全拒绝的场景",
            "clean_migration_committed": "完整迁移提交、数据改写和旧工作区清理的场景",
            "copy_interrupted_safely": "复制中断后源工作区仍保留的场景",
            "resumed_migration_committed": "正常启动恢复并提交迁移的场景",
            "failed_before_commit": "失败是否发生在提交之前",
            "source_data_preserved": "失败或中断后源工作区测试数据是否保持一致",
            "locator_preserved": "失败或中断后独立定位记录是否保持一致",
            "rollback_desktop": "回滚后受测桌面的进程标识与停止结果",
            "external_sentinel_preserved": "目录联接外部测试哨兵是否保留",
            "elapsed_seconds": "场景耗时秒数",
            "interrupted_during_copy": "helper（辅助进程）是否在文件复制阶段被定向终止",
            "journal_state": "事务记录中的最终或中断阶段",
            "progress_phases": "原生标准输出中汇总的迁移阶段进度",
            "resumed_by_normal_startup": "是否通过正常无额外事务参数的启动恢复",
            "resume_entry_outside_workspaces": "是否从移到来源和目标工作区之外的单独入口恢复",
            "workspace_ready": "目标桌面是否写入就绪标记",
            "locator_switched": "独立定位记录是否指向新工作区",
            "known_path_rewritten": "local.yaml（本机配置）内已知路径是否改写",
            "ordinary_session_text_preserved": "普通会话文本是否完整保留",
            "old_managed_workspace_cleaned": "旧工作区受管组件是否清理",
            "desktop_stopped_by_exact_ready_identity": "是否按 ready PID（就绪进程编号）、执行文件和创建时间停止桌面",
            "pid": "Windows PID（进程编号）",
            "workspace_id": "工作区稳定编号",
            "stopped_by_exact_ready_identity": "是否通过已核实的就绪进程身份停止",
            "shutdown_requested": "是否调用验收专用参数请求桌面执行正常退出流程",
            "forced_failure_cleanup": "是否仅在失败清理阶段强制结束已核实的原进程",
            "ready_marker_removed": "是否确认原 PID（进程编号）的就绪标记已移除",
            "phase": "原生事务阶段名称",
            "last_completed_bytes": "阶段最新完成字节数",
            "total_bytes": "阶段总字节数",
            "progress_events": "阶段进度事件数量",
            "test_cleanup": "仅清理本轮创建的目录联接并停止已核实受测桌面的步骤",
        },
    }


def main() -> int:
    args = _parse_args()
    try:
        report, exit_code = _run(args)
        report_path = Path(report["run_root"]) / "workspace-qualification.json"
        print(json.dumps({"ok": exit_code == 0, "report": str(report_path),
                          "summary": report["summary"],
                          "fields": {"ok": "本次工作区事务验收是否成功",
                                     "report": "结构化验收报告保存路径",
                                     "summary": "通过、失败和待验收步骤计数"}}, ensure_ascii=False))
        return exit_code
    except QualificationError as exc:
        print(json.dumps({"ok": False, "message": str(exc),
                          "fields": {"ok": "本次工作区事务验收是否成功",
                                     "message": "不包含原生命令原始输出的安全摘要"}}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "message": "工作区事务验收异常；未输出详细诊断或原生命令内容。",
                          "error_type": type(exc).__name__,
                          "fields": {"ok": "本次工作区事务验收是否成功",
                                     "message": "不包含原生命令原始输出的安全摘要",
                                     "error_type": "异常类型名称，不包含异常正文"}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

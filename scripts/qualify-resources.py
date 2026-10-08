#!/usr/bin/env python3
"""Qualify offline resource install flows（验收离线资源安装流程）.

This utility is intentionally separate from application startup. It only runs
when invoked explicitly and confines generated fixtures, imports, simulated
transactions, and reports to the D: qualification workspace.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
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
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_ROOT = Path("D:/项目开发/测试/资源验收")
RESOURCE_IDS = (
    "kws-sherpa-onnx",
    "paraformer-streaming",
    "fsmn-vad",
    "wespeaker-cnceleb-resnet34",
    "smart-turn",
    "sensevoice-small",
    "dsh",
)
OFFICIAL_DSH_FILENAME = "deepseek-harness-0.2.0-rc.2-win-x64.exe"


class QualificationError(RuntimeError):
    """Safe, user-facing qualification failure（不包含敏感内容的验收错误）."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线资源导入与事务验收工具")
    parser.add_argument("--test-root", type=Path, default=DEFAULT_TEST_ROOT,
                        help="D:/项目开发/测试 下的独立测试目录；默认 D:/项目开发/测试/资源验收")
    parser.add_argument("--workspace", type=Path, required=True,
                        help="已链接到测试目录的工作区根路径")
    parser.add_argument("--program", type=Path, required=True,
                        help="本轮分发产物的 app 程序目录")
    parser.add_argument("--python", type=Path, required=True,
                        help="本轮分发产物的 Python 解释器路径")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT,
                        help="只读离线资源来源仓库；默认当前源码仓库")
    parser.add_argument("--bundle", type=Path,
                        help="预先生成的 ZIP64 离线资源包；默认仓库 .local/releases/fixtures")
    parser.add_argument("--smart-turn-source", type=Path,
                        help="只读 SmartTurn（语音结束判断模型）目录；默认仓库 .local/models")
    parser.add_argument("--official-installer", type=Path,
                        help="可选的固定官方 DSH 安装器；只接受测试目录中已有文件，不下载")
    parser.add_argument("--report", type=Path,
                        help="JSON（结构化摘要）输出路径，默认测试根/resource-qualification.json")
    parser.add_argument("--startup-timeout-seconds", type=int, default=900,
                        help="官方 DSH 全应用启动最长等待秒数")
    return parser


def _inside(path: Path, parent: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(parent.resolve(strict=False))
    except (OSError, ValueError):
        return False


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
        return True
    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _reject_reparse_chain(path: Path, label: str) -> None:
    cursor = Path(os.path.abspath(path.expanduser()))
    while True:
        if _is_reparse(cursor):
            raise QualificationError(f"{label} 路径不能经过符号链接、目录联接或其他重解析点。")
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent


def _require_inside(path: Path, parent: Path, label: str) -> Path:
    _reject_reparse_chain(path, label)
    resolved = path.expanduser().resolve(strict=False)
    if not _inside(resolved, parent):
        raise QualificationError(f"{label} 必须位于测试专用目录内。")
    return resolved


def _test_root(path: Path) -> Path:
    fixed_parent = Path("D:/项目开发/测试").resolve(strict=False)
    resolved = _require_inside(path, fixed_parent, "test-root（测试根目录）")
    if resolved == fixed_parent:
        raise QualificationError("test-root（测试根目录）必须是 D:/项目开发/测试 下的独立子目录。")
    return resolved


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError("所需 JSON 文件缺失或格式无效。") from exc
    if not isinstance(value, dict):
        raise QualificationError("所需 JSON 文件顶层必须为对象。")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha512(path: Path) -> bytes:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def _task_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def _safe_tree(source: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise QualificationError("离线资源目录不存在或不是普通目录。")
    for folder, directories, files in os.walk(source, followlinks=False):
        for name in directories + files:
            path = Path(folder) / name
            if path.is_symlink() or path.is_junction():
                raise QualificationError("离线资源目录包含链接，拒绝复制到测试目录。")


def _copy_fixture(source: Path, destination: Path) -> Path:
    if not source.is_file() or source.is_symlink() or source.is_junction():
        raise QualificationError("ZIP64 离线资源包缺失或不是普通文件。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or destination.is_junction() or not destination.is_file():
            raise QualificationError("测试目录中的离线包目标不是普通文件。")
        if _sha256(destination) != _sha256(source):
            raise QualificationError("测试目录已有不同内容的离线包；为保护现有测试数据，未覆盖。")
        return destination
    shutil.copy2(source, destination)
    if _sha256(destination) != _sha256(source):
        raise QualificationError("离线包复制后的 SHA-256（文件摘要）不匹配。")
    return destination


def _base_env(program: Path, workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "VOICE_PROGRAM_ROOT": str(program),
        "VOICE_INSTALL_ROOT": str(workspace),
        "VOICE_LAUNCH_CHANNEL": "distribution",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    })
    # Resource CLIs receive explicit --program/--root values and do not resolve AppPaths.
    env.pop("VOICE_DATA_DIR", None)
    env.pop("VOICE_RESOURCE_POLICY", None)
    env.pop("VOICE_DSH_RUNTIME", None)
    return env


def _invoke_json(python: Path, script: Path, command: str, *, program: Path,
                 workspace: Path, arguments: list[str], timeout: int = 1800) -> dict:
    if not script.is_file():
        raise QualificationError("分发产物缺少资源管理脚本。")
    argv = [str(python), str(script), command, "--root", str(workspace),
            "--program", str(program), *arguments]
    try:
        result = subprocess.run(argv, cwd=program, env=_base_env(program, workspace),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise QualificationError("资源命令超过验收时限。") from exc
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        value = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        value = {}
    if not isinstance(value, dict):
        value = {}
    value["_exit_code"] = result.returncode
    return value


def _check_ok(result: dict, label: str, *, expected: bool = True) -> dict:
    ok = result.get("ok") is True and result.get("_exit_code") == 0
    if ok != expected:
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        code = error.get("code") if isinstance(error.get("code"), str) else "unknown"
        raise QualificationError(f"{label}结果不符合预期（错误代码：{code}）。")
    return result


def _expect_rejection(result: dict, label: str, expected_codes: set[str]) -> None:
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    code = error.get("code") if isinstance(error.get("code"), str) else ""
    if result.get("ok") is not False or result.get("_exit_code") in (None, 0) or code not in expected_codes:
        raise QualificationError(f"{label} 未以预期的安全错误拒绝（错误代码：{code or 'unknown'}）。")


def _status(python: Path, manager: Path, program: Path, workspace: Path) -> dict[str, dict]:
    result = _check_ok(_invoke_json(python, manager, "status", program=program,
        workspace=workspace, arguments=[]), "资源状态读取")
    rows = result.get("result", {}).get("resources")
    if not isinstance(rows, list):
        raise QualificationError("资源状态响应缺少 resources（资源列表）。")
    return {row.get("resource_id"): row for row in rows if isinstance(row, dict)}


def _model_paths(program: Path, workspace: Path, resource_id: str) -> tuple[Path, Path, dict]:
    catalog = _json(program / "config" / "resources" / "catalog.json")
    entry = next((item for item in catalog.get("resources", [])
                  if isinstance(item, dict) and item.get("resource_id") == resource_id), None)
    if not entry or entry.get("kind") != "model":
        raise QualificationError("应用资源目录缺少所需模型条目。")
    portable = _json(workspace / "portable.json")
    models_relative = portable.get("models")
    if not isinstance(models_relative, str):
        raise QualificationError("工作区 portable.json（资源布局清单）缺少 models（模型目录）。")
    return workspace / models_relative, workspace / models_relative / entry["destination"], entry


def _model_snapshot(program: Path, workspace: Path, resource_id: str) -> tuple[str | None, str | None]:
    _, target, entry = _model_paths(program, workspace, resource_id)
    # The model root is portable.models; do not rely on destination depth for this file.
    portable = _json(workspace / "portable.json")
    manifest = workspace / portable["models"] / "manifest.json"
    manifest_digest = _sha256(manifest) if manifest.is_file() else None
    check_file = next((item for item in entry.get("files", []) if not item.get("optional")), None)
    sample = target.joinpath(*check_file["path"].split("/")) if check_file else None
    sample_digest = _sha256(sample) if sample and sample.is_file() else None
    return manifest_digest, sample_digest


def _require_stopped_workspace(workspace: Path) -> None:
    """Avoid modifying resources while a product instance owns this workspace."""
    registry = workspace / "data/state/application/instance.json"
    if not registry.is_file():
        return
    value = _json(registry)
    pid = value.get("pid")
    created_at = value.get("created_at")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(created_at, (int, float)):
        raise QualificationError("测试工作区的实例记录无法验证；为避免修改正在使用的资源，已停止验收。")
    try:
        import psutil
    except ImportError as exc:
        raise QualificationError("测试 Python 缺少 psutil（进程核验库），不能确认工作区占用状态。") from exc
    try:
        process = psutil.Process(pid)
        if abs(float(process.create_time()) - float(created_at)) <= 2:
            raise QualificationError("测试工作区已有运行中的 Voice Terminal；请先退出后再执行离线资源验收。")
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise QualificationError("无法确认测试工作区是否仍由 Voice Terminal 使用。") from exc


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _simulated_recovery(python: Path, manager: Path, program: Path,
                        workspace: Path, run_dir: Path) -> dict:
    scratch = run_dir / "事务恢复模拟 工作区"
    scratch.mkdir(parents=True, exist_ok=False)
    shutil.copy2(workspace / "portable.json", scratch / "portable.json")
    release_dir = scratch / "app"
    release_dir.mkdir()
    release_before = {"schema": 1, "components": [{"kind": "models", "path": "models"}]}
    (release_dir / "release.json").write_text(json.dumps(release_before, ensure_ascii=False), encoding="utf-8")
    portable = _json(scratch / "portable.json")
    destination = "models/analysis/smart-turn"
    catalog = _json(program / "config" / "resources" / "catalog.json")
    smart_turn = next(item for item in catalog["resources"] if item["resource_id"] == "smart-turn")
    destination = f"{portable['models']}/{smart_turn['destination']}"
    tx_rel = "tmp/resource-transactions/qualification-recovery/smart-turn"
    tx_dir = scratch / tx_rel
    backup_rel = f"{tx_rel}/target-backup"
    target = scratch / destination
    backup = scratch / backup_rel
    target.mkdir(parents=True)
    backup.mkdir(parents=True)
    (target / "state.txt").write_text("new", encoding="utf-8")
    (backup / "state.txt").write_text("previous", encoding="utf-8")
    snapshot_rel = f"{tx_rel}/metadata-0.bak"
    (tx_dir / "metadata-0.bak").write_bytes((release_dir / "release.json").read_bytes())
    release_after = {"schema": 1, "components": [{"kind": "models", "path": "models", "build": "interrupted"}]}
    (release_dir / "release.json").write_text(json.dumps(release_after, ensure_ascii=False), encoding="utf-8")
    marker = {
        "schema": 1, "task_id": "qualification-recovery", "resource_id": "smart-turn",
        "transaction_dir": tx_rel, "target": destination, "backup": backup_rel,
        "target_preexisting": True,
        "metadata": [{"path": "app/release.json", "snapshot": snapshot_rel, "existed": True}],
    }
    tmp = scratch / "tmp"
    tmp.mkdir(exist_ok=True)
    (tmp / "resource-transaction.json").write_text(json.dumps(marker), encoding="utf-8")
    _check_ok(_invoke_json(python, manager, "status", program=program, workspace=scratch,
        arguments=[]), "模拟中断事务恢复")
    restored_release = _json(release_dir / "release.json")
    if ((target / "state.txt").read_text(encoding="utf-8") != "previous"
            or restored_release != release_before
            or (tmp / "resource-transaction.json").exists()
            or tx_dir.exists()):
        raise QualificationError("模拟事务恢复未恢复旧组件与发行清单。")
    return {"restored_previous_target": True, "restored_release_manifest": True,
            "transaction_marker_removed": True}


def _bundle_wrong_version(path: Path, entry: dict) -> None:
    index = {
        "schema_version": 1,
        "format": "ZIP64",
        "resources": [{
            "resource_id": "smart-turn",
            "kind": "model",
            "version": "qualification-wrong-version",
            "destination": entry["destination"],
            "origin": "locked_catalog",
            "files": [],
        }],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("resources.json", json.dumps(index, ensure_ascii=False))


def _official_artifact(program: Path) -> dict:
    catalog = _json(program / "config" / "resources" / "catalog.json")
    entry = next((item for item in catalog.get("resources", [])
                  if isinstance(item, dict) and item.get("resource_id") == "dsh"), None)
    artifact = (((entry or {}).get("source") or {}).get("transfer") or {}).get("artifact")
    if not isinstance(artifact, dict):
        raise QualificationError("应用资源目录缺少固定 DSH 官方安装器信息。")
    return artifact


def _find_official_installer(test_root: Path, supplied: Path | None) -> Path | None:
    if supplied:
        path = _require_inside(supplied, test_root, "官方安装器")
        if not path.is_file() or path.name != OFFICIAL_DSH_FILENAME:
            raise QualificationError("指定的官方 DSH 安装器文件名或文件类型不符合固定锁。")
        return path
    matches = [path for path in test_root.rglob(OFFICIAL_DSH_FILENAME)
               if path.is_file() and not path.is_symlink() and not path.is_junction()]
    if len(matches) > 1:
        raise QualificationError("测试目录找到多个固定官方 DSH 安装器，无法唯一选择。")
    return matches[0] if matches else None


def _install_official_dsh(python: Path, script: Path, program: Path,
                          workspace: Path, installer: Path, task_id: str) -> dict:
    artifact = _official_artifact(program)
    expected_size = artifact.get("size_bytes")
    expected_sha512 = artifact.get("sha512_base64")
    if (installer.stat().st_size != expected_size
            or not isinstance(expected_sha512, str)
            or base64.b64encode(_sha512(installer)).decode("ascii") != expected_sha512):
        raise QualificationError("测试目录中的官方 DSH 安装器与固定大小或 SHA-512 不匹配。")
    if not script.is_file():
        raise QualificationError("分发产物缺少官方 DSH 安装脚本。")
    command = [str(python), str(script), "--root", str(workspace), "--program", str(program),
               "--source", str(installer), "--task-id", task_id, "--wait-for-native-commit-ack"]
    process = subprocess.Popen(command, cwd=program, env=_base_env(program, workspace),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, name="official-dsh-qualifier-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + 1800
    acknowledged = False
    final: dict = {}
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if value.get("phase") == "swapping_runtime" and not acknowledged:
            assert process.stdin is not None
            ack = {"kind": "commit_ack", "task_id": task_id, "resource_id": "dsh"}
            process.stdin.write(json.dumps(ack, separators=(",", ":")) + "\n")
            process.stdin.flush()
            process.stdin.close()
            acknowledged = True
        if value.get("ok") is True:
            final = value
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)
        raise QualificationError("官方 DSH 安装超过验收时限；未记录成功状态。")
    if process.returncode != 0 or not acknowledged or final.get("installed_resources") != ["dsh"]:
        raise QualificationError("官方 DSH 固定安装器解包或提交确认失败。")
    return {"acknowledgement_received": True, "installed_resource_ids": ["dsh"]}


def _signal_shutdown(name: str) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenEventW(0x0002, False, name)
    if handle:
        try:
            kernel32.SetEvent(handle)
        finally:
            kernel32.CloseHandle(handle)


def _official_full_launch(python: Path, program: Path, workspace: Path,
                          timeout_seconds: int) -> dict:
    if os.name != "nt":
        raise QualificationError("官方 Windows DSH 全应用启动验收只支持 Windows。")
    launch_name = f"Local\\VoiceTerminal-ResourceQualification-{uuid.uuid4().hex}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    event = kernel32.CreateEventW(None, True, False, launch_name)
    if not event:
        raise QualificationError("无法建立测试专用的正常退出事件。")
    data_dir = _require_inside(workspace / "data", workspace, "data（工作区数据目录）")
    env = _base_env(program, workspace)
    portable = _json(workspace / "portable.json")
    runtime_root = _require_inside(workspace / portable["dsh"], workspace, "DSH（代理执行宿主）目录")
    models_root = _require_inside(workspace / portable["models"], workspace, "模型目录")
    runtime_metadata = _json(runtime_root / "runtime.json")
    executable_relative = runtime_metadata.get("executable")
    if not isinstance(executable_relative, str):
        raise QualificationError("官方 DSH 运行清单缺少 executable（执行文件）字段。")
    expected_executable = _require_inside(runtime_root / executable_relative, runtime_root,
                                          "DSH executable（执行文件）")
    if not expected_executable.is_file() or expected_executable.is_symlink() or expected_executable.is_junction():
        raise QualificationError("官方 DSH executable（执行文件）缺失或不是普通文件。")
    process_temp = _require_inside(workspace / "tmp" / "process", workspace, "TEMP/TMP（临时目录）")
    roaming = _require_inside(data_dir / "appdata" / "roaming", workspace, "APPDATA（漫游应用数据目录）")
    local_app_data = _require_inside(data_dir / "appdata" / "local", workspace, "LOCALAPPDATA（本机应用数据目录）")
    for directory in (process_temp, roaming, local_app_data):
        directory.mkdir(parents=True, exist_ok=True)
    env.update({
        "VOICE_DATA_DIR": str(data_dir),
        "VOICE_RESOURCE_POLICY": str(workspace / "config" / "runtime-resources.json"),
        "VOICE_MODEL_DIR": str(models_root),
        "VOICE_DSH_RUNTIME": str(runtime_root),
        "VOICE_NODE_EXECUTABLE": str(expected_executable),
        "DSH_HOME": str(data_dir / "dsh"),
        "TEMP": str(process_temp),
        "TMP": str(process_temp),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local_app_data),
        "npm_config_cache": str(workspace / "cache" / "npm"),
        "npm_config_store_dir": str(workspace / "cache" / "pnpm" / "store"),
        "npm_config_userconfig": str(workspace / "config" / "npmrc"),
        "XDG_CACHE_HOME": str(workspace / "cache"),
        "HF_HOME": str(workspace / "cache" / "huggingface"),
        "MODELSCOPE_CACHE": str(workspace / "cache" / "modelscope"),
        "VOICE_APP_SHUTDOWN_EVENT": launch_name,
        "PYTHONPATH": str(program) + os.pathsep + env.get("PYTHONPATH", ""),
    })
    startup_result = data_dir / "state" / "startup-result.json"
    if startup_result.exists():
        # Preserve a previous report and let the app write a fresh result under the active process identity.
        startup_result.rename(startup_result.with_name(f"startup-result-prior-{uuid.uuid4().hex}.json"))
    process = subprocess.Popen(
        [str(python), "-m", "voice_terminal.application.launch", "--root", str(program),
         "--data-dir", str(data_dir), "--headless"],
        cwd=program, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    listener = None
    try:
        try:
            import psutil
        except ImportError as exc:
            raise QualificationError("测试 Python 缺少 psutil（进程核验库）。") from exc
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise QualificationError("Voice Terminal（语音前端）在 DSH 就绪前退出。")
            try:
                descendants = psutil.Process(process.pid).children(recursive=True)
            except psutil.Error:
                descendants = []
            for child in descendants:
                try:
                    executable = Path(child.exe()).resolve()
                    if child.name().casefold() != expected_executable.name.casefold() or executable != expected_executable:
                        continue
                    listeners = [conn for conn in child.net_connections(kind="tcp")
                                 if conn.status == psutil.CONN_LISTEN and conn.laddr
                                 and conn.laddr.ip == "127.0.0.1" and conn.laddr.port > 0]
                    if listeners:
                        listener = {"pid": child.pid, "port": listeners[0].laddr.port}
                        return {"state": "ready", "listener": listener,
                                "startup_result_present": startup_result.is_file()}
                except (psutil.Error, OSError):
                    continue
            time.sleep(0.5)
        raise QualificationError("官方 DSH 未在时限内启动并建立回环监听。")
    finally:
        try:
            _signal_shutdown(launch_name)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    import psutil
                    children = psutil.Process(process.pid).children(recursive=True)
                    for child in reversed(children):
                        try:
                            child.terminate()
                        except psutil.Error:
                            pass
                    _, alive = psutil.wait_procs(children, timeout=5)
                    for child in alive:
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                except Exception:
                    pass
                process.kill()
                process.wait(timeout=10)
        finally:
            kernel32.CloseHandle(event)


def qualify(args: argparse.Namespace) -> tuple[dict, int]:
    test_root = _test_root(args.test_root)
    test_root.mkdir(parents=True, exist_ok=True)
    program = _require_inside(args.program, test_root, "program（分发程序目录）")
    workspace = _require_inside(args.workspace, test_root, "workspace（测试工作区）")
    python = _require_inside(args.python, test_root, "Python（测试解释器）")
    repo = args.repo.expanduser().resolve()
    bundle_source = args.bundle.expanduser().resolve() if args.bundle else repo / ".local/releases/fixtures/voice-resources-0.8.0.zip"
    smart_source = args.smart_turn_source.expanduser().resolve() if args.smart_turn_source else repo / ".local/models/analysis/smart-turn"
    report_path = _require_inside(args.report or (test_root / "resource-qualification.json"), test_root, "验收报告")
    manager = program / "scripts" / "manage-resources.py"
    official_script = program / "scripts" / "install-official-dsh.py"
    run_dir = test_root / f"异常样本 {uuid.uuid4().hex[:12]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    copied_bundle = run_dir / "全部资源 ZIP64.zip"
    records: list[dict] = []
    pending: list[dict] = []

    def passed(test_id: str, details: dict | None = None) -> None:
        records.append({"test_id": test_id, "status": "passed", "details": details or {}})

    def failed(test_id: str, error: Exception) -> None:
        records.append({"test_id": test_id, "status": "failed",
                        "error": error.args[0] if isinstance(error, QualificationError) else "验收步骤异常；详细诊断未写入摘要。"})

    if not python.is_file() or not program.is_dir() or not workspace.is_dir():
        raise QualificationError("传入的 Python、分发程序或测试工作区路径不存在。")
    for required in (manager, program / "config" / "resources" / "catalog.json",
                     workspace / "portable.json", workspace / "app" / "release.json"):
        if not required.is_file():
            raise QualificationError("分发产物或工作区缺少资源验收所需文件。")
    if (workspace / "tmp" / "resource-transaction.json").exists():
        raise QualificationError("测试工作区已有未完成资源事务；为保护测试数据，请先由单独恢复流程处理。")
    _require_stopped_workspace(workspace)

    try:
        copied_bundle = _copy_fixture(bundle_source, copied_bundle)
        passed("copy_offline_bundle", {"copied_to_test_root": True, "sha256_verified": True})
    except Exception as exc:
        failed("copy_offline_bundle", exc)
        copied_bundle = Path()

    try:
        inventory = _status(python, manager, program, workspace)
        if set(inventory) != set(RESOURCE_IDS) or any(inventory[item].get("state") != "missing" for item in RESOURCE_IDS):
            raise QualificationError("工作区初始资源库存必须恰有七项，且全部为 missing（缺失）。")
        passed("initial_inventory", {"resource_ids": list(RESOURCE_IDS), "all_missing": True})
    except Exception as exc:
        failed("initial_inventory", exc)
        report = _make_report(test_root, program, workspace, copied_bundle, records, pending)
        _write_report(report_path, report)
        return report, 1

    smart_spec = None
    try:
        smart_spec = next(item for item in _json(program / "config/resources/catalog.json")["resources"]
                          if item.get("resource_id") == "smart-turn")
        _safe_tree(smart_source)
        preview = _check_ok(_invoke_json(python, manager, "preview", program=program, workspace=workspace,
            arguments=["--source", str(smart_source), "--resource-id", "smart-turn", "--task-id", _task_id("smart-preview")]),
            "SmartTurn 文件夹预校验")
        if preview.get("result", {}).get("resource_ids") != ["smart-turn"]:
            raise QualificationError("SmartTurn 预校验未识别到固定资源编号。")
        imported = _check_ok(_invoke_json(python, manager, "import", program=program, workspace=workspace,
            arguments=["--source", str(smart_source), "--resource-id", "smart-turn", "--task-id", _task_id("smart-import")]),
            "SmartTurn 文件夹导入")
        current = _status(python, manager, program, workspace)["smart-turn"]
        if current.get("state") != "installed":
            raise QualificationError("SmartTurn 导入后未达到 installed（已安装）状态。")
        passed("smart_turn_folder_import", {"preview": True, "installed": True,
                "resource_id": "smart-turn", "import_result": imported.get("result", {})})
    except Exception as exc:
        failed("smart_turn_folder_import", exc)
        report = _make_report(test_root, program, workspace, copied_bundle, records, pending)
        _write_report(report_path, report)
        return report, 1

    try:
        prepared = _check_ok(_invoke_json(python, manager, "prepare", program=program, workspace=workspace,
            arguments=["--resource-id", "smart-turn", "--task-id", _task_id("installed-skip")]),
            "已安装资源跳过检查")
        if prepared.get("result", {}).get("already_installed") is not True:
            raise QualificationError("已安装 SmartTurn 未被标记为可跳过。")
        passed("installed_resource_skip", {"resource_id": "smart-turn", "already_installed": True})
    except Exception as exc:
        failed("installed_resource_skip", exc)

    before_snapshot = _model_snapshot(program, workspace, "smart-turn")
    try:
        corrupt_root = run_dir / "损坏单文件 SmartTurn"
        _safe_tree(smart_source)
        shutil.copytree(smart_source, corrupt_root, symlinks=False)
        required_paths = [item["path"] for item in smart_spec.get("files", []) if not item.get("optional")]
        candidates = [corrupt_root, corrupt_root / smart_spec["destination"],
                      corrupt_root / Path(smart_spec["destination"]).name]
        source_layout = next((candidate for candidate in candidates
                              if all((candidate / rel).is_file() for rel in required_paths)), None)
        if source_layout is None or not required_paths:
            raise QualificationError("无法在测试副本中定位 SmartTurn 必需文件。")
        damaged = source_layout.joinpath(*required_paths[0].split("/"))
        with damaged.open("ab") as stream:
            stream.write(b"qualification-corruption")
        rejected = _invoke_json(python, manager, "import", program=program, workspace=workspace,
            arguments=["--source", str(corrupt_root), "--resource-id", "smart-turn", "--task-id", _task_id("corrupt")])
        _expect_rejection(rejected, "损坏单文件导入", {"resource_mismatch", "resource_incomplete"})
        after_snapshot = _model_snapshot(program, workspace, "smart-turn")
        if before_snapshot != after_snapshot or _status(python, manager, program, workspace)["smart-turn"].get("state") != "installed":
            raise QualificationError("拒绝损坏文件后，已安装 SmartTurn 发生变化。")
        passed("single_file_corruption_rejected", {"existing_resource_preserved": True})
    except Exception as exc:
        failed("single_file_corruption_rejected", exc)

    try:
        if smart_spec is None:
            raise QualificationError("缺少 SmartTurn 资源定义，不能构造错误版本包。")
        wrong_bundle = run_dir / "错误版本 ZIP64.zip"
        _bundle_wrong_version(wrong_bundle, smart_spec)
        before_snapshot = _model_snapshot(program, workspace, "smart-turn")
        rejected = _invoke_json(python, manager, "import", program=program, workspace=workspace,
            arguments=["--source", str(wrong_bundle), "--task-id", _task_id("wrong-version")])
        _expect_rejection(rejected, "错误版本包导入", {"bundle_invalid"})
        if before_snapshot != _model_snapshot(program, workspace, "smart-turn"):
            raise QualificationError("拒绝错误版本包后，已安装 SmartTurn 发生变化。")
        passed("wrong_version_rejected", {"existing_resource_preserved": True})
    except Exception as exc:
        failed("wrong_version_rejected", exc)

    if copied_bundle.is_file():
        try:
            preview = _check_ok(_invoke_json(python, manager, "preview", program=program, workspace=workspace,
                arguments=["--source", str(copied_bundle), "--task-id", _task_id("bulk-preview")], timeout=3600),
                "批量 ZIP64 预校验")
            preview_result = preview.get("result", {})
            if (set(preview_result.get("resource_ids", [])) != set(RESOURCE_IDS)
                    or set(preview_result.get("already_installed_resource_ids", [])) != {"smart-turn"}):
                raise QualificationError("批量预校验未识别完整资源清单或已安装跳过项。")
            passed("bulk_bundle_preflight", {"zip64": True, "target_unchanged": True,
                    "already_installed_resource_ids": preview_result["already_installed_resource_ids"]})
        except Exception as exc:
            failed("bulk_bundle_preflight", exc)
        try:
            imported = _check_ok(_invoke_json(python, manager, "import", program=program, workspace=workspace,
                arguments=["--source", str(copied_bundle), "--task-id", _task_id("bulk-import")], timeout=7200),
                "批量 ZIP64 导入")
            installed = imported.get("result", {}).get("installed_resource_ids", [])
            skipped = imported.get("result", {}).get("skipped_resources", [])
            status = _status(python, manager, program, workspace)
            expected_skipped = ["smart-turn"]
            expected_installed = set(RESOURCE_IDS) - set(expected_skipped)
            if (set(installed) != expected_installed or set(skipped) != set(expected_skipped)
                    or any(status[item].get("state") != "installed" for item in RESOURCE_IDS)):
                raise QualificationError("批量导入未安装全部七项固定资源。")
            passed("bulk_bundle_import", {"installed_resource_ids": installed,
                    "skipped_resources": skipped, "all_seven_installed": True})
        except Exception as exc:
            failed("bulk_bundle_import", exc)
    else:
        pending.append({"test_id": "bulk_bundle", "status": "pending",
                        "reason": "没有可复制的离线 ZIP64 资源包；未尝试网络下载。"})

    try:
        details = _simulated_recovery(python, manager, program, workspace, run_dir)
        passed("transaction_recovery_simulation", details)
    except Exception as exc:
        failed("transaction_recovery_simulation", exc)

    official_status = {"status": "pending", "reason": "未找到固定官方安装器；未下载。"}
    try:
        installer = _find_official_installer(test_root, args.official_installer)
        if installer is None:
            pending.append({"test_id": "official_dsh_extract_and_launch", "status": "pending",
                            "reason": "测试目录没有已存在的固定官方安装器；未下载。"})
        else:
            official_status = {"status": "found_in_test_root", "filename": installer.name}
            install_details = _install_official_dsh(python, official_script, program, workspace,
                installer, _task_id("official-dsh"))
            official = _status(python, manager, program, workspace).get("dsh")
            if not official or official.get("state") != "installed":
                raise QualificationError("官方 DSH 安装后库存未达到 installed（已安装）状态。")
            passed("official_dsh_fixed_install", install_details)
            launch_details = _official_full_launch(python, program, workspace, args.startup_timeout_seconds)
            passed("official_dsh_full_launch", launch_details)
            official_status = {"status": "passed", "filename": installer.name,
                               "full_launch": launch_details}
    except Exception as exc:
        failed("official_dsh_extract_and_launch", exc)
        official_status = {"status": "failed", "error": exc.args[0] if isinstance(exc, QualificationError)
                           else "官方 DSH 验收异常；详细诊断未写入摘要。"}

    report = _make_report(test_root, program, workspace, copied_bundle, records, pending)
    report["official_dsh"] = official_status
    _write_report(report_path, report)
    return report, 1 if any(row["status"] == "failed" for row in records) else 0


def _make_report(test_root: Path, program: Path, workspace: Path,
                 bundle: Path, records: list[dict], pending: list[dict]) -> dict:
    passed_count = sum(row["status"] == "passed" for row in records)
    failed_count = sum(row["status"] == "failed" for row in records)
    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "test_root": str(test_root),
        "program_root": str(program),
        "workspace_root": str(workspace),
        "offline_bundle": str(bundle) if bundle and str(bundle) != "." else "",
        "summary": {"passed": passed_count, "failed": failed_count, "pending": len(pending)},
        "test_cases": records,
        "pending": pending,
        "official_dsh": {"status": "pending", "reason": "该步骤尚未执行；不会自动下载官方安装器。"},
        "fields": {
            "ok": "本次离线验收命令是否成功",
            "report": "resource-qualification.json（验收结果摘要）的保存路径",
            "message": "不包含凭据的安全摘要",
            "schema_version": "验收报告格式版本",
            "created_at_utc": "报告生成时间（UTC）",
            "test_root": "测试专用根目录",
            "program_root": "本轮分发程序目录",
            "workspace_root": "隔离验收工作区根目录",
            "offline_bundle": "从仓库只读静态资源复制来的 ZIP64（大文件 ZIP）包",
            "summary": "通过、失败和待验收计数",
            "passed": "通过步骤数",
            "failed": "失败步骤数",
            "pending": "未具备离线样本或条件的待验收步骤数",
            "test_cases": "验收步骤及结果",
            "test_id": "验收步骤编号",
            "status": "步骤状态：passed（通过）、failed（失败）、pending（待验收）",
            "details": "不含凭据的结构化结果",
            "error": "安全错误摘要，不写入子进程原始输出",
            "reason": "待验收或失败原因的安全摘要",
            "filename": "固定官方安装器的文件名",
            "full_launch": "官方 DSH 完整启动结果",
            "official_dsh": "固定官方 DSH（代理执行宿主）安装器和全应用启动验收结果",
            "resource_id": "固定资源编号",
            "resource_ids": "识别或预校验通过的固定资源编号数组",
            "installed_resource_ids": "已安装资源编号数组",
            "skipped_resources": "通过完整摘要校验并因已安装而跳过的资源编号数组",
            "already_installed_resource_ids": "批量预校验中已安装且导入时会跳过的资源编号数组",
            "all_missing": "初始七项资源是否全部缺失",
            "all_seven_installed": "批量导入后七项资源是否全部已安装",
            "already_installed": "资源是否已安装并可跳过后续下载",
            "existing_resource_preserved": "拒绝损坏或错版输入后既有资源是否保持一致",
            "restored_previous_target": "事务恢复是否恢复旧资源目录",
            "restored_release_manifest": "事务恢复是否恢复发行组件清单",
            "transaction_marker_removed": "恢复后是否清理事务标记",
            "acknowledgement_received": "是否收到本地原生提交确认",
            "copied_to_test_root": "离线包是否复制到 D 盘测试根目录",
            "sha256_verified": "复制前后 SHA-256（文件摘要）是否一致",
            "preview": "安装前预校验是否成功",
            "installed": "单资源导入后是否达到已安装状态",
            "import_result": "单资源导入的结构化结果",
            "zip64": "离线包是否按 ZIP64（大文件 ZIP）格式导出",
            "target_unchanged": "预校验期间目标资源是否保持不变",
            "listener": "DSH 本机回环监听信息",
            "startup_result_present": "是否生成应用启动结果记录",
            "pid": "进程标识",
            "port": "实际监听端口",
        },
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        report, exit_code = qualify(args)
    except Exception as exc:
        safe_message = exc.args[0] if isinstance(exc, QualificationError) else "验收工具准备或运行失败；详细异常未写入摘要。"
        test_root: Path | None = None
        report_path: Path | None = None
        program_report = Path()
        workspace_report = Path()
        try:
            test_root = _test_root(args.test_root)
            report_path = _require_inside(args.report or (test_root / "resource-qualification.json"),
                                          test_root, "验收报告")
            program_report = _require_inside(args.program, test_root, "program（分发程序目录）")
            workspace_report = _require_inside(args.workspace, test_root, "workspace（测试工作区）")
        except Exception:
            pass
        report_written = False
        if test_root is not None and report_path is not None:
            try:
                report = _make_report(test_root, program_report, workspace_report, Path(),
                                      [{"test_id": "qualification", "status": "failed", "error": safe_message}], [])
                _write_report(report_path, report)
                report_written = True
            except Exception:
                pass
        output = {"ok": False, "report": str(report_path) if report_written else None,
                  "message": safe_message,
                  "fields": {"ok": "本次离线验收命令是否成功",
                             "report": "验收结果摘要保存路径；null（空值）表示未写入",
                             "message": "不包含凭据的安全摘要"}}
        print(json.dumps(output, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": exit_code == 0, "report": str(args.report or (args.test_root / "resource-qualification.json")),
                      "summary": report["summary"],
                      "fields": {"ok": "本次离线验收命令是否成功",
                                 "report": "验收结果摘要保存路径",
                                 "summary": "通过、失败和待验收计数"}}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

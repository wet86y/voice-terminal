"""Install a pinned official DSH tree（安装锁定的官方 DSH 运行目录）."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

ROOT = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").is_file())
sys.path.insert(0, str(ROOT))

from voice_terminal.distribution import manifest, safe_path, verify
from voice_terminal.paths import atomic_json
from voice_terminal.dsh.bridge import BRIDGE_PROTOCOL_VERSION
from voice_terminal.dsh.official import (
    OFFICIAL_ASAR,
    OFFICIAL_ASAR_UNPACKED,
    OFFICIAL_CLI,
    OFFICIAL_EXECUTABLE,
    OFFICIAL_OFFICE_CLI,
    OFFICIAL_OFFICE_ENGINE,
    OFFICIAL_SCHEDULE,
    OFFICIAL_SOURCE_KIND,
    OfficialArtifact,
    hash_official_installer,
    load_official_artifact,
    validate_official_runtime,
)

_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA512_HEX = re.compile(r"^[0-9a-f]{128}$")
_COMMIT_ACK_TIMEOUT_SECONDS = 60
_OFFICIAL_RUNTIME_LOCK_FIELDS = {
    "schema": "可信提取记录格式版本",
    "version": "固定官方 DSH（代理执行宿主）版本",
    "source_sha512": "固定官方安装器完整 SHA-512（文件摘要）十六进制值",
    "manifest_sha256": "完整提取运行清单 SHA-256（文件摘要）十六进制值",
}


def _emit_progress(phase: str, completed_bytes: int, total_bytes: int) -> None:
    """Emit one JSON progress line（JSON 进度行）for the native task bridge."""
    print(json.dumps({
        "phase": phase,
        "completed_bytes": completed_bytes,
        "total_bytes": total_bytes,
    }, ensure_ascii=False), flush=True)


def _wait_for_native_commit_ack(task_id: str) -> None:
    """Wait for native service quiescence before replacing the managed runtime.

    The native resource manager（原生资源管理器）first marks this phase
    non-cancellable and stops Voice services, then writes one JSON ACK line.
    No target, backup, or transaction marker is touched before this succeeds.
    """
    result: list[str] = []
    completed = threading.Event()

    def read_line() -> None:
        try:
            result.append(sys.stdin.readline(4097))
        except Exception:
            result.append("")
        finally:
            completed.set()

    reader = threading.Thread(target=read_line, name="dsh-commit-ack", daemon=True)
    reader.start()
    if not completed.wait(_COMMIT_ACK_TIMEOUT_SECONDS):
        raise TimeoutError("等待原生 DSH 提交确认超时；现有运行目录未替换。")

    line = result[0] if result else ""
    if not line or len(line) > 4096 or not line.endswith("\n"):
        raise ValueError("未收到完整的原生 DSH 提交确认；现有运行目录未替换。")
    try:
        ack = json.loads(line)
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("原生 DSH 提交确认格式无效；现有运行目录未替换。") from None
    expected = {"kind": "commit_ack", "task_id": task_id, "resource_id": "dsh"}
    if not isinstance(ack, dict) or ack != expected:
        raise ValueError("原生 DSH 提交确认与当前任务不匹配；现有运行目录未替换。")


@contextmanager
def _workspace_lock(path: Path):
    """Serialize DSH directory swaps（串行化受管目录替换）across installer processes."""
    if path.is_symlink() or path.is_junction():
        raise ValueError("DSH 安装锁不能是链接。")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _remove_tree(path: Path, root: Path) -> None:
    """Remove only a normal in-workspace tree（仅移除工作区内普通目录）."""
    if not path.exists() and not path.is_symlink() and not path.is_junction():
        return
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("拒绝清理工作区之外的 DSH 暂存目录。")
    if path.is_symlink() or path.is_junction():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _transaction_paths(root: Path, marker: dict) -> tuple[Path, Path, Path]:
    """Resolve journal paths（解析事务日志路径）without allowing traversal."""
    if not isinstance(marker.get("target"), str) or not isinstance(marker.get("backup"), str):
        raise ValueError("DSH 目录替换记录无效。")
    target = safe_path(root, marker["target"])
    backup = safe_path(root, marker["backup"])
    stage_value = marker.get("stage")
    stage = safe_path(root, stage_value) if isinstance(stage_value, str) else root / "tmp"
    return target, backup, stage


def _restore_metadata(root: Path, records: list, *, require_snapshots: bool) -> None:
    """Restore JSON sidecar snapshots（恢复 JSON 发行清单快照）from a pending transaction."""
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("path"), str)
                or not isinstance(record.get("snapshot"), str)
                or not isinstance(record.get("existed"), bool)):
            raise ValueError("DSH 目录替换元数据记录无效。")
        metadata_path = safe_path(root, record["path"])
        snapshot_path = safe_path(root, record["snapshot"])
        if metadata_path.is_symlink() or metadata_path.is_junction():
            raise ValueError("DSH 目录替换元数据不能是链接。")
        if record["existed"]:
            if snapshot_path.is_symlink() or snapshot_path.is_junction() or not snapshot_path.is_file():
                if require_snapshots:
                    raise ValueError("DSH 目录替换元数据备份缺失。")
                continue
            try:
                snapshot_value = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                if require_snapshots:
                    raise ValueError("DSH 目录替换元数据备份无法读取。") from exc
                continue
            if not isinstance(snapshot_value, dict):
                if require_snapshots:
                    raise ValueError("DSH 目录替换元数据备份格式无效。")
                continue
            atomic_json(metadata_path, snapshot_value)
        else:
            metadata_path.unlink(missing_ok=True)


def _recover_transaction(root: Path, marker_path: Path) -> None:
    """Restore an interrupted swap（恢复中断的目录替换）before accepting another task."""
    marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
    if not isinstance(marker, dict) or marker.get("schema") != 1:
        raise ValueError("DSH 目录替换记录无法安全恢复。")
    target, backup, stage = _transaction_paths(root, marker)
    phase = marker.get("phase")
    had_target = marker.get("target_preexisting") is True
    metadata_records = marker.get("metadata", [])
    if not isinstance(metadata_records, list):
        raise ValueError("DSH 目录替换元数据记录无效。")
    metadata_snapshots: list[tuple[Path, bool, dict | None]] = []
    if phase == "prepared":
        for record in metadata_records:
            if (not isinstance(record, dict) or not isinstance(record.get("path"), str)
                    or not isinstance(record.get("snapshot"), str)
                    or not isinstance(record.get("existed"), bool)):
                raise ValueError("DSH 目录替换元数据记录无效。")
            metadata_path = safe_path(root, record["path"])
            snapshot_path = safe_path(root, record["snapshot"])
            if metadata_path.is_symlink() or metadata_path.is_junction():
                raise ValueError("DSH 目录替换元数据不能是链接。")
            snapshot_value = None
            if record["existed"]:
                if snapshot_path.is_symlink() or snapshot_path.is_junction() or not snapshot_path.is_file():
                    raise ValueError("DSH 目录替换元数据备份缺失。")
                try:
                    snapshot_value = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("DSH 目录替换元数据备份无法读取。") from exc
                if not isinstance(snapshot_value, dict):
                    raise ValueError("DSH 目录替换元数据备份格式无效。")
            metadata_snapshots.append((metadata_path, record["existed"], snapshot_value))
    if phase == "promoted":
        if not target.is_dir():
            if had_target and backup.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
                _restore_metadata(root, metadata_records, require_snapshots=False)
                if stage.exists():
                    _remove_tree(stage, root)
                marker_path.unlink(missing_ok=True)
                raise ValueError("DSH 新目录丢失，已恢复上一份受管目录。")
            raise ValueError("DSH 新目录丢失，无法确认安装结果。")
        if backup.exists():
            _remove_tree(backup, root)
    elif phase == "prepared":
        if had_target:
            if backup.exists():
                failed: Path | None = None
                if target.exists():
                    failed = stage / "interrupted-new"
                    if failed.exists():
                        raise ValueError("发现未完成的 DSH 回滚暂存，不能覆盖。")
                    os.replace(target, failed)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
                if failed is not None and failed.exists():
                    _remove_tree(failed, root)
            elif not target.exists():
                raise ValueError("旧 DSH 目录备份缺失，无法安全恢复。")
        elif target.exists():
            _remove_tree(target, root)
    else:
        raise ValueError("DSH 目录替换阶段未知，拒绝自动清理。")
    if phase == "prepared":
        for metadata_path, existed, snapshot_value in metadata_snapshots:
            if existed:
                assert snapshot_value is not None
                atomic_json(metadata_path, snapshot_value)
            else:
                metadata_path.unlink(missing_ok=True)
    if stage.exists():
        _remove_tree(stage, root)
    marker_path.unlink(missing_ok=True)


def _recover_pending(root: Path) -> None:
    for marker in sorted((root / "tmp").glob("dsh-install-*.json")):
        if marker.is_symlink() or marker.is_junction() or not marker.is_file():
            raise ValueError("发现不安全的 DSH 目录替换记录。")
        _recover_transaction(root, marker)


def _7zip(program: Path) -> Path:
    """Find the packaged 7-Zip pair（查找随程序携带的完整解包器）."""
    executable = program / "tools/7zip/7z.exe"
    library = executable.with_name("7z.dll")
    if (executable.is_symlink() or executable.is_junction() or library.is_symlink()
            or library.is_junction() or not executable.is_file() or not library.is_file()):
        raise FileNotFoundError("缺少应用自带的完整 7-Zip（压缩解包工具）及 7z.dll。")
    return executable


def _extract(executable: Path, archive: Path, destination: Path, *, phase: str, total: int) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    _emit_progress(phase, 0, total)
    try:
        result = subprocess.run(
            [str(executable), "x", str(archive), f"-o{destination}", "-y", "-bd", "-bso0", "-bsp0"],
            cwd=str(destination), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as exc:
        raise RuntimeError("7-Zip（压缩解包工具）无法启动。") from exc
    if result.returncode != 0:
        raise RuntimeError(f"7-Zip（压缩解包工具）解包失败（exit {result.returncode}）。")
    _emit_progress(phase, total, total)


def _locate_payload(outer: Path) -> Path:
    matches = [path for path in outer.rglob("app-64.7z") if path.is_file()]
    if len(matches) != 1:
        raise ValueError("官方 NSIS（安装器）中没有唯一的 app-64.7z（64 位应用负载）。")
    if matches[0].is_symlink() or matches[0].is_junction():
        raise ValueError("官方 NSIS（安装器）负载不能是链接。")
    return matches[0]


def _locate_application(extracted: Path) -> Path:
    """Find the single Electron app root（查找唯一 Electron 应用根目录）."""
    direct = extracted / OFFICIAL_EXECUTABLE
    if (direct.is_file() and (extracted / OFFICIAL_ASAR).is_file()):
        return extracted
    candidates = []
    for asar in extracted.rglob("app.asar"):
        if asar.is_file() and asar.as_posix().endswith("/resources/app.asar"):
            app_root = asar.parent.parent
            if (app_root / OFFICIAL_EXECUTABLE).is_file():
                candidates.append(app_root)
    unique = {path.resolve() for path in candidates}
    if len(unique) != 1:
        raise ValueError("官方 64 位应用负载中无法唯一识别 DeepSeek Harness（桌面客户端）目录。")
    return next(iter(unique))


def _install_metadata(stage: Path, artifact: OfficialArtifact) -> None:
    metadata = {
        "schema": 2,
        "kind": OFFICIAL_SOURCE_KIND,
        "source_kind": OFFICIAL_SOURCE_KIND,
        "version": artifact.version,
        "bridge_protocol": BRIDGE_PROTOCOL_VERSION,
        # compatibility commit（兼容源码锁）：描述项目验证的上游版本组合，不表示二进制构建来源。
        "compat_expected_commit": artifact.compatibility_commit,
        "source_sha512": artifact.sha512_hex,
        "executable": OFFICIAL_EXECUTABLE,
        "app_asar": OFFICIAL_ASAR,
        "entry": OFFICIAL_CLI,
        "schedule": OFFICIAL_SCHEDULE,
        "office_cli": OFFICIAL_OFFICE_CLI,
        "office_engine": OFFICIAL_OFFICE_ENGINE,
        "asar_unpacked_root": OFFICIAL_ASAR_UNPACKED,
    }
    atomic_json(stage / "runtime.json", metadata)
    record = manifest(stage, kind="dsh", build=f"official-{artifact.version}-{artifact.sha512_hex[:16]}")
    atomic_json(stage / "manifest.json", record)
    verify(stage, record, exact=True)


def _install_from_installer(source: Path, stage: Path, program: Path,
                            artifact: OfficialArtifact) -> str:
    digest = hash_official_installer(source, artifact, lambda done, total: _emit_progress(
        "verifying_installer", done, total,
    ))
    extractor = _7zip(program)
    outer = stage / "nsis"
    _extract(extractor, source, outer, phase="extracting_nsis", total=artifact.size_bytes)
    payload = _locate_payload(outer)
    extracted = stage / "payload"
    _extract(extractor, payload, extracted, phase="extracting_payload", total=payload.stat().st_size)
    _remove_tree(outer, stage.parent)
    app_root = _locate_application(extracted)
    if app_root != stage / "runtime":
        shutil.move(str(app_root), str(stage / "runtime"))
    _remove_tree(extracted, stage.parent)
    _install_metadata(stage / "runtime", artifact)
    return digest


def _export_official_runtime_lock(destination: Path, runtime: Path, artifact: OfficialArtifact,
                                  verified_installer_sha512: str) -> dict:
    """Export a maintainer record only from a fixed, fully verified installer extraction（导出已核验安装器的维护者记录）."""
    if (not isinstance(verified_installer_sha512, str)
            or not _SHA512_HEX.fullmatch(verified_installer_sha512)
            or verified_installer_sha512 != artifact.sha512_hex):
        raise ValueError("可信提取记录只能由 SHA-512（文件摘要）匹配固定版本的安装器生成。")
    paths = validate_official_runtime(runtime, artifact, verify_required=False)
    verify(runtime, paths.manifest, exact=True)
    if (paths.metadata.get("version") != artifact.version
            or paths.metadata.get("source_sha512") != verified_installer_sha512):
        raise ValueError("提取运行目录与已核验安装器版本或摘要不匹配。")
    manifest_sha256 = hashlib.sha256((runtime / "manifest.json").read_bytes()).hexdigest()
    record = {
        "schema": 1,  # schema（记录格式版本）：当前可信提取摘要记录格式。
        "version": artifact.version,  # version（固定版本）：本体锁定的官方 DSH 版本。
        "source_sha512": verified_installer_sha512,  # source_sha512（来源摘要）：固定官方安装器完整 SHA-512。
        "manifest_sha256": manifest_sha256,  # manifest_sha256（清单摘要）：完整提取文件清单的 SHA-256。
        "fields": dict(_OFFICIAL_RUNTIME_LOCK_FIELDS),  # fields（字段说明）：随包提供的中文字段说明。
    }
    destination = destination.expanduser()
    if destination.exists() and (destination.is_symlink() or destination.is_junction() or not destination.is_file()):
        raise ValueError("可信提取记录目标必须是普通文件，不能是链接或目录。")
    parent = destination.parent.resolve(strict=False)
    current = destination.parent
    while True:
        if current.is_symlink() or current.is_junction():
            raise ValueError("可信提取记录不能写入链接或目录联接。")
        if current.parent == current:
            break
        current = current.parent
    if destination.exists() and not destination.resolve().is_relative_to(parent):
        raise ValueError("可信提取记录目标超出目标目录。")
    atomic_json(destination, record)
    return record


def _copy_verified_tree(source: Path, stage: Path, artifact: OfficialArtifact) -> str:
    if source.is_symlink() or source.is_junction() or not source.is_dir():
        raise ValueError("官方 DSH tree（运行目录）来源必须是普通目录。")
    paths = validate_official_runtime(source, artifact)
    verify(source, paths.manifest, exact=True)
    shutil.copytree(source, stage / "runtime", symlinks=False)
    paths = validate_official_runtime(stage / "runtime", artifact)
    verify(stage / "runtime", paths.manifest, exact=True)
    return artifact.sha512_hex


def _release_update(root: Path, staged_runtime: Path) -> tuple[Path, dict]:
    """Build the workspace release binding（构造工作区发行组件绑定）before swap."""
    release_path = safe_path(root, "app/release.json")
    if (release_path.is_symlink() or release_path.is_junction()
            or not release_path.is_file()):
        raise FileNotFoundError("工作区缺少 app/release.json（发行组件清单）。")
    try:
        release = json.loads(release_path.read_text(encoding="utf-8-sig"))
        runtime_manifest = json.loads((staged_runtime / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("DSH 或工作区发行组件清单格式无效。") from exc
    if not isinstance(release, dict) or not isinstance(release.get("components"), list):
        raise ValueError("工作区发行组件清单缺少 components（组件列表）。")
    if (not isinstance(runtime_manifest, dict) or runtime_manifest.get("kind") != "dsh"
            or not isinstance(runtime_manifest.get("build"), str)):
        raise ValueError("DSH 运行清单无法绑定到工作区发行记录。")
    component = next((item for item in release["components"]
                      if isinstance(item, dict) and item.get("kind") == "dsh"), None)
    if component is None:
        component = {"kind": "dsh"}  # kind（组件类别）：代理执行宿主。
        release["components"].append(component)
    component.update({
        "path": "runtimes/dsh",  # path（工作区内运行目录）：官方 DSH 运行树。
        "build": runtime_manifest["build"],  # build（运行清单构建编号）：绑定本次安装内容。
        "manifest_sha256": hashlib.sha256((staged_runtime / "manifest.json").read_bytes()).hexdigest(),  # manifest_sha256（清单 SHA-256 摘要）：绑定实际文件列表。
        "optional": True,  # optional（可选组件）：基础程序可在未安装 DSH 时运行。
    })
    return release_path, release


def _swap_into_place(root: Path, stage: Path, task_id: str) -> bool:
    target = safe_path(root, "runtimes/dsh")
    if target.is_symlink() or target.is_junction():
        raise ValueError("DSH 受管安装目录不能是链接。")
    target.parent.mkdir(parents=True, exist_ok=True)
    staged_runtime = stage / "runtime"
    if not staged_runtime.is_dir():
        raise ValueError("DSH 安装暂存目录未准备完整。")
    release_path, release = _release_update(root, staged_runtime)
    metadata_snapshot = stage / "metadata-snapshots" / "app-release.json"
    metadata_snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(release_path, metadata_snapshot)
    backup = root / "tmp" / f"dsh-backup-{task_id}"
    marker_path = root / "tmp" / f"dsh-install-{task_id}.json"
    if backup.exists() or marker_path.exists():
        raise FileExistsError("DSH 安装任务已有未清理的事务记录。")
    marker = {
        "schema": 1,
        "phase": "prepared",
        "target_preexisting": target.exists(),
        "target": "runtimes/dsh",
        "backup": backup.relative_to(root).as_posix(),
        "stage": stage.relative_to(root).as_posix(),
        "metadata": [{
            "path": release_path.relative_to(root).as_posix(),
            "snapshot": metadata_snapshot.relative_to(root).as_posix(),
            "existed": True,
        }],
    }
    atomic_json(marker_path, marker)
    try:
        if target.exists():
            os.replace(target, backup)
        os.replace(staged_runtime, target)
        # app/release.json（发行组件清单）与运行目录处于同一事务，避免出现未绑定的官方 DSH。
        atomic_json(release_path, release)
        marker["phase"] = "promoted"
        atomic_json(marker_path, marker)
    except BaseException:
        if marker_path.exists():
            _recover_transaction(root, marker_path)
        raise
    # The promoted marker is the commit point.  Cleanup errors leave a recoverable
    # marker/backup for the next install instead of reporting failure after replacing
    # a previously valid runtime.
    try:
        if backup.exists():
            _remove_tree(backup, root)
        marker_path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"DSH（代理执行宿主）安装已提交；旧目录清理将在下次任务恢复：{type(exc).__name__}",
              file=sys.stderr, flush=True)
        return False
    return True


def install(root: Path, program: Path, source: Path, task_id: str,
            wait_for_native_commit_ack: bool = False,
            export_trust_record: Path | None = None) -> None:
    """Install a verified tree from the installer or an attested local tree."""
    if not _TASK_ID.fullmatch(task_id) or task_id in {".", ".."}:
        raise ValueError("DSH task-id（任务编号）格式无效。")
    if any(path.is_symlink() or path.is_junction() for path in (root, program, source)):
        raise ValueError("DSH 安装路径不能从链接进入。")
    root, program, source = root.resolve(), program.resolve(), source.resolve()
    if not program.is_dir() or not root.is_dir():
        raise FileNotFoundError("workspace root（工作区根目录）或 program（程序目录）不存在。")
    artifact = load_official_artifact(program)
    if export_trust_record is not None and source.is_dir():
        raise ValueError("不能从本地目录导出可信记录；请使用固定官方 EXE（安装器）来源。")
    temp_root = root / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with _workspace_lock(temp_root / "dsh-install.lock"):
        _recover_pending(root)
        stage = temp_root / f"dsh-official-{task_id}"
        if stage.exists() or stage.is_symlink() or stage.is_junction():
            raise FileExistsError("DSH 安装暂存目录已存在；请先完成或清理同编号任务。")
        stage.mkdir()
        try:
            if source.is_dir():
                _copy_verified_tree(source, stage, artifact)
            else:
                verified_installer_sha512 = _install_from_installer(source, stage, program, artifact)
                if export_trust_record is not None:
                    _export_official_runtime_lock(
                        export_trust_record, stage / "runtime", artifact, verified_installer_sha512,
                    )
            validate_official_runtime(stage / "runtime", artifact)
            _emit_progress("swapping_runtime", 0, 1)
            if wait_for_native_commit_ack:
                _wait_for_native_commit_ack(task_id)
            transaction_clean = _swap_into_place(root, stage, task_id)
            _emit_progress("complete", 1, 1)
        except BaseException:
            marker = temp_root / f"dsh-install-{task_id}.json"
            if marker.exists():
                _recover_transaction(root, marker)
            if stage.exists():
                _remove_tree(stage, root)
            raise
        if transaction_clean and stage.exists():
            try:
                _remove_tree(stage, root)
            except Exception as exc:
                print(f"DSH（代理执行宿主）安装已提交；暂存清理待恢复：{type(exc).__name__}",
                      file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="安装固定版本官方 DSH 桌面运行组件")
    parser.add_argument("--root", required=True, type=Path, help="workspace root（工作区根目录）")
    parser.add_argument("--program", required=True, type=Path, help="program（程序目录）")
    parser.add_argument("--source", required=True, type=Path, help="固定官方 EXE（安装器）或已校验运行目录")
    parser.add_argument("--task-id", required=True, help="task-id（资源任务编号）")
    parser.add_argument(
        "--wait-for-native-commit-ack", action="store_true",
        help="wait for the native service manager（原生服务管理器）ACK before replacing the runtime",
    )
    parser.add_argument(
        "--export-trust-record", type=Path, metavar="PATH",
        help="可选：仅在固定 EXE（安装器）SHA-512（文件摘要）验证、实际提取与完整文件清单验证后，导出维护者用 official-runtime.lock.json（可信提取记录）；不能由目录来源生成",
    )
    args = parser.parse_args()
    try:
        install(args.root, args.program, args.source, args.task_id,
                wait_for_native_commit_ack=args.wait_for_native_commit_ack,
                export_trust_record=args.export_trust_record)
    except Exception as exc:
        print(f"DSH 官方运行环境安装失败：{exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps({"ok": True, "installed_resources": ["dsh"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

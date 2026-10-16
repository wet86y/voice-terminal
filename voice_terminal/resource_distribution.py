"""Resource distribution（资源分发）：独立于语音服务的锁定资源管理。"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any, Iterator
import zipfile

from .distribution import verify as verify_component
from .dsh.official import OFFICIAL_SOURCE_KIND, load_official_artifact, validate_official_runtime
from .local_layout import read_portable


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA512_HEX = re.compile(r"^[0-9a-f]{128}$")
_SHA512_B64 = re.compile(r"^[A-Za-z0-9+/]{86}==$|^[A-Za-z0-9+/]{87}=$|^[A-Za-z0-9+/]{88}$")
_MANIFEST_SCHEMA = 1
_MAX_BUNDLE_INDEX_BYTES = 32 * 1024 * 1024
_MAX_BUNDLE_FILES = 100_000
_MAX_BUNDLE_BYTES = 24 * 1024**3
_OFFICIAL_RUNTIME_LOCK_FIELDS = {
    "schema": "可信提取记录格式版本",
    "version": "固定官方 DSH（代理执行宿主）版本",
    "source_sha512": "固定官方安装器完整 SHA-512（文件摘要）十六进制值",
    "manifest_sha256": "完整提取运行清单 SHA-256（文件摘要）十六进制值",
}


class ResourceError(RuntimeError):
    """ResourceError（资源管理错误）：message（用户提示）不携带请求或凭据内容。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ResourceFile:
    """ResourceFile（资源文件）：安装路径与可选的上游 source_path（来源相对路径）。"""

    path: str
    size_bytes: int
    sha256: str
    optional: bool = False
    asset: str | None = None
    source_path: str | None = None


@dataclass(frozen=True)
class ResourceSpec:
    """ResourceSpec（固定资源描述）：所有路径和来源均由随包 catalog（资源清单）锁定。"""

    resource_id: str
    kind: str
    title: str
    destination: str
    version: str
    source: dict[str, Any]
    files: tuple[ResourceFile, ...]
    required: bool = True  # required（是否必需）：只有显式 false（可选）不参与整体启动门槛。
    generated_assets: tuple[dict[str, Any], ...] = ()
    legacy_export: dict[str, Any] | None = None
    legacy: bool = False

    @property
    def required_files(self) -> tuple[ResourceFile, ...]:
        return tuple(item for item in self.files if not item.optional)

    @property
    def expected_files(self) -> tuple[ResourceFile, ...]:
        return self.files


class ResourceCatalog:
    """ResourceCatalog（资源目录）：验证 schema 并创建不可变 spec 映射。"""

    def __init__(self, program: Path):
        self.program = program.resolve()
        self.path = self.program / "config" / "resources" / "catalog.json"
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResourceError("catalog_unavailable", "随包资源清单缺失或无法读取。") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ResourceError("catalog_invalid", "随包资源清单格式不受支持。")
        raw = value.get("resources")
        if not isinstance(raw, list):
            raise ResourceError("catalog_invalid", "随包资源清单缺少资源列表。")
        specs: dict[str, ResourceSpec] = {}
        for item in raw:
            spec = self._parse_spec(item)
            if spec.resource_id in specs:
                raise ResourceError("catalog_invalid", "随包资源清单包含重复编号。")
            specs[spec.resource_id] = spec
        self.specs = specs
        self.fields = value.get("fields", {})

    def _parse_spec(self, raw: Any) -> ResourceSpec:
        if not isinstance(raw, dict):
            raise ResourceError("catalog_invalid", "随包资源描述格式不正确。")
        resource_id = raw.get("resource_id")
        kind = raw.get("kind")
        title = raw.get("title")
        destination = raw.get("destination")
        version = raw.get("version")
        source = raw.get("source", {})
        files_raw = raw.get("files", [])
        legacy_export = raw.get("legacy_export", {})
        if (not isinstance(resource_id, str) or not _ID.fullmatch(resource_id)
                or kind not in {"model", "dsh"} or not isinstance(title, str)
                or not isinstance(version, str) or not isinstance(source, dict)
                or not isinstance(files_raw, list) or not isinstance(legacy_export, dict)):
            raise ResourceError("catalog_invalid", "随包资源描述缺少固定编号、类型或版本。")
        _safe_relative(destination)
        files: list[ResourceFile] = []
        seen: set[str] = set()
        for entry in files_raw:
            if not isinstance(entry, dict):
                raise ResourceError("catalog_invalid", "随包资源文件描述格式不正确。")
            path = entry.get("path")
            size = entry.get("size_bytes")
            digest = entry.get("sha256")
            optional = entry.get("optional", False)
            asset = entry.get("asset")
            source_path = entry.get("source_path")
            _safe_relative(path)
            if source_path is not None:
                _safe_relative(source_path)
            if (path in seen or not isinstance(size, int) or size < 0
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                    or not isinstance(optional, bool)
                    or (asset is not None and not isinstance(asset, str))
                    or (source_path is not None and not isinstance(source_path, str))):
                raise ResourceError("catalog_invalid", "随包资源文件摘要、大小或路径无效。")
            if asset is not None:
                _safe_relative(asset)
            seen.add(path)
            files.append(ResourceFile(path, size, digest, optional, asset, source_path))
        if kind == "model" and not any(not item.optional for item in files):
            raise ResourceError("catalog_invalid", "模型资源缺少必需文件。")
        if kind == "dsh" and raw.get("origin") not in {"official_installer", "npm_package"}:
            raise ResourceError("catalog_invalid", "DSH 资源只能声明固定官方安装器来源。")
        if kind == "dsh" and raw.get("origin") == "npm_package":
            from .dsh.distribution import target_spec
            target = target_spec(self.program)
            version = target["version"]
            source = {**source, "distribution": target}
        if kind == "dsh" and raw.get("origin") == "official_installer":
            artifact = source.get("transfer", {}).get("artifact", {})
            if (not isinstance(artifact, dict) or not isinstance(artifact.get("size_bytes"), int)
                    or artifact["size_bytes"] <= 0 or not isinstance(artifact.get("sha512_base64"), str)
                    or not _SHA512_B64.fullmatch(artifact["sha512_base64"])):
                raise ResourceError("catalog_invalid", "DSH 官方安装器摘要或长度无效。")
        return ResourceSpec(
            resource_id=resource_id,
            kind=kind,
            title=title,
            destination=destination,
            version=version,
            source=source,
            files=tuple(files),
            required=raw.get("required") is not False,
            generated_assets=tuple(item for item in raw.get("generated_assets", []) if isinstance(item, dict)),
            legacy_export=legacy_export,
            legacy=False,
        )

    def get(self, resource_id: str) -> ResourceSpec:
        try:
            return self.specs[resource_id]
        except KeyError as exc:
            raise ResourceError("unknown_resource", "资源编号不在当前固定清单中。") from exc


def _safe_relative(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ResourceError("unsafe_path", "资源清单包含无效相对路径。")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or ":" in path.parts[0]:
        raise ResourceError("unsafe_path", "资源路径必须位于资源根目录内。")
    return path


def _under(root: Path, relative: str) -> Path:
    path = root.joinpath(*_safe_relative(relative).parts)
    resolved_root = root.resolve()
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(resolved_root):
        raise ResourceError("unsafe_path", "资源路径指向管理目录之外。")
    return path


def _bundle_stage_dir(task_dir: Path, stage_kind: str, resource_id: str | None = None) -> Path:
    """Return compact ZIP prevalidation paths（返回较短的 ZIP 预校验暂存路径）.

    task_dir（任务目录）、stage_kind（资源暂存类别）和 resource_id（资源编号）均只影响内部暂存位置。
    """
    # ZIP（压缩包）成员可能很深；短目录段避免 Windows MAX_PATH（最大路径）在解包校验时溢出。
    if stage_kind == "model":
        if not isinstance(resource_id, str) or not _ID.fullmatch(resource_id):
            raise ResourceError("unknown_resource", "批量资源暂存目录缺少有效资源编号。")
        return task_dir / "b" / "m" / resource_id
    if stage_kind == "legacy_dsh":
        return task_dir / "b" / "d"
    if stage_kind in {"npm_dsh", "npm_dsh_bundle"}:
        return task_dir / "b" / ("n" if stage_kind == "npm_dsh" else "q")
    if stage_kind == "official_dsh":
        return task_dir / "b" / "o"
    raise ValueError("unknown bundle staging kind（未知批量资源暂存类型）")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _official_runtime_lock_matches(program: Path, artifact: Any, metadata: dict[str, Any],
                                   manifest_sha256: str) -> bool:
    """Check an optional program-published DSH attestation（校验随本体发布的可选信任记录）."""
    program = program.resolve()
    lock_path = program / "config" / "dsh" / "official-runtime.lock.json"
    if not lock_path.is_file():
        return False
    current = lock_path
    while current != program:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            return False
        current = current.parent
    try:
        if not lock_path.resolve().is_relative_to(program):
            return False
        record = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError):
        return False
    if (not isinstance(record, dict)
            or set(record) != {"schema", "version", "source_sha512", "manifest_sha256", "fields"}
            or type(record.get("schema")) is not int or record.get("schema") != 1
            or record.get("fields") != _OFFICIAL_RUNTIME_LOCK_FIELDS):
        return False
    source_sha512 = metadata.get("source_sha512")
    version = metadata.get("version")
    return bool(
        isinstance(artifact.version, str)
        and version == artifact.version == record.get("version")
        and isinstance(source_sha512, str)
        and _SHA512_HEX.fullmatch(source_sha512)
        and source_sha512 == artifact.sha512_hex == record.get("source_sha512")
        and isinstance(manifest_sha256, str)
        and _SHA256.fullmatch(manifest_sha256)
        and record.get("manifest_sha256") == manifest_sha256
    )


def _regular_tree_files(root: Path) -> dict[str, tuple[str, Path, int]]:
    """Enumerate regular files（枚举普通文件）once and reject reparse points（重解析点）."""
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError("资源根目录不能是链接或目录联接。")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("资源根目录不是普通目录。")
    files: dict[str, tuple[str, Path, int]] = {}
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def walk(directory: Path) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            path = Path(entry.path)
            if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & reparse_flag:
                raise ValueError("资源目录包含链接或重解析点。")
            if entry.is_dir(follow_symlinks=False):
                walk(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ValueError("资源目录包含非普通文件。")
            relative = path.relative_to(root).as_posix()
            key = relative.casefold()
            if key in files:
                raise ValueError("资源目录包含大小写重复路径。")
            files[key] = (relative, path, info.st_size)

    walk(root)
    return files


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".resource-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_path(path: Path) -> None:
    """Remove one managed path（删除单个受管路径），unlinking symlinks without following them."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@contextmanager
def _install_lock(root: Path) -> Iterator[None]:
    """Cross-process install lock（跨进程安装锁）：避免两个资源同时改写根清单。"""
    lock_path = root / "tmp" / "resource-distribution.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise ResourceError("resource_busy", "另一个资源任务正在安装，请稍后重试。") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


class ResourceManager:
    """ResourceManager（资源管理器）：仅操作给定 install_root（安装根目录）。"""

    def __init__(self, program: Path, install_root: Path):
        self.program = program.resolve()
        requested_root = install_root.resolve()
        if not (requested_root / "portable.json").is_file() and (requested_root / ".local" / "portable.json").is_file():
            requested_root = requested_root / ".local"
        self.root = requested_root
        self.catalog = ResourceCatalog(self.program)
        try:
            portable = read_portable(self.root)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ResourceError("install_root_invalid", "安装根目录缺少受支持的便携配置。") from exc
        self.models_relative = portable["models"]
        self.dsh_relative = portable["dsh"]
        self.models_root = _under(self.root, self.models_relative)
        self.task_root = self.root / "tmp" / "resource-tasks"
        self.transaction_marker = self.root / "tmp" / "resource-transaction.json"

    def _spec_target(self, spec: ResourceSpec) -> Path:
        rel = f"{self.models_relative}/{spec.destination}" if spec.kind == "model" else f"{self.dsh_relative}/{spec.destination}"
        # Catalog destinations are model-relative; DSH has no catalog file destinations.
        if spec.kind == "dsh":
            rel = self.dsh_relative
        target = _under(self.root, rel)
        if target.is_symlink():
            raise ResourceError("unsafe_path", "资源安装目标不能是符号链接。")
        return target

    def _task_dir(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id) or task_id in {".", ".."}:
            raise ResourceError("invalid_task_id", "任务编号只能包含字母、数字、点、下划线和短横线。")
        return _under(self.task_root, task_id)

    def recover_pending_transaction(self) -> bool:
        """Recover a process-interrupted transaction（恢复被进程中断的事务）。"""
        if not self.transaction_marker.is_file():
            return False
        try:
            marker = json.loads(self.transaction_marker.read_text(encoding="utf-8"))
            tx_dir = _under(self.root, marker["transaction_dir"])
            target = _under(self.root, marker["target"])
            backup = _under(self.root, marker["backup"])
            if marker.get("target_preexisting"):
                if backup.exists():
                    if target.exists():
                        _remove_path(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
            elif target.exists():
                _remove_path(target)
            for record in marker.get("metadata", []):
                metadata_path = _under(self.root, record["path"])
                snapshot = _under(self.root, record["snapshot"])
                if record.get("existed"):
                    if not snapshot.is_file():
                        raise ResourceError("recovery_failed", "资源事务备份不完整，无法安全恢复。")
                    _atomic_write(metadata_path, snapshot.read_bytes())
                else:
                    metadata_path.unlink(missing_ok=True)
            self.transaction_marker.unlink(missing_ok=True)
            shutil.rmtree(tx_dir, ignore_errors=True)
            return True
        except ResourceError:
            raise
        except Exception as exc:
            raise ResourceError("recovery_failed", "发现未完成资源事务，恢复失败。") from exc

    def _read_models_manifest(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        path = self.models_root / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = {"schema": _MANIFEST_SCHEMA, "kind": "models", "build": "", "files": []}
        entries = value.get("files", []) if isinstance(value, dict) else []
        mapping = {entry["path"]: entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
        return value, mapping

    def _model_state(self, spec: ResourceSpec) -> dict[str, Any]:
        target = self._spec_target(spec)
        manifest_path = self.models_root / "manifest.json"
        manifest_error: str | None = None
        entries: dict[str, dict[str, Any]] = {}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if (not isinstance(manifest, dict) or manifest.get("schema") != _MANIFEST_SCHEMA
                    or manifest.get("kind") != "models" or not isinstance(manifest.get("files"), list)):
                manifest_error = "模型安装清单缺失或格式无效。"
            else:
                entries = {item["path"]: item for item in manifest["files"]
                           if isinstance(item, dict) and isinstance(item.get("path"), str)}
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            manifest_error = "模型安装清单无法读取。"
        target_resolved = target.resolve(strict=False)
        target_is_reparse = target.is_symlink() or getattr(target, "is_junction", lambda: False)()
        checks = []
        present = 0
        for item in spec.expected_files:
            path = target.joinpath(*_safe_relative(item.path).parts)
            found = False
            safe = not target_is_reparse
            try:
                safe = safe and path.resolve(strict=False).is_relative_to(target_resolved)
            except (OSError, RuntimeError):
                safe = False
            if safe:
                found = path.is_file() and not path.is_symlink() and not getattr(path, "is_junction", lambda: False)()
            if found:
                present += 1
            rel = f"{spec.destination}/{item.path}"
            record = entries.get(rel)
            valid_record = (isinstance(record, dict) and record.get("size") == item.size_bytes
                            and record.get("sha256") == item.sha256)
            error: str | None = None
            actual_digest: str | None = None
            if not safe:
                error = f"文件路径越出模型目录：{item.path}。"
            elif not found:
                if not item.optional:
                    error = f"缺少必需模型文件：{item.path}。"
            else:
                try:
                    actual_size = path.stat().st_size
                except OSError:
                    actual_size = -1
                if actual_size != item.size_bytes:
                    error = f"文件大小不符：{item.path}（预期 {item.size_bytes} 字节，实际 {max(actual_size, 0)} 字节）。"
                elif manifest_error is not None:
                    error = manifest_error
                elif not valid_record:
                    error = f"模型安装清单与固定资源摘要不符：{item.path}。"
                else:
                    try:
                        actual_digest = _hash_file(path)
                    except OSError:
                        error = f"无法读取模型文件：{item.path}。"
                    else:
                        if actual_digest != item.sha256:
                            error = f"文件 SHA-256（摘要）不符：{item.path}。"
            valid = bool(found and error is None and valid_record and actual_digest == item.sha256)
            checks.append({"path": item.path, "present": bool(found), "valid": valid, "error": error})
        required_ok = all(next(check["valid"] for check in checks if check["path"] == item.path)
                          for item in spec.required_files)
        if required_ok:
            state = "installed"
        elif present:
            state = "incomplete"
        else:
            state = "missing"
        required_present = sum(1 for item in spec.required_files
                               if next(check["valid"] for check in checks if check["path"] == item.path))
        optional_present = sum(1 for item in spec.files if item.optional
                               and next(check["valid"] for check in checks if check["path"] == item.path))
        required_errors = [check["error"] for check in checks if check["path"] in {item.path for item in spec.required_files} and check["error"]]
        optional_errors = [check["error"] for check in checks if check["path"] not in {item.path for item in spec.required_files} and check["error"]]
        from .model_installation import inspect_installed_model, model_update_available
        target_verified = state == "installed"
        installed = inspect_installed_model(self.models_root, spec.destination, spec.resource_id,
                                            verify_hashes=state != "installed")
        update_available = model_update_available(spec, installed)
        if installed.error is None:
            # 已安装内容按自身收据核验；随包目标新增文件不会强迫用户先更新再启动。
            state = "installed"
        elif (target / "resource-install.json").is_file():
            state = "incomplete"
        installed_error = None if installed.error is None else (required_errors or optional_errors or [installed.error])[0]
        return {
            "resource_id": spec.resource_id,
            "kind": spec.kind,
            "title": spec.title,
            "version": spec.version,
            "state": state,
            "installed_version": installed.version,
            "update_available": update_available,
            "files_installed": target_verified,
            "can_start": target_verified and not update_available and installed.error is None,
            "required_files_present": required_present,
            "required_files_total": len(spec.required_files),
            "optional_files_present": optional_present,
            "error": installed_error,
            "installed_path": str(target),
            "source": {key: value for key, value in spec.source.items() if key in {"platform", "repo_id", "revision", "url", "page_url"}},
            "verification_level": "manifest_and_sha256",
            "checks": checks,
            "available_actions": ["download", "import", "open_source"] + (["update"] if update_available else []),
        }

    def _dsh_state(self, spec: ResourceSpec) -> dict[str, Any]:
        if "distribution" in spec.source:
            return self._npm_dsh_state(spec)
        target = self._spec_target(spec)
        version = None
        state = "missing"
        origin = None
        error: str | None = None
        manifest_sha256: str | None = None
        source_sha512: str | None = None
        manifest_path = target / "manifest.json"
        if not target.exists():
            error = "DSH 运行目录尚未安装。"
        elif target.is_symlink() or getattr(target, "is_junction", lambda: False)():
            state = "incomplete"
            error = "DSH 运行目录不能是链接或目录联接。"
        else:
            try:
                runtime = json.loads((target / "runtime.json").read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                runtime = {}
            if isinstance(runtime, dict) and runtime.get("source_kind") == OFFICIAL_SOURCE_KIND:
                state = "incomplete"
                try:
                    paths, manifest_sha = self._inspect_official_dsh_tree(target)
                    version = paths.metadata.get("version")
                    source_sha512 = paths.metadata.get("source_sha512")
                    manifest_sha256 = manifest_sha
                    origin = "official_runtime"
                    state = "installed"
                except ResourceError as exc:
                    error = exc.message
            else:
                state = "incomplete"
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    runtime = json.loads((target / "runtime.json").read_text(encoding="utf-8-sig"))
                    files = manifest.get("files", [])
                    legacy_record = spec.legacy_export or {}
                    if (manifest.get("schema") != 1 or manifest.get("kind") != "dsh" or not files
                            or _hash_file(manifest_path) != legacy_record.get("manifest_sha256")):
                        raise ResourceError("legacy_dsh_invalid", "DSH 兼容运行清单不匹配本项目锁定的旧版本。")
                    actual_files = _regular_tree_files(target)
                    for entry in files:
                        rel = _safe_relative(entry.get("path"))
                        actual = actual_files.get(rel.as_posix().casefold())
                        if actual is None or actual[0] != rel.as_posix():
                            raise ResourceError("legacy_dsh_invalid", f"DSH 兼容运行文件缺失或路径越界：{rel.as_posix()}。")
                        if actual[2] != entry.get("size"):
                            raise ResourceError("legacy_dsh_invalid", f"DSH 兼容运行文件大小不符：{rel.as_posix()}。")
                    state = "installed"
                    version = runtime.get("version")
                    manifest_sha256 = _hash_file(manifest_path)
                    origin = "legacy_runtime"
                except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError, ResourceError) as exc:
                    error = exc.message if isinstance(exc, ResourceError) else "DSH 兼容运行目录的清单或元数据无法读取。"
        return {
            "resource_id": spec.resource_id,
            "kind": "dsh",
            "title": spec.title,
            "version": spec.version,
            "state": state,
            "installed_version": version,
            "origin": origin,
            "error": error,
            "source_sha512": source_sha512,
            "manifest_sha256": manifest_sha256,
            "installed_path": str(target),
            "source": {key: value for key, value in spec.source.items() if key in {"platform", "repo_id", "revision", "url", "page_url"}},
            "verification_level": "official_full_manifest_and_sha256" if origin == "official_runtime" else "manifest_and_size",
            "official_installer_available": True,
            "available_actions": ["download", "import", "open_source"],
        }

    def _npm_dsh_state(self, spec: ResourceSpec) -> dict[str, Any]:
        from .dsh.distribution import inspect_runtime, is_current, discover_node, check_binding
        target = self._spec_target(spec)
        state, metadata, error = "missing", {}, None
        runtime_ready = False
        update = False
        if target.exists():
            try:
                metadata = self._read_runtime_metadata(target)
                if metadata.get("source_kind") == "npm_package":
                    metadata = inspect_runtime(self.program, target)
                    check_binding(target)
                    state = "installed"
                    update = not is_current(metadata, spec.source["distribution"])
                else:
                    # legacy（旧来源）：保留识别入口，不能满足新分发目标。
                    if metadata.get("source_kind") == OFFICIAL_SOURCE_KIND:
                        self._inspect_official_dsh_tree(target)
                    else:
                        self._validate_dsh_tree(target, self.root / "tmp" / "legacy-inspect", create_candidate=False, verify_hashes=False)
                    state = "installed"
                    update = True
            except (OSError, ValueError, KeyError, TypeError, ResourceError) as exc:
                state, error = "incomplete", str(exc)
                update = bool(metadata) and not is_current(metadata, spec.source["distribution"])
        try:
            discover_node(spec.source["distribution"])
            runtime_ready = True
        except ValueError as exc:
            error = error or str(exc)
        if update:
            error = error or "DSH（代理宿主）版本或分发方式需要更新，请点击更新。"
        return {"resource_id": spec.resource_id, "kind": "dsh", "title": spec.title,
                "version": spec.version, "state": state, "installation_state": state,
                "installed_version": metadata.get("version"), "origin": metadata.get("source_kind"),
                "update_available": update, "files_installed": state == "installed",
                "runtime_ready": runtime_ready, "can_start": state == "installed" and runtime_ready and not update,
                "error": error, "manifest_sha256": _hash_file(target / "manifest.json") if (target / "manifest.json").is_file() else None,
                "source_sha512": metadata.get("source_sha512"), "installed_path": str(target),
                "source": spec.source, "verification_level": "installed_manifest", "official_installer_available": False,
                "available_actions": ["download", "import", "open_source"] + (["update"] if update else [])}

    def _stage_npm_dsh_tree(self, source: Path, task_dir: Path) -> Path:
        from .dsh.distribution import inspect_runtime
        inspect_runtime(self.program, source, full=True)
        candidate = _bundle_stage_dir(task_dir, "npm_dsh")
        shutil.copytree(source, candidate, dirs_exist_ok=True)
        inspect_runtime(self.program, candidate, full=True)
        return candidate

    def _stage_npm_dsh_bundle(self, bundle, by_name, entry, task_dir):
        records = entry.get("files")
        if (entry.get("trust_level") != "verified_installed_manifest" or entry.get("destination") != self.dsh_relative
                or not isinstance(records, list) or not records or len(records) > _MAX_BUNDLE_FILES):
            raise ResourceError("bundle_invalid", "npm（包管理器）宿主资源包缺少完整文件记录。")
        stage = _bundle_stage_dir(task_dir, "npm_dsh_bundle")
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                raise ResourceError("bundle_invalid", "DSH（代理宿主）资源包文件记录无效。")
            relative = _safe_relative(record.get("path")).as_posix()
            package_path = _safe_relative(record.get("package_path")).as_posix()
            info = by_name.get(package_path)
            size, sha = record.get("size_bytes"), record.get("sha256")
            if (relative.casefold() in seen or package_path != f"resources/dsh/{relative}"
                    or type(size) is not int or size < 0 or not isinstance(sha, str) or not _SHA256.fullmatch(sha)
                    or info is None or info.is_dir() or info.file_size != size):
                raise ResourceError("bundle_invalid", "DSH（代理宿主）资源包文件或路径不符。")
            seen.add(relative.casefold())
            destination = _under(stage, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._copy_zip_member(bundle, info, destination, size, sha)
        return self._stage_npm_dsh_tree(stage, task_dir)

    def _official_dsh_release_binding(self, manifest: dict[str, Any], manifest_sha256: str,
                                     metadata: dict[str, Any], artifact: Any) -> None:
        """Require a packaged trust lock or this workspace's official DSH binding（发布锁或本机绑定）."""
        if _official_runtime_lock_matches(self.program, artifact, metadata, manifest_sha256):
            return
        try:
            release = json.loads((self.root / "app" / "release.json").read_text(encoding="utf-8-sig"))
            components = release.get("components") if isinstance(release, dict) else None
            binding = next((item for item in components if isinstance(item, dict) and item.get("kind") == "dsh"), None) if isinstance(components, list) else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            binding = None
        if not isinstance(binding, dict):
            raise ResourceError("official_dsh_unattested", "官方 DSH 目录既未匹配本体发布的可信提取记录，也缺少本机安装清单绑定；请使用锁定的官方安装器安装。")
        source_sha512 = metadata.get("source_sha512")
        if (binding.get("path") != self.dsh_relative
                or binding.get("build") != manifest.get("build")
                or binding.get("manifest_sha256") != manifest_sha256
                or metadata.get("version") != artifact.version
                or not isinstance(source_sha512, str)
                or not _SHA512_HEX.fullmatch(source_sha512)
                or source_sha512 != artifact.sha512_hex):
            raise ResourceError("official_dsh_unattested", "官方 DSH 目录与本机程序签发的安装清单或固定安装器摘要不匹配。")

    def _inspect_official_dsh_tree(self, source: Path) -> tuple[Any, str]:
        """Validate official metadata, every manifested byte, and program-issued binding."""
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)() or not source.is_dir():
            raise ResourceError("official_dsh_invalid", "官方 DSH 来源必须是普通运行目录。")
        try:
            artifact = load_official_artifact(self.program)
            paths = validate_official_runtime(source, artifact, verify_required=False)
            verify_component(source, paths.manifest, exact=True)
            manifest_sha256 = _hash_file(source / "manifest.json")
            self._official_dsh_release_binding(paths.manifest, manifest_sha256, paths.metadata, artifact)
        except ResourceError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            message = str(exc)
            if len(message) > 280:
                message = "运行目录完整文件摘要校验失败。"
            raise ResourceError("official_dsh_invalid", f"官方 DSH 运行目录校验失败：{message}") from exc
        return paths, manifest_sha256

    def _stage_official_dsh_tree(self, source: Path, task_dir: Path) -> Path:
        paths, _ = self._inspect_official_dsh_tree(source)
        candidate = task_dir / "verified" / "dsh"
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.mkdir(parents=True, exist_ok=True)
        names = [entry["path"] for entry in paths.manifest["files"]] + ["manifest.json"]
        for name in names:
            rel = _safe_relative(name)
            src = source.joinpath(*rel.parts)
            if not src.is_file() or src.is_symlink() or getattr(src, "is_junction", lambda: False)():
                raise ResourceError("official_dsh_invalid", f"官方 DSH 文件缺失或不是普通文件：{rel.as_posix()}。")
            dst = candidate.joinpath(*rel.parts)
            self._link_or_copy(src, dst)
        try:
            artifact = load_official_artifact(self.program)
            copied_paths = validate_official_runtime(candidate, artifact, verify_required=False)
            verify_component(candidate, copied_paths.manifest, exact=True)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ResourceError("official_dsh_invalid", "官方 DSH 暂存目录完整性校验失败。") from exc
        return candidate

    def status(self, resource_id: str | None = None) -> dict[str, Any]:
        with _install_lock(self.root):
            self.recover_pending_transaction()
            specs = [self.catalog.get(resource_id)] if resource_id else list(self.catalog.specs.values())
            resources = [self._dsh_state(spec) if spec.kind == "dsh" else self._model_state(spec) for spec in specs]
            return {"resources": resources, "fields": {
                "resource_id": "资源稳定编号", "state": "安装状态：missing（缺失）、incomplete（不完整）、installed（已安装）",
                "kind": "资源类别", "title": "资源显示名称", "version": "资源锁定版本",
                "installed_version": "已安装版本；旧模型未登记版本时为空", "origin": "当前资源来源类型",
                "update_available": "模型或宿主须同步至随包目标组合，完成前阻止整体服务启动",
                "files_installed": "随包目标模型的必需文件已校验", "can_start": "模型符合当前版本且无待更新内容",
                "source_sha512": "固定官方安装器 SHA-512（摘要）", "manifest_sha256": "本机程序签发并绑定的运行清单 SHA-256（摘要）",
                "required_files_present": "已通过清单核验的必需文件数", "required_files_total": "必需文件总数",
                "optional_files_present": "已安装的可选文件数", "installed_path": "本机安装路径",
                "verification_level": "日常状态核验级别；manifest_and_size 表示核对固定清单及文件大小，不重算全部文件摘要",
                "checks": "逐文件状态和摘要清单匹配情况",
                "available_actions": "界面可用操作", "official_installer_available": "是否提供官方安装器下载计划",
                "error": "具体缺失或摘要校验错误",
            }}

    def download_plan(self, resource_id: str, task_id: str) -> dict[str, Any]:
        spec = self.catalog.get(resource_id)
        task = self._task_dir(task_id)
        transport = spec.source.get("transfer", {})
        files: list[dict[str, Any]] = []
        if spec.kind == "dsh" and "distribution" in spec.source:
            return {"resource_id": spec.resource_id, "task_id": task_id, "version": spec.version,
                    "kind": "dsh", "transport": "npm_package", "install_strategy": "npm_package",
                    "stage_directory": str(task / "download"), "install_path": str(self._spec_target(spec)),
                    "files": [], "generated_files": [], "source": spec.source,
                    "fields": {"resource_id": "组件编号", "task_id": "任务编号", "version": "目标宿主版本",
                               "kind": "组件类型", "transport": "安装适配器", "install_strategy": "事务安装策略",
                               "stage_directory": "安装暂存目录", "install_path": "安装目录", "files": "由 npm（包管理器）解析下载", "generated_files": "随包资源", "source": "宿主适配声明"}}
        if spec.kind == "dsh":
            artifact = transport.get("artifact", {})
            if not isinstance(artifact, dict) or not isinstance(artifact.get("url"), str):
                raise ResourceError("catalog_invalid", "DSH 固定安装器信息缺失。")
            files.append({
                "role": "official_installer", "url": artifact["url"],
                "destination_path": str(task / "download" / artifact.get("filename", "dsh-installer.exe")),
                "size_bytes": artifact.get("size_bytes"), "hash_algorithm": "sha512",
                "hash_encoding": "base64", "hash_value": artifact.get("sha512_base64"),
                "restartable": True,
            })
            transport_kind = "native_resource_http"
        elif transport.get("kind") == "modelscope_sdk":
            transport_kind = "modelscope_sdk_worker"
            files = [{"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256,
                      "source_path": item.source_path or item.path, "optional": item.optional}
                     for item in spec.files if item.asset is None]
        elif transport.get("kind") == "official_audiocpp_patch":
            # 客户机由原生宿主下载官方归档并应用随包补丁；Python（后端）只投影锁定计划。
            transport_kind = "native_official_audiocpp_patch"
            files = [{"role": "official_archive", "url": item["url"], "size_bytes": item["size_bytes"],
                      "destination_path": str(task / "download" / item["filename"]),
                      "hash_algorithm": "sha256", "hash_encoding": "hex", "hash_value": item["sha256"],
                      "restartable": True} for item in transport["artifacts"]]
        elif transport.get("kind") == "archive_http":
            artifact = transport.get("artifact", {})
            files.append({
                "role": "source_archive", "url": artifact.get("url"),
                "destination_path": str(task / "download" / artifact.get("filename", "resource.tar.bz2")),
                "size_bytes": artifact.get("size_bytes"), "hash_algorithm": "sha256",
                "hash_encoding": "hex", "hash_value": artifact.get("sha256"),
                "restartable": True,
            })
            transport_kind = "native_resource_http"
        else:
            transport_kind = "native_resource_http"
            repo_id = spec.source.get("repo_id")
            revision = spec.source.get("revision")
            if not isinstance(repo_id, str) or not isinstance(revision, str):
                raise ResourceError("catalog_invalid", "固定模型来源缺少仓库或修订号。")
            for item in spec.files:
                if item.asset is not None:
                    continue
                from urllib.parse import quote
                source_path = item.source_path or item.path
                url = f"https://huggingface.co/{repo_id}/resolve/{revision}/{quote(source_path, safe='/')}"
                files.append({
                    "path": item.path, "source_path": source_path, "url": url,
                    "destination_path": str(task / "download" / item.path),
                    "install_path": f"{spec.destination}/{item.path}",
                    "size_bytes": item.size_bytes, "hash_algorithm": "sha256",
                    "hash_encoding": "hex", "hash_value": item.sha256,
                    "optional": item.optional, "restartable": True,
                })
        generated = [{"path": item.path, "asset": item.asset, "size_bytes": item.size_bytes,
                      "sha256": item.sha256} for item in spec.files if item.asset is not None]
        if transport.get("kind") == "official_audiocpp_patch":
            generated.append({**transport["patch"], "role": "bundled_patch"})
        return {
            "resource_id": spec.resource_id,
            "task_id": task_id,
            "version": spec.version,
            "kind": spec.kind,
            "transport": transport_kind,
            "stage_directory": str(task / "download"),
            "files": files,
            "generated_files": generated,
            "install_path": str(self._spec_target(spec)),
            "install_strategy": "native_official_installer" if spec.kind == "dsh" else "verified_transactional_swap",
            "source": {key: value for key, value in spec.source.items() if key in {"platform", "repo_id", "revision", "url", "page_url"}},
            "fields": {
                "source_path": "source_path（上游仓库中的相对文件路径）",
                "resource_id": "资源稳定编号", "task_id": "资源任务编号", "transport": "传输适配器类型",
                "destination_path": "下载暂存文件路径", "install_path": "事务安装目标路径",
                "size_bytes": "预期文件字节数", "sha256": "SHA-256（文件摘要）十六进制值",
                "sha512_base64": "SHA-512（文件摘要）Base64 编码值",
                "version": "资源锁定版本", "kind": "资源类别", "stage_directory": "下载暂存目录",
                "files": "远端需传输的文件清单", "generated_files": "由程序随包提供的生成文件清单",
                "source": "固定平台与仓库来源", "install_strategy": "安装策略标识",
                "url": "固定公开资源地址", "role": "传输对象用途", "path": "来源内相对文件路径",
                "install_path": "资源安装目标目录", "hash_algorithm": "摘要算法名称",
                "hash_encoding": "摘要编码方式", "hash_value": "摘要预期值", "optional": "可选文件标记",
                "restartable": "下载进程退出后的续传能力标记",
            },
        }

    def prepare(self, resource_id: str, task_id: str) -> dict[str, Any]:
        current = self.status(resource_id)["resources"][0]
        plan = self.download_plan(resource_id, task_id)
        applicable = (current["state"] == "installed" and not current.get("update_available", False)
                      and current.get("origin") != "legacy_runtime")
        return {"already_installed": applicable, "current": current, "download_plan": plan,
                "fields": {"already_installed": "固定版本是否已适用于当前安装流程",
                           "current": "当前资源状态", "download_plan": "固定资源传输计划"}}

    def _check_source_file(self, source: Path, spec_file: ResourceFile) -> None:
        if not source.is_file() or source.is_symlink():
            if spec_file.optional:
                return
            raise ResourceError("resource_incomplete", f"所选资源缺少固定清单中的必需文件：{spec_file.path}。")
        actual_size = source.stat().st_size
        if actual_size != spec_file.size_bytes:
            raise ResourceError("resource_mismatch", f"资源文件大小与固定版本不匹配：{spec_file.path}（预期 {spec_file.size_bytes} 字节，实际 {actual_size} 字节）。")
        if _hash_file(source) != spec_file.sha256:
            raise ResourceError("resource_mismatch", f"资源文件 SHA-256（摘要）与固定版本不匹配：{spec_file.path}。")

    def _locate_folder(self, source: Path, spec: ResourceSpec) -> Path:
        if not source.is_dir():
            raise ResourceError("source_invalid", "单资源导入需要选择资源文件夹。")
        required = [item for item in spec.files if item.asset is None and not item.optional]
        candidates = [source, source / spec.destination, source / PurePosixPath(spec.destination).name]
        unique: list[Path] = []
        for candidate in candidates:
            if candidate not in unique and candidate.is_dir() and all(self._source_file(candidate, item).is_file() for item in required):
                unique.append(candidate)
        if len(unique) != 1:
            raise ResourceError("source_invalid", "所选文件夹无法唯一识别为该固定资源。")
        return unique[0]

    @staticmethod
    def _source_file(source_root: Path, item: ResourceFile) -> Path:
        """Resolve a selected file folder using destination path then upstream source_path."""
        paths = [item.path]
        if item.source_path and item.source_path != item.path:
            paths.append(item.source_path)
        for relative in paths:
            candidate = source_root.joinpath(*_safe_relative(relative).parts)
            if candidate.exists():
                if not candidate.resolve(strict=False).is_relative_to(source_root.resolve()):
                    raise ResourceError("unsafe_path", "资源文件路径指向所选目录之外。")
                return candidate
        return source_root.joinpath(*_safe_relative(paths[0]).parts)

    def _materialize_generated(self, candidate: Path, spec: ResourceSpec) -> None:
        for item in spec.files:
            if item.asset is None:
                continue
            asset = _under(self.program, f"config/resources/{item.asset}")
            self._check_source_file(asset, item)
            target = candidate.joinpath(*_safe_relative(item.path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(asset, target)

    def _link_or_copy(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

    def _prepare_candidate(self, source_root: Path, spec: ResourceSpec, task_dir: Path) -> Path:
        candidate = task_dir / "verified" / spec.resource_id
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.mkdir(parents=True, exist_ok=True)
        for item in spec.files:
            destination = candidate.joinpath(*_safe_relative(item.path).parts)
            if item.asset is not None:
                asset = _under(self.program, f"config/resources/{item.asset}")
                self._check_source_file(asset, item)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(asset, destination)
                continue
            source = self._source_file(source_root, item)
            if not source.resolve(strict=False).is_relative_to(source_root.resolve()):
                raise ResourceError("unsafe_path", "资源文件路径指向所选目录之外。")
            if not source.exists() and item.optional:
                continue
            self._check_source_file(source, item)
            self._link_or_copy(source, destination)
        for item in spec.required_files:
            path = candidate.joinpath(*_safe_relative(item.path).parts)
            self._check_source_file(path, item)
        return candidate

    def _model_manifest_after_swap(self, spec: ResourceSpec, candidate: Path) -> dict[str, Any]:
        _, old_records = self._read_models_manifest()
        target_relative = f"{spec.destination}/"
        # 上次签发清单的时间：在此之后被改写的内容不能沿用旧摘要（同长度改写也必须重算）。
        try:
            manifest_time = (self.models_root / "manifest.json").stat().st_mtime_ns
        except OSError:
            manifest_time = None
        records: list[dict[str, Any]] = []
        for file in sorted(path for path in self.models_root.rglob("*")
                           if path.is_file() and path != self.models_root / "manifest.json"):
            if file.is_symlink() or not file.resolve().is_relative_to(self.models_root.resolve()):
                raise ResourceError("unsafe_path", "模型目录包含指向目录之外的文件。")
            rel = file.relative_to(self.models_root).as_posix()
            if rel.startswith(target_relative):
                continue
            prior = old_records.get(rel)
            metadata = file.stat()
            if (isinstance(prior, dict) and prior.get("size") == metadata.st_size
                    and _SHA256.fullmatch(str(prior.get("sha256", "")))
                    and manifest_time is not None and metadata.st_mtime_ns <= manifest_time):
                digest = prior["sha256"]
            else:
                digest = _hash_file(file)
            records.append({"path": rel, "size": metadata.st_size, "sha256": digest})
        for file in sorted(path for path in candidate.rglob("*") if path.is_file()):
            rel = f"{spec.destination}/{file.relative_to(candidate).as_posix()}"
            records.append({"path": rel, "size": file.stat().st_size, "sha256": _hash_file(file)})
        records.sort(key=lambda item: item["path"])
        build = hashlib.sha256(json.dumps(records, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return {"schema": _MANIFEST_SCHEMA, "kind": "models", "build": build, "files": records}

    def _release_after_swap(self, kind: str, manifest: dict[str, Any], manifest_bytes: bytes) -> tuple[str, bytes]:
        path = self.root / "app" / "release.json"
        try:
            release = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResourceError("release_metadata_missing", "安装目录缺少可更新的发行组件清单。") from exc
        components = release.get("components")
        if not isinstance(components, list):
            raise ResourceError("release_metadata_invalid", "发行组件清单格式不受支持。")
        relative_path = "models" if kind == "models" else "runtimes/dsh"
        entry = next((item for item in components if isinstance(item, dict) and item.get("kind") == kind), None)
        if entry is None:
            entry = {"kind": kind, "path": relative_path}
            components.append(entry)
        entry.update({"path": relative_path, "build": manifest.get("build"),
                      "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "optional": True})
        return "app/release.json", _json_bytes(release)

    def _commit_candidate(self, spec: ResourceSpec, candidate: Path, task_id: str) -> None:
        target = self._spec_target(spec)
        with _install_lock(self.root):
            self.recover_pending_transaction()
            if (self.root / "tmp" / "update-transaction.json").exists():
                raise ResourceError("update_in_progress", "应用组件更新期间不能安装资源。")
            if spec.kind == "model":
                from .model_installation import RECEIPT_NAME, model_receipt
                receipt_files = []
                for item in spec.files:
                    path = _under(candidate, item.path)
                    if item.optional and not path.is_file():
                        continue
                    self._check_source_file(path, item)
                    receipt_files.append({"path": item.path, "size": item.size_bytes,
                                          "sha256": item.sha256, "optional": item.optional})
                _atomic_write(candidate / RECEIPT_NAME, _json_bytes(model_receipt(spec.resource_id, spec.version, receipt_files)))
                model_manifest = self._model_manifest_after_swap(spec, candidate)
                model_bytes = _json_bytes(model_manifest)
                release_rel, release_bytes = self._release_after_swap("models", model_manifest, model_bytes)
                metadata = [(f"{self.models_relative}/manifest.json", model_bytes), (release_rel, release_bytes)]
            else:
                manifest_path = candidate / "manifest.json"
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ResourceError("legacy_dsh_invalid", "DSH 兼容资源缺少运行清单。") from exc
                manifest_bytes = manifest_path.read_bytes()
                release_rel, release_bytes = self._release_after_swap("dsh", manifest, manifest_bytes)
                metadata = [(release_rel, release_bytes)]
                runtime = self._read_runtime_metadata(candidate)
                if runtime.get("source_kind") == "npm_package":
                    binding = {key: runtime[key] for key in ("source_kind", "adapter", "version")}
                    binding["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
                    binding["fields"] = {"source_kind": "组件来源", "adapter": "运行适配器", "version": "安装版本", "manifest_sha256": "已验证运行清单摘要"}
                    metadata.append(("data/state/native-dsh-binding.json", _json_bytes(binding)))
            transaction_rel = f"tmp/resource-transactions/{task_id}/{spec.resource_id}"
            tx_dir = _under(self.root, transaction_rel)
            if tx_dir.exists():
                shutil.rmtree(tx_dir)
            tx_dir.mkdir(parents=True, exist_ok=True)
            backup_rel = f"{transaction_rel}/target-backup"
            backup = _under(self.root, backup_rel)
            target_rel = target.relative_to(self.root).as_posix()
            metadata_records = []
            for index, (relative, _) in enumerate(metadata):
                current = _under(self.root, relative)
                snapshot_rel = f"{transaction_rel}/metadata-{index}.bak"
                snapshot = _under(self.root, snapshot_rel)
                existed = current.is_file()
                if existed:
                    shutil.copyfile(current, snapshot)
                metadata_records.append({"path": relative, "snapshot": snapshot_rel, "existed": existed})
            marker = {
                "schema": 1, "task_id": task_id, "resource_id": spec.resource_id,
                "transaction_dir": transaction_rel, "target": target_rel, "backup": backup_rel,
                "target_preexisting": target.exists(), "metadata": metadata_records,
            }
            _atomic_write(self.transaction_marker, _json_bytes(marker))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    os.replace(target, backup)
                os.replace(candidate, target)
                for relative, content in metadata:
                    _atomic_write(_under(self.root, relative), content)
                self.transaction_marker.unlink(missing_ok=True)
                if spec.kind == "dsh":
                    (self.root / "cache/component-validation/dsh.json").unlink(missing_ok=True)
                shutil.rmtree(tx_dir, ignore_errors=True)
            except ResourceError:
                self.recover_pending_transaction()
                raise
            except Exception:
                self.recover_pending_transaction()
                raise ResourceError("install_failed", "资源安装事务失败，已尝试恢复原有资源。")

    def _install_folder(self, spec: ResourceSpec, source: Path, task_id: str) -> list[str]:
        task_dir = self._task_dir(task_id)
        source_root = self._locate_folder(source, spec)
        candidate = self._prepare_candidate(source_root, spec, task_dir)
        self._commit_candidate(spec, candidate, task_id)
        return [spec.resource_id]

    def _extract_kws_archive(self, spec: ResourceSpec, archive: Path, task_dir: Path) -> Path:
        artifact = spec.source.get("transfer", {}).get("artifact", {})
        if archive.stat().st_size != artifact.get("size_bytes") or _hash_file(archive) != artifact.get("sha256"):
            raise ResourceError("resource_mismatch", "唤醒模型压缩包与固定版本不匹配。")
        extract_root = task_dir / "archive-extracted"
        if extract_root.exists():
            shutil.rmtree(extract_root)
        extract_root.mkdir(parents=True, exist_ok=True)
        root_resolved = extract_root.resolve()
        try:
            with tarfile.open(archive, mode="r:bz2") as tar:
                members = tar.getmembers()
                if len(members) > _MAX_BUNDLE_FILES:
                    raise ResourceError("archive_invalid", "模型压缩包包含过多条目。")
                for member in members:
                    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                        raise ResourceError("archive_invalid", "模型压缩包包含不支持的特殊文件。")
                    rel = _safe_relative(member.name.rstrip("/"))
                    target = extract_root.joinpath(*rel.parts)
                    if not target.resolve(strict=False).is_relative_to(root_resolved):
                        raise ResourceError("archive_invalid", "模型压缩包路径越界。")
                tar.extractall(extract_root, filter="data")
        except ResourceError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise ResourceError("archive_invalid", "唤醒模型压缩包无法安全读取。") from exc
        # The official archive contains one model directory; locate it using the locked token file.
        token_candidates = list(extract_root.rglob("tokens.txt"))
        if len(token_candidates) != 1:
            raise ResourceError("archive_invalid", "唤醒模型压缩包目录结构与固定版本不符。")
        source_root = token_candidates[0].parent
        candidate = self._prepare_candidate(source_root, spec, task_dir)
        return candidate

    def _validate_dsh_tree(self, source: Path, task_dir: Path, *,
                           prevalidated_archive_records: dict[str, tuple[int, str]] | None = None,
                           create_candidate: bool = True, verify_hashes: bool = True) -> Path:
        """Validate a legacy DSH tree（校验旧版 DSH 目录）and optionally materialize a candidate（构建候选目录）."""
        if create_candidate and not verify_hashes:
            raise ValueError("候选 DSH（代理宿主）必须执行完整摘要校验。")
        if (not source.is_dir() or source.is_symlink()
                or getattr(source, "is_junction", lambda: False)()):
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容导入需要普通运行目录。")
        try:
            actual_files = _regular_tree_files(source)
        except (OSError, ValueError) as exc:
            raise ResourceError("unsafe_path", "DSH 兼容目录包含链接、重解析点或特殊文件。") from exc
        manifest_file = actual_files.get("manifest.json")
        runtime_file = actual_files.get("runtime.json")
        if (manifest_file is None or manifest_file[0] != "manifest.json"
                or runtime_file is None or runtime_file[0] != "runtime.json"):
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容资源缺少根运行清单或元数据。")
        try:
            manifest_path, runtime_path = manifest_file[1], runtime_file[1]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
            lock = json.loads((self.program / "config" / "dsh" / "runtime.lock.json").read_text(encoding="utf-8-sig"))
            compatibility = self.catalog.get("dsh").source.get("official_compatibility")
            if compatibility:
                lock = compatibility
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容资源缺少有效运行元数据。") from exc
        if (not isinstance(manifest, dict) or not isinstance(runtime, dict) or not isinstance(lock, dict)
                or manifest.get("schema") != 1 or manifest.get("kind") != "dsh"
                or not isinstance(manifest.get("files"), list) or not manifest["files"]
                or runtime.get("version") != lock.get("version") or runtime.get("commit") != lock.get("commit")):
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容资源不匹配本项目当前运行版本。")
        expected_manifest = (self.catalog.get("dsh").legacy_export or {}).get("manifest_sha256")
        if not isinstance(expected_manifest, str) or _hash_file(manifest_path) != expected_manifest:
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容清单不匹配本项目固定的旧运行包。")

        entries: dict[str, dict[str, Any]] = {}
        seen_names: set[str] = set()
        actual_for_entry: dict[str, tuple[str, Path, int]] = {}
        total = 0
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                raise ResourceError("legacy_dsh_invalid", "DSH 兼容运行清单格式不正确。")
            rel = _safe_relative(entry.get("path"))
            name = rel.as_posix()
            key = name.casefold()
            size = entry.get("size")
            sha = entry.get("sha256")
            if key in seen_names or not isinstance(size, int) or size < 0 or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
                raise ResourceError("legacy_dsh_invalid", "DSH 兼容运行清单包含重复或无效文件记录。")
            seen_names.add(key)
            actual = actual_files.get(key)
            if actual is None or actual[0] != name:
                raise ResourceError("legacy_dsh_invalid", f"DSH 兼容运行文件缺失或路径大小写不匹配：{name}。")
            if actual[2] != size:
                raise ResourceError("legacy_dsh_invalid", f"DSH 兼容运行文件大小不符：{name}。")
            entries[name] = entry
            actual_for_entry[name] = actual
            total += size
        if total > _MAX_BUNDLE_BYTES:
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容资源超过本地导入大小限制。")
        if set(actual[0] for actual in actual_files.values()) != {*entries, "manifest.json"}:
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容运行文件集合不匹配固定清单。")

        if prevalidated_archive_records is None and verify_hashes:
            def verify_entry(item: tuple[str, dict[str, Any], Path]) -> None:
                name, entry, path = item
                if _hash_file(path) != entry["sha256"]:
                    raise ResourceError("legacy_dsh_invalid", f"DSH 兼容运行文件摘要不匹配：{name}。")

            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="dsh-legacy-verify") as workers:
                for _ in workers.map(verify_entry, ((name, entry, actual_for_entry[name][1])
                                                    for name, entry in entries.items())):
                    pass
        elif prevalidated_archive_records is not None:
            archive_files = {name: digest for name, digest in prevalidated_archive_records.items()
                             if name != "manifest.json"}
            expected_files = {name: (entry["size"], entry["sha256"]) for name, entry in entries.items()}
            if archive_files != expected_files:
                raise ResourceError("legacy_dsh_invalid", "归档中逐文件摘要与固定 DSH manifest（运行清单）不一致。")
            manifest_record = prevalidated_archive_records.get("manifest.json")
            if (manifest_record != (manifest_file[2], expected_manifest)
                    or set(actual[0] for actual in actual_files.values()) != set(prevalidated_archive_records)):
                raise ResourceError("legacy_dsh_invalid", "归档中的 DSH 文件集合或根清单摘要不一致。")

        if not create_candidate:
            return source
        candidate = task_dir / "verified" / "dsh"
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.mkdir(parents=True, exist_ok=True)
        for name in [*entries, "manifest.json"]:
            src = (manifest_file[1] if name == "manifest.json" else actual_for_entry[name][1])
            dst = candidate.joinpath(*_safe_relative(name).parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._link_or_copy(src, dst)
        return candidate

    @staticmethod
    def _read_runtime_metadata(source: Path) -> dict[str, Any]:
        try:
            value = json.loads((source / "runtime.json").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _bundle_import(self, archive: Path, task_id: str, *, commit: bool = True) -> dict[str, Any]:
        task_dir = self._task_dir(task_id)
        try:
            package = zipfile.ZipFile(archive)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ResourceError("bundle_invalid", "资源压缩包无法读取。") from exc
        with package as bundle:
            infos = bundle.infolist()
            if len(infos) > _MAX_BUNDLE_FILES:
                raise ResourceError("bundle_invalid", "资源压缩包包含过多条目。")
            by_name: dict[str, zipfile.ZipInfo] = {}
            case_names: set[str] = set()
            total = 0
            for info in infos:
                canonical_name = info.filename.rstrip("/")
                if canonical_name.casefold() in case_names:
                    raise ResourceError("bundle_invalid", "资源压缩包包含重复路径。")
                case_names.add(canonical_name.casefold())
                if canonical_name == "resources.json":
                    by_name[canonical_name] = info
                    continue
                rel = _safe_relative(canonical_name)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK or info.flag_bits & 0x1:
                    raise ResourceError("bundle_invalid", "资源压缩包包含链接或加密条目。")
                total += info.file_size
                if total > _MAX_BUNDLE_BYTES:
                    raise ResourceError("bundle_invalid", "资源压缩包超过本地导入大小限制。")
                by_name[rel.as_posix()] = info
            index_info = by_name.get("resources.json")
            if index_info is None or index_info.is_dir() or index_info.file_size > _MAX_BUNDLE_INDEX_BYTES:
                raise ResourceError("bundle_invalid", "资源压缩包缺少有效 resources.json（资源清单）。")
            try:
                index = json.loads(bundle.read(index_info).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ResourceError("bundle_invalid", "资源压缩包中的 resources.json 格式不正确。") from exc
            if not isinstance(index, dict) or index.get("schema_version") != 1 or not isinstance(index.get("resources"), list):
                raise ResourceError("bundle_invalid", "资源压缩包格式版本不受支持。")
            if len(index["resources"]) > 128:
                raise ResourceError("bundle_invalid", "资源压缩包包含过多资源条目。")
            staged: list[tuple[str, Path, dict[str, Any]]] = []
            referenced_members: set[str] = set()
            seen: set[str] = set()
            for entry in index["resources"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("resource_id"), str):
                    raise ResourceError("bundle_invalid", "资源压缩包条目格式不正确。")
                resource_id = entry["resource_id"]
                if resource_id in seen:
                    raise ResourceError("bundle_invalid", "资源压缩包包含重复资源。")
                seen.add(resource_id)
                if resource_id == "dsh" and entry.get("origin") in {"legacy_runtime", "official_runtime", "npm_runtime"}:
                    dsh_records = entry.get("files")
                    if not isinstance(dsh_records, list) or len(dsh_records) > _MAX_BUNDLE_FILES:
                        raise ResourceError("bundle_invalid", "DSH 兼容资源文件清单格式不正确。")
                    for record in dsh_records:
                        if not isinstance(record, dict) or not isinstance(record.get("package_path"), str):
                            raise ResourceError("bundle_invalid", "DSH 兼容资源文件记录格式不正确。")
                        referenced_members.add(_safe_relative(record["package_path"]).as_posix())
                    if entry.get("origin") == "npm_runtime":
                        dsh = self._stage_npm_dsh_bundle(bundle, by_name, entry, task_dir)
                    elif entry.get("origin") == "official_runtime":
                        dsh = self._stage_official_dsh_bundle(bundle, by_name, entry, task_dir)
                    else:
                        dsh = self._stage_dsh_bundle(bundle, by_name, entry, task_dir)
                    staged.append(("dsh", dsh, entry))
                    continue
                spec = self.catalog.get(resource_id)
                if spec.kind != "model":
                    raise ResourceError("bundle_invalid", "资源压缩包的资源类型与当前固定清单不符。")
                if entry.get("version") != spec.version or entry.get("destination") != spec.destination:
                    raise ResourceError("bundle_invalid", "资源压缩包版本与当前固定清单不一致。")
                files_raw = entry.get("files")
                if not isinstance(files_raw, list) or len(files_raw) > _MAX_BUNDLE_FILES:
                    raise ResourceError("bundle_invalid", "资源压缩包缺少文件清单。")
                listed: dict[str, dict[str, Any]] = {}
                for record in files_raw:
                    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                        raise ResourceError("bundle_invalid", "资源文件记录格式不正确。")
                    rel = _safe_relative(record["path"]).as_posix()
                    if rel in listed:
                        raise ResourceError("bundle_invalid", "资源文件清单包含重复路径。")
                    listed[rel] = record
                required = {item.path for item in spec.required_files}
                allowed = {item.path for item in spec.files}
                if not required.issubset(listed) or not set(listed).issubset(allowed):
                    raise ResourceError("bundle_invalid", "资源压缩包文件不符合固定白名单。")
                stage = _bundle_stage_dir(task_dir, "model", resource_id)
                if stage.exists():
                    shutil.rmtree(stage)
                stage.mkdir(parents=True, exist_ok=True)
                for rel, record in listed.items():
                    spec_file = next(item for item in spec.files if item.path == rel)
                    if record.get("size_bytes") != spec_file.size_bytes or record.get("sha256") != spec_file.sha256:
                        raise ResourceError("bundle_invalid", "资源压缩包摘要与固定清单不匹配。")
                    package_path = _safe_relative(record.get("package_path"))
                    if package_path.as_posix() != f"resources/{resource_id}/{rel}":
                        raise ResourceError("bundle_invalid", "资源包内文件路径与资源编号不匹配。")
                    info = by_name.get(package_path.as_posix())
                    if info is None or info.is_dir() or info.file_size != spec_file.size_bytes:
                        raise ResourceError("bundle_invalid", "资源压缩包缺少固定文件或文件长度不匹配。")
                    referenced_members.add(package_path.as_posix())
                    destination = stage.joinpath(*_safe_relative(rel).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._copy_zip_member(bundle, info, destination, spec_file.size_bytes, spec_file.sha256)
                # Project-generated assets come from the trusted bundled catalog assets.
                candidate = self._prepare_candidate(stage, spec, task_dir)
                staged.append((resource_id, candidate, entry))
            if not staged:
                raise ResourceError("bundle_invalid", "资源压缩包没有可安装资源。")
            bundle_files = {name for name, info in by_name.items() if name != "resources.json" and not info.is_dir()}
            if bundle_files != referenced_members:
                raise ResourceError("bundle_invalid", "资源压缩包包含未列入 resources.json 的文件。")
        # All bundle members have been validated before the first target is changed.
        if not commit:
            already_installed = [resource_id for resource_id, candidate, _ in staged
                                 if self._same_installed_resource(self.catalog.get(resource_id), candidate)]
            return {"valid": True, "resource_ids": [item[0] for item in staged],
                    "validated_resource_count": len(staged), "already_installed_resource_ids": already_installed,
                    "fields": {"valid": "是否完成全包预校验", "resource_ids": "预校验通过的资源编号",
                               "validated_resource_count": "预校验资源数", "already_installed_resource_ids": "当前完整摘要已匹配、可跳过的资源编号"}}
        installed: list[str] = []
        skipped: list[str] = []
        try:
            for resource_id, candidate, entry in staged:
                spec = self.catalog.get(resource_id)
                if self._same_installed_resource(spec, candidate):
                    skipped.append(resource_id)
                    continue
                if resource_id == "dsh" and entry.get("origin") == "legacy_runtime":
                    candidate = self._materialize_legacy_dsh_candidate(candidate, task_dir)
                self._commit_candidate(spec, candidate, task_id)
                installed.append(resource_id)
        except ResourceError as exc:
            raise ResourceError(exc.code, exc.message, details={"installed_resource_ids": installed,
                                                               "skipped_resources": skipped}) from exc
        shutil.rmtree(task_dir, ignore_errors=True)
        return {"installed_resource_ids": installed, "skipped_resources": skipped,
                "fields": {"installed_resource_ids": "本次事务安装的资源编号数组",
                           "skipped_resources": "全部固定文件摘要已与当前安装匹配、未重复替换的资源编号数组"}}

    def _materialize_legacy_dsh_candidate(self, source: Path, task_dir: Path) -> Path:
        """Materialize a DSH candidate（构建代理运行候选目录）after validation and duplicate-skip checks（重复跳过检查）."""
        try:
            manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容暂存目录缺少有效运行清单。") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise ResourceError("legacy_dsh_invalid", "DSH 兼容暂存目录清单格式不正确。")
        candidate = task_dir / "verified" / "dsh"
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.mkdir(parents=True, exist_ok=True)
        names = []
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                raise ResourceError("legacy_dsh_invalid", "DSH 兼容暂存目录清单格式不正确。")
            names.append(entry.get("path"))
        names.append("manifest.json")
        for name in names:
            rel = _safe_relative(name)
            src = source.joinpath(*rel.parts)
            if not src.is_file() or src.is_symlink() or getattr(src, "is_junction", lambda: False)():
                raise ResourceError("legacy_dsh_invalid", "DSH 兼容暂存文件缺失或不是普通文件。")
            dst = candidate.joinpath(*rel.parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._link_or_copy(src, dst)
        return candidate

    def _same_installed_resource(self, spec: ResourceSpec, candidate: Path) -> bool:
        """Skip only when the destination already passes full locked integrity checks."""
        if spec.kind == "model":
            current = self._model_state(spec)
            return current["state"] == "installed" and not current.get("update_available", False)
        current = self._dsh_state(spec)
        if current["state"] != "installed":
            return False
        try:
            target = self._spec_target(spec)
            if _hash_file(candidate / "manifest.json") != current.get("manifest_sha256"):
                return False
            if current.get("origin") == "legacy_runtime":
                self._validate_dsh_tree(target, candidate.parent, create_candidate=False)
            return True
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError, ResourceError):
            return False

    def validate_bundle(self, archive: Path, task_id: str) -> dict[str, Any]:
        """Validate a ZIP64 package（验证 ZIP64 资源包）without changing installed resources."""
        result = self._bundle_import(archive.expanduser().resolve(), task_id, commit=False)
        shutil.rmtree(self._task_dir(task_id), ignore_errors=True)
        return result

    def preview_source(self, source: Path, *, resource_id: str | None, task_id: str) -> dict[str, Any]:
        """Prevalidate a source（安装前校验来源）without changing installed resources."""
        source = source.expanduser().resolve()
        if not source.exists() or source.is_symlink():
            raise ResourceError("source_invalid", "选择的资源路径不存在或不是普通文件。")
        if source.is_file() and zipfile.is_zipfile(source):
            try:
                with zipfile.ZipFile(source) as bundle:
                    has_index = any(item.filename.rstrip("/") == "resources.json" for item in bundle.infolist())
            except (OSError, zipfile.BadZipFile) as exc:
                raise ResourceError("bundle_invalid", "资源压缩包无法读取。") from exc
            if not has_index:
                raise ResourceError("bundle_invalid", "ZIP（资源压缩包）缺少 resources.json（资源清单）。")
            if resource_id is not None:
                raise ResourceError("bundle_invalid", "批量资源包会自动识别资源编号，请勿指定单一资源。")
            return self.validate_bundle(source, task_id)
        if not resource_id:
            raise ResourceError("resource_required", "单资源预校验必须指定资源编号。")

        spec = self.catalog.get(resource_id)
        task_dir = self._task_dir(task_id)
        try:
            if spec.kind == "dsh":
                if not source.is_dir():
                    raise ResourceError("legacy_dsh_invalid", "官方 DSH 安装器应由固定安装器处理。")
                runtime_value = self._read_runtime_metadata(source)
                if runtime_value.get("source_kind") == "npm_package":
                    self._stage_npm_dsh_tree(source, task_dir)
                elif runtime_value.get("source_kind") == OFFICIAL_SOURCE_KIND:
                    self._stage_official_dsh_tree(source, task_dir)
                else:
                    self._validate_dsh_tree(source, task_dir, create_candidate=False)
            elif source.is_file() and source.suffix.casefold() in {".tar", ".bz2", ".tbz", ".tbz2"}:
                if resource_id != "kws-sherpa-onnx":
                    raise ResourceError("source_invalid", "该模型不支持此压缩包格式。")
                self._extract_kws_archive(spec, source, task_dir)
            elif source.is_dir():
                source_root = self._locate_folder(source, spec)
                for item in spec.files:
                    if item.asset is not None:
                        self._check_source_file(_under(self.program, f"config/resources/{item.asset}"), item)
                    else:
                        self._check_source_file(self._source_file(source_root, item), item)
            else:
                raise ResourceError("source_invalid", "单资源导入需要模型文件夹或锁定的唤醒模型压缩包。")
            return {"valid": True, "resource_ids": [resource_id], "validated_resource_count": 1,
                    "fields": {"valid": "是否完成资源预校验", "resource_ids": "预校验通过的资源编号",
                               "validated_resource_count": "预校验资源数"}}
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

    def _copy_zip_member(self, bundle: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path,
                         expected_size: int, expected_sha256: str) -> None:
        digest = hashlib.sha256()
        count = 0
        with bundle.open(info, "r") as source, destination.open("wb") as target:
            while block := source.read(1024 * 1024):
                count += len(block)
                if count > expected_size:
                    raise ResourceError("bundle_invalid", "资源压缩包成员超过清单长度。")
                digest.update(block)
                target.write(block)
        if count != expected_size or digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise ResourceError("bundle_invalid", "资源压缩包文件摘要校验失败。")

    def _stage_dsh_bundle(self, bundle: zipfile.ZipFile, by_name: dict[str, zipfile.ZipInfo],
                          entry: dict[str, Any], task_dir: Path) -> Path:
        if entry.get("trust_level") != "legacy_compatibility_only":
            raise ResourceError("bundle_invalid", "DSH 兼容资源必须显式标注来源级别。")
        records = entry.get("files")
        if not isinstance(records, list) or not records:
            raise ResourceError("bundle_invalid", "DSH 兼容资源缺少文件清单。")
        stage = _bundle_stage_dir(task_dir, "legacy_dsh")
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True, exist_ok=True)
        allowed_names = set()
        seen_paths: set[str] = set()
        prevalidated_records: dict[str, tuple[int, str]] = {}
        for record in records:
            rel = _safe_relative(record.get("path"))
            if rel.as_posix().casefold() in seen_paths:
                raise ResourceError("bundle_invalid", "DSH 兼容资源包含重复文件路径。")
            seen_paths.add(rel.as_posix().casefold())
            size = record.get("size_bytes")
            sha = record.get("sha256")
            package_path = _safe_relative(record.get("package_path"))
            info = by_name.get(package_path.as_posix())
            if (not isinstance(size, int) or size < 0 or not isinstance(sha, str) or not _SHA256.fullmatch(sha)
                    or package_path.as_posix() != f"resources/dsh/{rel.as_posix()}"
                    or info is None or info.is_dir() or info.file_size != size):
                raise ResourceError("bundle_invalid", "DSH 兼容资源文件记录无效。")
            target = stage.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._copy_zip_member(bundle, info, target, size, sha)
            allowed_names.add(rel.as_posix())
            prevalidated_records[rel.as_posix()] = (size, sha)
        manifest_name = "manifest.json"
        if manifest_name not in allowed_names:
            raise ResourceError("bundle_invalid", "DSH 兼容资源缺少根运行清单。")
        return self._validate_dsh_tree(stage, task_dir,
                                       prevalidated_archive_records=prevalidated_records,
                                       create_candidate=False)

    def _stage_official_dsh_bundle(self, bundle: zipfile.ZipFile, by_name: dict[str, zipfile.ZipInfo],
                                   entry: dict[str, Any], task_dir: Path) -> Path:
        """Stage a ZIP only when its official tree matches this program's attestation."""
        try:
            artifact = load_official_artifact(self.program)
        except (OSError, ValueError, TypeError) as exc:
            raise ResourceError("catalog_invalid", "官方 DSH 固定安装器信息不可用。") from exc
        if (entry.get("trust_level") != "program_attested_official_runtime"
                or entry.get("version") != artifact.version
                or entry.get("destination") != self.dsh_relative
                or entry.get("source_installer_sha512") != artifact.sha512_hex
                or not isinstance(entry.get("manifest_sha256"), str)
                or not _SHA256.fullmatch(entry["manifest_sha256"])):
            raise ResourceError("bundle_invalid", "官方 DSH 资源包缺少程序签发的固定安装器与完整清单绑定。")
        records = entry.get("files")
        if not isinstance(records, list) or not records or len(records) > _MAX_BUNDLE_FILES:
            raise ResourceError("bundle_invalid", "官方 DSH 资源包缺少完整文件清单。")
        stage = _bundle_stage_dir(task_dir, "official_dsh")
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True, exist_ok=True)
        allowed_names: set[str] = set()
        seen_paths: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ResourceError("bundle_invalid", "官方 DSH 文件清单记录格式无效。")
            rel = _safe_relative(record.get("path"))
            rel_name = rel.as_posix()
            package_path = _safe_relative(record.get("package_path"))
            size = record.get("size_bytes")
            sha256 = record.get("sha256")
            expected_package_path = f"resources/dsh/{rel_name}"
            info = by_name.get(package_path.as_posix())
            if (rel_name.casefold() in seen_paths or package_path.as_posix() != expected_package_path
                    or type(size) is not int or size < 0 or not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)
                    or info is None or info.is_dir() or info.file_size != size):
                raise ResourceError("bundle_invalid", f"官方 DSH 文件记录无效：{rel_name}。")
            seen_paths.add(rel_name.casefold())
            allowed_names.add(rel_name)
            target = stage.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._copy_zip_member(bundle, info, target, size, sha256)
        if "manifest.json" not in seen_paths:
            raise ResourceError("bundle_invalid", "官方 DSH 资源包缺少根运行清单。")
        try:
            _, manifest_sha256 = self._inspect_official_dsh_tree(stage)
        except ResourceError as exc:
            raise ResourceError(exc.code, exc.message) from exc
        if manifest_sha256 != entry["manifest_sha256"]:
            raise ResourceError("bundle_invalid", "官方 DSH 资源包的程序签发清单摘要不匹配。")
        return self._stage_official_dsh_tree(stage, task_dir)

    def import_source(self, source: Path, *, resource_id: str | None, task_id: str) -> dict[str, Any]:
        source = source.expanduser().resolve()
        if not source.exists() or source.is_symlink():
            raise ResourceError("source_invalid", "选择的资源路径不存在或不是普通文件。")
        if source.is_file() and zipfile.is_zipfile(source):
            try:
                with zipfile.ZipFile(source) as archive:
                    has_index = any(name == "resources.json" for name in archive.namelist())
            except (OSError, zipfile.BadZipFile):
                has_index = False
            if has_index:
                if resource_id is not None:
                    raise ResourceError("bundle_invalid", "批量资源包会自动识别资源编号，请勿指定单一资源。")
                return self._bundle_import(source, task_id)
        if not resource_id:
            raise ResourceError("resource_required", "单资源导入必须指定资源编号。")
        spec = self.catalog.get(resource_id)
        task_dir = self._task_dir(task_id)
        if spec.kind == "dsh":
            if not source.is_dir():
                raise ResourceError("legacy_dsh_invalid", "官方 DSH 安装器必须交由原生安装器处理；此入口只接收已验证运行目录。")
            runtime_value = self._read_runtime_metadata(source)
            if runtime_value.get("source_kind") == "npm_package":
                candidate = self._stage_npm_dsh_tree(source, task_dir)
                origin = "npm_runtime"
            elif runtime_value.get("source_kind") == OFFICIAL_SOURCE_KIND:
                candidate = self._stage_official_dsh_tree(source, task_dir)
                origin = "official_runtime"
            else:
                candidate = self._validate_dsh_tree(source, task_dir)
                origin = "legacy_runtime"
            if self._same_installed_resource(spec, candidate):
                shutil.rmtree(task_dir, ignore_errors=True)
                return {"installed_resource_ids": [], "skipped_resources": [resource_id], "origin": origin,
                        "fields": {"installed_resource_ids": "本次事务实际安装的资源编号数组",
                                   "skipped_resources": "摘要已完全匹配、未重复替换的资源编号数组",
                                   "origin": "资源来源类型"}}
            self._commit_candidate(spec, candidate, task_id)
            shutil.rmtree(task_dir, ignore_errors=True)
            return {"installed_resource_ids": [resource_id], "skipped_resources": [], "origin": origin,
                    "fields": {"installed_resource_ids": "成功安装的资源编号数组", "origin": "资源来源类型",
                               "skipped_resources": "摘要已完全匹配、未重复替换的资源编号数组"}}
        if source.is_file() and source.suffix.casefold() in {".tar", ".bz2", ".tbz", ".tbz2"}:
            if resource_id != "kws-sherpa-onnx":
                raise ResourceError("source_invalid", "该模型不支持此压缩包格式。")
            candidate = self._extract_kws_archive(spec, source, task_dir)
            if self._same_installed_resource(spec, candidate):
                shutil.rmtree(task_dir, ignore_errors=True)
                return {"installed_resource_ids": [], "skipped_resources": [resource_id],
                        "fields": {"installed_resource_ids": "本次事务实际安装的资源编号数组",
                                   "skipped_resources": "摘要已完全匹配、未重复替换的资源编号数组"}}
            self._commit_candidate(spec, candidate, task_id)
            shutil.rmtree(task_dir, ignore_errors=True)
            return {"installed_resource_ids": [resource_id], "skipped_resources": [],
                    "fields": {"installed_resource_ids": "成功安装的资源编号数组",
                               "skipped_resources": "摘要已完全匹配、未重复替换的资源编号数组"}}
        if not source.is_dir():
            raise ResourceError("source_invalid", "单模型导入需要模型文件夹或锁定的唤醒模型压缩包。")
        source_root = self._locate_folder(source, spec)
        for item in spec.files:
            if item.asset is not None:
                self._check_source_file(_under(self.program, f"config/resources/{item.asset}"), item)
            else:
                self._check_source_file(self._source_file(source_root, item), item)
        current = self._model_state(spec)
        if current["state"] == "installed" and not current.get("update_available", False):
            shutil.rmtree(task_dir, ignore_errors=True)
            return {"installed_resource_ids": [], "skipped_resources": [resource_id],
                    "fields": {"installed_resource_ids": "本次事务实际安装的资源编号数组",
                               "skipped_resources": "摘要已完全匹配、未重复替换的资源编号数组"}}
        installed = self._install_folder(spec, source, task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
        return {"installed_resource_ids": installed, "skipped_resources": [],
                "fields": {"installed_resource_ids": "成功安装的资源编号数组",
                           "skipped_resources": "摘要已完全匹配、未重复替换的资源编号数组"}}

    def download_modelscope(self, resource_id: str, task_id: str, *, download_only: bool = False) -> dict[str, Any]:
        spec = self.catalog.get(resource_id)
        if spec.kind != "model" or spec.source.get("transfer", {}).get("kind") != "modelscope_sdk":
            raise ResourceError("wrong_platform", "该资源不使用 ModelScope SDK 下载。")
        task_dir = self._task_dir(task_id)
        download_dir = task_dir / "modelscope-download"
        download_dir.mkdir(parents=True, exist_ok=True)
        total_bytes = sum(item.size_bytes for item in spec.files if not item.optional and item.asset is None)
        tracker = _ModelScopeProgress(total_bytes)
        _emit_progress("starting", 0, total_bytes)
        try:
            from modelscope_hub import HubApi, HubConfig, ProgressCallback
        except ImportError as exc:
            raise ResourceError("sdk_unavailable", "随包 ModelScope SDK 不可用。") from exc
        cache_dir = task_dir / "modelscope-cache"
        config_dir = task_dir / "modelscope-config"
        cache_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        tracker_cls = tracker.callback_class(ProgressCallback)
        previous_log_disable = logging.root.manager.disable
        try:
            config = HubConfig(endpoint="https://modelscope.cn", token="", cache_dir=cache_dir, config_dir=config_dir)
            api = HubApi(config=config)
            logging.disable(logging.CRITICAL)
            with redirect_stdout(sys.stderr):
                downloaded = api.download_repo(
                    spec.source["repo_id"], "model", revision=spec.source["revision"],
                    local_dir=download_dir,
                    allow_patterns=[item.source_path or item.path for item in spec.files if item.asset is None],
                    max_workers=3,
                    progress_callbacks=[tracker_cls],
                    user_agent={"app": "voice-terminal-resource-manager"},
                )
        except ResourceError:
            raise
        except Exception as exc:
            raise ResourceError("download_failed", "ModelScope 资源下载失败；可使用同一任务编号重试，临时文件已保留。") from exc
        finally:
            logging.disable(previous_log_disable)
        _emit_progress("verifying", tracker.completed, total_bytes)
        source_root = Path(downloaded).resolve()
        # SDK result paths are constrained to the task's local_dir; reject cache or URL results.
        if not source_root.is_relative_to(download_dir.resolve()):
            raise ResourceError("download_failed", "ModelScope SDK 返回路径不在本次临时目录内。")
        candidate = self._prepare_candidate(source_root, spec, task_dir)
        if download_only:
            _emit_progress("ready", total_bytes, total_bytes)
            return {"staged_path": str(download_dir.resolve()), "resource_id": resource_id, "version": spec.version,
                    "can_resume_after_process_exit": True,
                    "fields": {"staged_path": "已校验资源暂存目录，等待原生宿主暂停服务后提交安装",
                               "resource_id": "资源稳定编号", "version": "资源固定版本",
                               "can_resume_after_process_exit": "进程退出后能否用相同任务编号恢复临时下载"}}
        _emit_progress("installing", total_bytes, total_bytes)
        self._commit_candidate(spec, candidate, task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
        _emit_progress("complete", total_bytes, total_bytes)
        return {"installed_resource_ids": [resource_id], "version": spec.version,
                "can_resume_after_process_exit": True,
                "pause_supported": False, "cancel_supported": False,
                "fields": {"installed_resource_ids": "成功安装的资源编号数组", "version": "已安装的固定资源版本",
                           "can_resume_after_process_exit": "进程退出后能否用相同任务编号恢复临时下载",
                           "pause_supported": "当前任务是否提供精确暂停接口",
                           "cancel_supported": "当前任务是否提供平台级取消接口"}}


class _ModelScopeProgress:
    """ModelScope progress adapter（魔搭进度适配器）：将逐文件增量合并成任务总字节数。"""

    def __init__(self, total: int):
        self.total = total
        self.completed = 0
        self.last_emit = 0.0
        self.lock = threading.Lock()

    def callback_class(self, base: type):
        tracker = self

        class Callback(base):
            def __init__(self, filename: str, file_size: int):
                super().__init__(filename, file_size)
                self.reported = 0

            def update(self, size: int) -> None:
                with tracker.lock:
                    previous = self.reported
                    self.reported = min(max(0, int(self.file_size)), previous + max(0, int(size)))
                    tracker.completed = min(tracker.total, tracker.completed + self.reported - previous)
                    now = time.monotonic()
                    if now - tracker.last_emit >= 0.2 or tracker.completed >= tracker.total:
                        tracker.last_emit = now
                        _emit_progress("downloading", tracker.completed, tracker.total)

            def end(self) -> None:
                return None

        return Callback


_PROGRESS_LOCK = threading.Lock()
_JSON_STDOUT = sys.stdout


def _emit_progress(phase: str, completed: int, total: int) -> None:
    record = {"phase": phase, "completed_bytes": max(0, int(completed)), "total_bytes": max(0, int(total))}
    with _PROGRESS_LOCK:
        _JSON_STDOUT.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        _JSON_STDOUT.flush()


def make_manager(program: Path, install_root: Path) -> ResourceManager:
    return ResourceManager(program, install_root)


__all__ = ["ResourceCatalog", "ResourceError", "ResourceFile", "ResourceManager", "ResourceSpec", "make_manager"]

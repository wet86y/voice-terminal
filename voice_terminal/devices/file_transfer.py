"""SFTP staging（安全文件传输暂存）：设备与核心之间的有界、摘要校验文件中转。"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import threading
import time
from uuid import uuid4
from typing import Any


MAX_CHUNK_BYTES = 64 * 1024  # MAX_CHUNK_BYTES（控制 API 每次读写暂存文件的字节上限）。
MAX_TRANSFERS = 64  # MAX_TRANSFERS（单核心运行可登记的并行文件传输上限）。
MAX_TRANSFER_BYTES = 4 * 1024 * 1024 * 1024  # MAX_TRANSFER_BYTES（单文件 SFTP 传输上限，字节）。
MAX_STAGING_BYTES = 8 * 1024 * 1024 * 1024  # MAX_STAGING_BYTES（同一核心并行暂存内容的总预留上限）。
TRANSFER_TTL_SECONDS = 60 * 60  # TRANSFER_TTL_SECONDS（传输租约空闲失效时限）。
_TRANSFER_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DeviceFileTransferError(RuntimeError):
    """DeviceFileTransferError（设备文件传输错误）：不把未知 SFTP 结果解释为未执行。"""


@dataclass
class _Transfer:
    transfer_id: str
    device_id: str
    connection_generation: int
    executor_generation: int
    path: Path
    partial_path: Path
    direction: str
    state: str
    bytes: int = 0
    sha256: str | None = None
    expected_bytes: int | None = None
    expected_sha256: str | None = None
    target_path: str | None = None
    modified_ns: int | None = None
    touched_at: float = 0.0
    chunk_receipts: dict[int, tuple[int, str, int]] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "device_id": self.device_id,
            "connection_generation": self.connection_generation,
            "executor_generation": self.executor_generation,
            "state": self.state,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "expected_bytes": self.expected_bytes,
            "expected_sha256": self.expected_sha256,
            "target_path": self.target_path,
            "fields": {
                "transfer_id": "本次 SFTP 文件传输编号",
                "device_id": "固定执行设备编号",
                "connection_generation": "设备 SSH 连接代次",
                "executor_generation": "设备执行器代次",
                "state": "暂存与摘要复核状态",
                "bytes": "已核验原始字节数",
                "sha256": "已核验原始字节 SHA-256 摘要",
                "expected_bytes": "在登记时锁定的文件字节数",
                "expected_sha256": "在登记时锁定的预期 SHA-256 摘要",
            },
        }


class DeviceFileTransferStore:
    """DeviceFileTransferStore（核心文件暂存）：路径由核心固定，设备只收取一次性暂存路径。"""

    def __init__(self, runtime_data_dir: Path, *, staging_root: Path | None = None,
                 preserve_staged_files: bool = False) -> None:
        data_root = Path(runtime_data_dir).absolute()
        configured = os.environ.get("VOICE_DEVICE_FILE_STAGING_ROOT")
        self.root = Path(staging_root or configured or (data_root / "device-file-staging")).absolute()
        self.preserve_staged_files = bool(preserve_staged_files or os.environ.get("VOICE_DEVICE_FILE_STAGING_PRESERVE") == "1")
        self._lock = threading.RLock()
        self._records: dict[str, _Transfer] = {}

    def prepare_device_upload(self, *, device_id: str, connection_generation: int,
                              executor_generation: int, expected_bytes: int,
                              transfer_id: str | None = None) -> dict[str, Any]:
        """为设备 -> 核心的大文件读取预留唯一 SFTP（安全文件传输）落点。"""
        record = self._new(device_id, connection_generation, executor_generation, "device_upload",
                           transfer_id, expected_bytes=expected_bytes)
        return {**record.snapshot(), "sftp_path": str(record.path),
                "fields": {**record.snapshot()["fields"], "sftp_path": "核心签发的唯一 SFTP 上传落点；模型不可指定"}}

    def prepare_local_upload(self, *, device_id: str, connection_generation: int,
                             executor_generation: int, expected_bytes: int,
                             transfer_id: str | None = None) -> dict[str, Any]:
        """为本机设备的大文件读取预留核心暂存项；不经过网络或 PCM 控制通道。"""
        record = self._new(device_id, connection_generation, executor_generation, "local_upload",
                           transfer_id, expected_bytes=expected_bytes)
        return record.snapshot()

    def prepare_core_upload(self, *, device_id: str, connection_generation: int,
                            executor_generation: int, expected_bytes: int, expected_sha256: str,
                            target_path: str,
                            transfer_id: str | None = None) -> dict[str, Any]:
        """为 DSH -> 设备的文件写入预留本机附件流暂存。"""
        record = self._new(device_id, connection_generation, executor_generation, "core_upload",
                           transfer_id, expected_bytes=expected_bytes, expected_sha256=expected_sha256,
                           target_path=target_path)
        return record.snapshot()

    def stage_local_file(self, *, transfer_id: str, device_id: str, connection_generation: int,
                         executor_generation: int, source_path: str | Path) -> dict[str, Any]:
        """将获准的本机文件流式复制到核心暂存，并核对读取前后的文件身份。"""
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "local_upload":
                raise DeviceFileTransferError("文件暂存租约不接受本机文件读取。")
            if record.state == "ready":
                self._verify_ready_file(record)
                return record.snapshot()
            if record.state != "waiting_device":
                raise DeviceFileTransferError("本机文件暂存租约状态无效。")
            source = self._safe_regular_file(source_path)
            try:
                before = source.stat()
                self._ensure_root()
                if self._is_reparse(record.partial_path) or record.partial_path.exists():
                    raise DeviceFileTransferError("本机文件暂存临时路径已被占用。")
                digest = hashlib.sha256()
                total = 0
                with source.open("rb") as reader, record.partial_path.open("xb") as writer:
                    while True:
                        chunk = reader.read(MAX_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > record.expected_bytes or total > MAX_TRANSFER_BYTES:
                            raise DeviceFileTransferError("本机文件读取期间长度超过已登记上限。")
                        digest.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                after = source.stat()
                if (total != record.expected_bytes or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns or before.st_ino != after.st_ino):
                    raise DeviceFileTransferError("本机文件读取期间内容、长度或文件身份发生变化。")
                actual_bytes, actual_sha = self._digest(record.partial_path)
                if actual_bytes != total or actual_sha != digest.hexdigest():
                    raise DeviceFileTransferError("本机文件暂存副本的长度或摘要不匹配。")
                if self._is_reparse(record.path) or record.path.exists():
                    raise DeviceFileTransferError("本机文件暂存最终路径已被占用。")
                record.partial_path.rename(record.path)
                stat = record.path.stat()
                record.bytes, record.sha256, record.modified_ns = actual_bytes, actual_sha, stat.st_mtime_ns
                record.state, record.touched_at = "ready", time.monotonic()
                return record.snapshot()
            except OSError as exc:
                raise DeviceFileTransferError("本机文件暂存读取或提交失败。") from exc

    def commit_local_file(self, *, transfer_id: str, device_id: str, connection_generation: int,
                          executor_generation: int, target_path: str | Path) -> dict[str, Any]:
        """将已校验附件原子创建到本机用户目标；拒绝覆盖已有文件。"""
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "core_upload":
                raise DeviceFileTransferError("文件暂存租约不接受本机文件写入。")
            target = Path(target_path)
            if str(target) != record.target_path:
                raise DeviceFileTransferError("本机文件写入目标与一次性审批时的路径不一致。")
            if record.state == "committed":
                self._safe_directory(target.parent)
                existing_bytes, existing_sha = self._digest(target)
                if existing_bytes == record.bytes and existing_sha == record.sha256:
                    return {"path": str(target), "bytes": existing_bytes, "sha256": existing_sha,
                            "duplicate": True, "fields": {"path": "本机已存在且摘要匹配的原任务目标",
                                "bytes": "已写入并复核的字节数", "sha256": "与 DSH 附件一致的 SHA-256 摘要",
                                "duplicate": "相同原请求的幂等完成回执"}}
                raise DeviceFileTransferError("已完成文件写入目标在复查时发生变化。")
            self._verify_ready_file(record)
            if not target.is_absolute() or target.name in {"", ".", ".."}:
                raise DeviceFileTransferError("本机文件写入目标必须是绝对文件路径。")
            parent = self._safe_directory(target.parent)
            if self._is_reparse(target):
                raise DeviceFileTransferError("本机文件写入目标是链接，拒绝覆盖。")
            if target.exists():
                existing_bytes, existing_sha = self._digest(target)
                if existing_bytes == record.bytes and existing_sha == record.sha256:
                    return {"path": str(target), "bytes": existing_bytes, "sha256": existing_sha,
                            "duplicate": True, "fields": {"path": "本机已存在且摘要匹配的原任务目标",
                                "bytes": "已写入并复核的字节数", "sha256": "与 DSH 附件一致的 SHA-256 摘要",
                                "duplicate": "相同原请求的幂等完成回执"}}
                raise DeviceFileTransferError("本机文件写入目标已存在，拒绝覆盖。")
            temporary = parent / f".{target.name}.{transfer_id}.part"
            if self._is_reparse(temporary) or temporary.exists():
                raise DeviceFileTransferError("本机文件写入临时项已存在。")
            digest = hashlib.sha256()
            total = 0
            try:
                with record.path.open("rb") as reader, temporary.open("xb") as writer:
                    while True:
                        chunk = reader.read(MAX_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        digest.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                if total != record.bytes or digest.hexdigest() != record.sha256:
                    raise DeviceFileTransferError("本机文件写入前的暂存摘要复核失败。")
                if self._is_reparse(parent) or self._is_reparse(target) or target.exists():
                    raise DeviceFileTransferError("本机文件写入目标在提交期间已被占用或改变。")
                # Windows rename 在目标存在时失败；其他平台用原子硬链接确保不覆盖。
                if os.name == "nt":
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
                    temporary.unlink()
                record.state = "committed"
                return {"path": str(target), "bytes": total, "sha256": record.sha256,
                        "fields": {"path": "本机已新建的目标文件路径", "bytes": "已写入并复核的字节数",
                                   "sha256": "与 DSH 附件一致的 SHA-256 摘要"}}
            except DeviceFileTransferError:
                try:
                    if temporary.is_file() and not self._is_reparse(temporary):
                        temporary.unlink()
                except OSError:
                    pass
                raise
            except OSError as exc:
                try:
                    if temporary.is_file() and not self._is_reparse(temporary):
                        temporary.unlink()
                except OSError:
                    pass
                raise DeviceFileTransferError("本机文件写入提交失败；目标不会被覆盖。") from exc

    def append_core_chunk(self, *, transfer_id: str, device_id: str, connection_generation: int,
                          executor_generation: int, offset: int, data_base64: str) -> dict[str, Any]:
        """按顺序写入附件流分块；重复或越序分块失败关闭。"""
        if type(offset) is not int or offset < 0 or not isinstance(data_base64, str):
            raise DeviceFileTransferError("文件暂存分块偏移或编码无效。")
        try:
            raw = base64.b64decode(data_base64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise DeviceFileTransferError("文件暂存分块 Base64 编码无效。") from exc
        if not raw or len(raw) > MAX_CHUNK_BYTES or base64.b64encode(raw).decode("ascii") != data_base64:
            raise DeviceFileTransferError("文件暂存分块为空、超限或不是规范 Base64。")
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "core_upload":
                raise DeviceFileTransferError("文件暂存租约不接受核心上传分块。")
            chunk_digest = hashlib.sha256(raw).hexdigest()
            previous = record.chunk_receipts.get(offset)
            if previous is not None:
                if previous[:2] != (len(raw), chunk_digest):
                    raise DeviceFileTransferError("同一附件分块偏移收到冲突内容。")
                record.touched_at = time.monotonic()
                return {"transfer_id": transfer_id, "next_offset": previous[2],
                        "fields": {"transfer_id": "原文件传输编号", "next_offset": "下一分块所需字节偏移"}}
            if (record.state != "receiving" or offset != record.bytes
                    or record.bytes + len(raw) > MAX_TRANSFER_BYTES
                    or record.bytes + len(raw) > (record.expected_bytes or 0)):
                raise DeviceFileTransferError("文件暂存分块重复、越序或超过总长度上限。")
            self._ensure_root()
            if self._is_reparse(record.partial_path):
                raise DeviceFileTransferError("文件暂存分块路径不能是链接。")
            if not record.partial_path.exists():
                with record.partial_path.open("xb"):
                    pass
            try:
                with record.partial_path.open("ab") as handle:
                    handle.write(raw)
                    handle.flush()
            except OSError as exc:
                raise DeviceFileTransferError("文件暂存分块写入失败。") from exc
            record.bytes += len(raw)
            record.chunk_receipts[offset] = (len(raw), chunk_digest, record.bytes)
            record.touched_at = time.monotonic()
            return {"transfer_id": transfer_id, "next_offset": record.bytes,
                    "fields": {"transfer_id": "原文件传输编号", "next_offset": "下一分块所需字节偏移"}}

    def finish_core_upload(self, *, transfer_id: str, device_id: str, connection_generation: int,
                           executor_generation: int, expected_bytes: int, expected_sha256: str) -> dict[str, Any]:
        """完成核心附件暂存并核验完整长度、摘要后才允许设备下载。"""
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "core_upload":
                raise DeviceFileTransferError("文件暂存租约不能完成核心上传。")
            if record.state == "ready":
                self._verify_ready_file(record)
                if record.bytes != expected_bytes or record.sha256 != expected_sha256:
                    raise DeviceFileTransferError("重复的附件流完成回执与原文件摘要不一致。")
                return record.snapshot()
            if record.state != "receiving":
                raise DeviceFileTransferError("核心文件暂存租约状态无效。")
            self._verify_common(record, expected_bytes, expected_sha256)
            if not record.partial_path.is_file():
                raise DeviceFileTransferError("文件暂存上传没有收到内容。")
            try:
                with record.partial_path.open("ab") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise DeviceFileTransferError("文件暂存内容无法持久化。") from exc
            actual_bytes, actual_sha = self._digest(record.partial_path)
            if actual_bytes != expected_bytes or actual_sha != expected_sha256:
                raise DeviceFileTransferError("文件附件流分块完成后的长度或 SHA-256 摘要不匹配。")
            if self._is_reparse(record.path) or record.path.exists():
                raise DeviceFileTransferError("文件暂存最终路径已被占用，拒绝覆盖。")
            record.partial_path.rename(record.path)
            stat = record.path.stat()
            record.bytes, record.sha256, record.modified_ns = actual_bytes, actual_sha, stat.st_mtime_ns
            record.state = "ready"
            return record.snapshot()

    def verify_device_upload(self, *, transfer_id: str, device_id: str, connection_generation: int,
                             executor_generation: int, expected_bytes: int, expected_sha256: str) -> dict[str, Any]:
        """确认设备 SFTP 上传已提交，且内容摘要与设备回执一致。"""
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "device_upload":
                raise DeviceFileTransferError("文件暂存租约不接受设备上传回执。")
            if record.state == "ready":
                self._verify_ready_file(record)
                if record.bytes != expected_bytes or record.sha256 != expected_sha256:
                    raise DeviceFileTransferError("重复的设备文件回执与原传输摘要不一致。")
                return record.snapshot()
            if record.state != "waiting_device":
                raise DeviceFileTransferError("设备文件传输租约状态无效。")
            self._verify_common(record, expected_bytes, expected_sha256)
            if not record.path.is_file():
                raise DeviceFileTransferError("设备没有提交 SFTP 暂存文件。")
            actual_bytes, actual_sha = self._digest(record.path)
            if actual_bytes != expected_bytes or actual_sha != expected_sha256:
                raise DeviceFileTransferError("设备 SFTP 暂存文件长度或 SHA-256 摘要不匹配。")
            stat = record.path.stat()
            record.bytes, record.sha256, record.modified_ns = actual_bytes, actual_sha, stat.st_mtime_ns
            record.state, record.touched_at = "ready", time.monotonic()
            return record.snapshot()

    def read_chunk(self, *, transfer_id: str, device_id: str, connection_generation: int,
                   executor_generation: int, offset: int, limit: int = MAX_CHUNK_BYTES) -> dict[str, Any]:
        """从已复核暂存文件读取一个有界附件分块。"""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_CHUNK_BYTES:
            raise DeviceFileTransferError("文件暂存读取偏移或分块上限无效。")
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            self._verify_ready_file(record)
            if offset > record.bytes:
                raise DeviceFileTransferError("文件暂存读取偏移超过文件长度。")
            with record.path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(min(limit, record.bytes - offset))
            record.touched_at = time.monotonic()
            return {"transfer_id": transfer_id, "offset": offset, "bytes": len(chunk),
                    "total_bytes": record.bytes, "sha256": record.sha256,
                    "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
                    "data_base64": base64.b64encode(chunk).decode("ascii"),
                    "fields": {"transfer_id": "原文件传输编号", "offset": "本分块的原文件字节偏移",
                        "bytes": "本分块字节数", "total_bytes": "完整文件字节数", "sha256": "完整文件摘要",
                        "chunk_sha256": "当前分块摘要", "data_base64": "规范 Base64 文件分块"}}

    def device_download_ticket(self, *, transfer_id: str, device_id: str,
                               connection_generation: int, executor_generation: int) -> dict[str, Any]:
        """为设备下载返回已经摘要复核的核心路径和获批目标。"""
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "core_upload":
                raise DeviceFileTransferError("文件暂存租约不是等待设备下载的核心附件。")
            self._verify_ready_file(record)
            return {"sftp_path": str(record.path), "target_path": record.target_path,
                    "bytes": record.bytes, "sha256": record.sha256,
                    "fields": {"sftp_path": "核心签发的已校验 SFTP 下载源路径",
                        "target_path": "一次性审批绑定的设备目标路径", "bytes": "已校验原始字节数",
                        "sha256": "文件内容 SHA-256 摘要"}}

    def close(self, *, transfer_id: str, device_id: str, connection_generation: int,
              executor_generation: int) -> bool:
        """关闭传输租约；验收保留策略启用时保留现场文件。"""
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            self._records.pop(transfer_id, None)
            if self.preserve_staged_files:
                return True
            for path in (record.partial_path, record.path, Path(f"{record.path}.{record.transfer_id}.part")):
                try:
                    if path.is_file() and not self._is_reparse(path):
                        path.unlink()
                except OSError:
                    return False
            return True

    def _new(self, device_id: str, connection_generation: int,
             executor_generation: int, direction: str, transfer_id: str | None = None, *,
             expected_bytes: int, expected_sha256: str | None = None,
             target_path: str | None = None) -> _Transfer:
        if (not isinstance(device_id, str) or not device_id or len(device_id) > 128
                or type(connection_generation) is not int or connection_generation < 1
                or type(executor_generation) is not int or executor_generation < 1
                or type(expected_bytes) is not int or not 0 <= expected_bytes <= MAX_TRANSFER_BYTES
                or (expected_sha256 is not None and (not isinstance(expected_sha256, str)
                    or not _SHA256.fullmatch(expected_sha256)))
                or (direction == "core_upload" and (expected_sha256 is None
                    or not isinstance(target_path, str) or not target_path or len(target_path) > 4096))):
            raise DeviceFileTransferError("文件传输目标或设备代次无效。")
        self._expire()
        with self._lock:
            transfer_id = transfer_id or uuid4().hex
            if not isinstance(transfer_id, str) or not _TRANSFER_ID.fullmatch(transfer_id):
                raise DeviceFileTransferError("核心文件传输编号格式无效。")
            existing = self._records.get(transfer_id)
            if existing is not None:
                if (existing.device_id != device_id or existing.connection_generation != connection_generation
                        or existing.executor_generation != executor_generation or existing.direction != direction
                        or existing.expected_bytes != expected_bytes or existing.expected_sha256 != expected_sha256
                        or existing.target_path != target_path):
                    raise DeviceFileTransferError("原文件传输编号已绑定其他设备、代次或方向。")
                existing.touched_at = time.monotonic()
                return existing
            if len(self._records) >= MAX_TRANSFERS:
                raise DeviceFileTransferError("核心文件传输并行数已达上限。")
            reserved = sum(item.expected_bytes or 0 for item in self._records.values())
            if reserved + expected_bytes > MAX_STAGING_BYTES:
                raise DeviceFileTransferError("核心并行文件暂存预留容量已达上限。")
            self._ensure_root()
            path = self.root / f"{transfer_id}.blob"
            partial = self.root / f".{transfer_id}.partial"
            sftp_partial = Path(f"{path}.{transfer_id}.part")
            if path.exists() or partial.exists() or sftp_partial.exists():
                raise DeviceFileTransferError("文件传输编号冲突。")
            record = _Transfer(transfer_id, device_id, connection_generation, executor_generation,
                               path, partial, direction,
                               "waiting_device" if direction in {"device_upload", "local_upload"} else "receiving",
                               expected_bytes=expected_bytes, expected_sha256=expected_sha256,
                               target_path=target_path,
                               touched_at=time.monotonic())
            if direction == "core_upload" and expected_bytes == 0:
                with partial.open("xb"):
                    pass
            self._records[transfer_id] = record
            return record

    def _get(self, transfer_id: str, device_id: str, connection_generation: int,
             executor_generation: int) -> _Transfer:
        if not isinstance(transfer_id, str) or not _TRANSFER_ID.fullmatch(transfer_id):
            raise DeviceFileTransferError("文件传输编号无效。")
        record = self._records.get(transfer_id)
        if (record is None or record.device_id != device_id
                or record.connection_generation != connection_generation
                or record.executor_generation != executor_generation
                or time.monotonic() - record.touched_at >= TRANSFER_TTL_SECONDS):
            raise DeviceFileTransferError("文件传输租约不存在、已过期或设备代次已变化。")
        return record

    def _ensure_root(self) -> None:
        root = self.root
        current = Path(root.anchor)
        for component in root.parts[1:]:
            current = current / component
            if self._is_reparse(current):
                raise DeviceFileTransferError("文件暂存路径不能经过链接或目录联接。")
        root.mkdir(parents=True, exist_ok=True)
        if self._is_reparse(root) or not root.is_dir():
            raise DeviceFileTransferError("文件暂存根不是安全目录。")

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())

    @classmethod
    def _safe_regular_file(cls, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute() or any(part in {".", ".."} for part in candidate.parts):
            raise DeviceFileTransferError("本机文件路径必须绝对且不能包含 . 或 .. 路径段。")
        current = Path(candidate.anchor)
        for component in candidate.parts[1:]:
            current = current / component
            if cls._is_reparse(current):
                raise DeviceFileTransferError("本机文件路径经过链接或目录联接。")
        try:
            metadata = candidate.stat()
        except OSError as exc:
            raise DeviceFileTransferError("本机获准文件不可读取。") from exc
        if cls._is_reparse(candidate) or not candidate.is_file():
            raise DeviceFileTransferError("本机获准读取目标必须是普通文件。")
        return candidate

    @classmethod
    def _safe_directory(cls, path: Path) -> Path:
        if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
            raise DeviceFileTransferError("本机文件写入父目录路径无效。")
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current = current / component
            if cls._is_reparse(current):
                raise DeviceFileTransferError("本机文件写入父目录经过链接或目录联接。")
        try:
            if not path.is_dir():
                raise DeviceFileTransferError("本机文件写入父路径不是现存目录。")
        except OSError as exc:
            raise DeviceFileTransferError("本机文件写入父目录无法读取。") from exc
        return path

    @staticmethod
    def _digest(path: Path) -> tuple[int, str]:
        if DeviceFileTransferStore._is_reparse(path) or not path.is_file():
            raise DeviceFileTransferError("暂存对象必须是普通文件。")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_TRANSFER_BYTES:
                        raise DeviceFileTransferError("暂存文件超过 SFTP 总长度上限。")
                    digest.update(chunk)
        except OSError as exc:
            raise DeviceFileTransferError("暂存文件无法完整读取。") from exc
        return size, digest.hexdigest()

    @staticmethod
    def _verify_common(record: _Transfer, expected_bytes: int, expected_sha256: str) -> None:
        if (type(expected_bytes) is not int or not 0 <= expected_bytes <= MAX_TRANSFER_BYTES
                or not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256)):
            raise DeviceFileTransferError("文件传输长度或 SHA-256 摘要格式无效。")
        if record.expected_bytes != expected_bytes or (record.expected_sha256 is not None
                and record.expected_sha256 != expected_sha256):
            raise DeviceFileTransferError("文件传输完成声明与登记时锁定的附件不一致。")
        if record.direction == "core_upload" and record.bytes != expected_bytes:
            raise DeviceFileTransferError("文件附件分块总长度与声明不一致。")

    def _verify_ready_file(self, record: _Transfer) -> None:
        if record.state != "ready" or record.sha256 is None or record.modified_ns is None:
            raise DeviceFileTransferError("文件传输内容尚未完成摘要复核。")
        try:
            stat = record.path.stat()
        except OSError as exc:
            raise DeviceFileTransferError("已复核文件暂存项不可读取。") from exc
        if (self._is_reparse(record.path) or stat.st_size != record.bytes
                or stat.st_mtime_ns != record.modified_ns):
            raise DeviceFileTransferError("已复核文件暂存项在读取期间发生变化。")

    def _expire(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [key for key, item in self._records.items() if now - item.touched_at >= TRANSFER_TTL_SECONDS]
            for key in expired:
                record = self._records.pop(key)
                if not self.preserve_staged_files:
                    for path in (record.partial_path, record.path, Path(f"{record.path}.{record.transfer_id}.part")):
                        try:
                            if path.is_file() and not self._is_reparse(path):
                                path.unlink()
                        except OSError:
                            pass

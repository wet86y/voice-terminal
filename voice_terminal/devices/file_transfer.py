"""Device file staging（设备文件暂存）：设备与核心之间的有界、摘要校验文件中转。"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import secrets
import threading
import time
from uuid import uuid4
from typing import Any


MAX_CHUNK_BYTES = 64 * 1024  # MAX_CHUNK_BYTES（控制 API 每次读写暂存文件的字节上限）。
MAX_TRANSFERS = 64  # MAX_TRANSFERS（单核心运行可登记的并行文件传输上限）。
MAX_TRANSFER_BYTES = 4 * 1024 * 1024 * 1024  # MAX_TRANSFER_BYTES（单文件设备文件传输上限，字节）。
MAX_STAGING_BYTES = 8 * 1024 * 1024 * 1024  # MAX_STAGING_BYTES（同一核心并行暂存内容的总预留上限）。
TRANSFER_TTL_SECONDS = 60 * 60  # TRANSFER_TTL_SECONDS（传输租约空闲失效时限）。
FILE_CHANNEL_TTL_SECONDS = 15 * 60  # FILE_CHANNEL_TTL_SECONDS（受限文件会话令牌空闲失效时限）。
_TRANSFER_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DeviceFileTransferError(RuntimeError):
    """DeviceFileTransferError（设备文件传输错误）：不把未知任务结果解释为未执行。"""


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
    channel_token: str | None = None  # channel_token（受限 FILE 会话的短时能力令牌）。
    channel_operation: str | None = None  # channel_operation（该 FILE 会话获准执行的固定操作）。
    channel_touched_at: float = 0.0  # channel_touched_at（最近一次 FILE 能力令牌活动时间）。
    channel_completed: bool = False  # channel_completed（该 FILE 会话是否已完成长度与摘要复核）。
    channel_next_offset: int = 0  # channel_next_offset（下载 FILE 会话中下一个顺序读取偏移）。
    channel_chunk_receipts: dict[int, tuple[int, str, int]] = field(default_factory=dict)  # channel_chunk_receipts（下载块的幂等收据）。
    tts_import_ticket: dict[str, Any] | None = None  # tts_import_ticket（可选的受管 TTS 单文件导入票据绑定）。
    tts_import_committed: bool = False  # tts_import_committed（VOICE 库已按本票据持久化导入结果）。

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
                "transfer_id": "本次设备文件传输编号",
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
        self._closed_tts_imports: dict[str, tuple[str, int, int, str, float]] = {}

    def prepare_device_upload(self, *, device_id: str, connection_generation: int,
                              executor_generation: int, expected_bytes: int,
                              transfer_id: str | None = None) -> dict[str, Any]:
        """为设备到核心的大文件读取预留既有文件传输票据。"""
        record = self._new(device_id, connection_generation, executor_generation, "device_upload",
                           transfer_id, expected_bytes=expected_bytes)
        if record.tts_import_ticket is not None:
            raise DeviceFileTransferError("TTS 音频导入票据不能复用为 DSH 文件读取票据。")
        return record.snapshot()

    def prepare_tts_asset_import(self, *, transfer_id: str, device_id: str,
                                 connection_generation: int, executor_generation: int,
                                 parent_request_id: str, model_id: str, purpose: str,
                                 filename: str, expected_bytes: int) -> dict[str, Any]:
        """为受管 TTS（语音合成）参考音频创建原 DFT 设备上传票据。"""
        from ..tts.voices import ALLOWED_IMPORT_EXTENSIONS, MAX_IMPORT_BYTES
        from ..tts.settings import valid_local_model
        if (not isinstance(parent_request_id, str) or not 1 <= len(parent_request_id) <= 128
                or any(not (character.isascii() and (character.isalnum() or character in "._:-"))
                       for character in parent_request_id)
                or not valid_local_model(model_id)
                or purpose not in {"asset", "source", "source_and_asset"}
                or not isinstance(filename, str) or not filename or len(filename) > 240
                or filename in {".", ".."} or Path(filename).name != filename
                or "/" in filename or "\\" in filename or any(ord(character) < 32 for character in filename)
                or Path(filename).suffix.lower() not in ALLOWED_IMPORT_EXTENSIONS
                or type(expected_bytes) is not int or not 1 <= expected_bytes <= MAX_IMPORT_BYTES):
            raise DeviceFileTransferError("TTS 参考音频票据的模型、用途、文件名或大小无效。")
        metadata = {"transfer_id": transfer_id, "device_id": device_id,
            "connection_generation": connection_generation, "executor_generation": executor_generation,
            "parent_request_id": parent_request_id, "model_id": model_id,
            "purpose": purpose, "filename": filename, "expected_bytes": expected_bytes}
        self._expire()
        with self._lock:
            existing = self._records.get(transfer_id)
            if existing is not None and existing.tts_import_ticket is None:
                raise DeviceFileTransferError("已有普通 DSH 文件票据不能改绑为 TTS 音频导入。")
            record = self._new(device_id, connection_generation, executor_generation,
                "device_upload", transfer_id, expected_bytes=expected_bytes)
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "device_upload" or record.expected_bytes != expected_bytes:
                raise DeviceFileTransferError("TTS 导入票据与原设备上传票据冲突。")
            if record.tts_import_ticket is not None and record.tts_import_ticket != metadata:
                raise DeviceFileTransferError("原 transfer_id 已绑定另一模型、用途、文件名或设备请求。")
            if record.tts_import_ticket is None:
                if record.state != "waiting_device" or record.bytes != 0:
                    raise DeviceFileTransferError("已经开始的普通设备传输不能改作 TTS 导入。")
                record.tts_import_ticket = metadata
            record.touched_at = time.monotonic()
            return {"transfer_id": transfer_id, "parent_request_id": parent_request_id,
                    "expected_bytes": expected_bytes, "total_bytes": expected_bytes,
                    "connection_generation": connection_generation,
                    "executor_generation": executor_generation, "maximum_bytes": expected_bytes,
                    "fields": {"transfer_id": "由设备、代次和 parent_request_id 确定的 DFT 票据编号",
                        "parent_request_id": "稳定的本机音频导入请求编号", "expected_bytes": "已审批音频文件长度",
                        "total_bytes": "已审批音频文件长度",
                        "connection_generation": "原生设备连接代次", "executor_generation": "设备执行器代次",
                        "maximum_bytes": "音频导入允许的最大字节数"}}

    def read_tts_asset_import(self, *, transfer_id: str, device_id: str,
                              connection_generation: int, executor_generation: int,
                              parent_request_id: str, model_id: str, purpose: str,
                              filename: str, expected_bytes: int,
                              expected_sha256: str) -> tuple[bytes, dict[str, Any]]:
        """读取仅已完成 DFT 校验的 TTS 上传字节，并复核原票据的全部元数据。"""
        expected = {"transfer_id": transfer_id, "device_id": device_id,
            "connection_generation": connection_generation, "executor_generation": executor_generation,
            "parent_request_id": parent_request_id, "model_id": model_id,
            "purpose": purpose, "filename": filename, "expected_bytes": expected_bytes}
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "device_upload" or record.tts_import_ticket != expected:
                raise DeviceFileTransferError("TTS commit 请求与预留 DFT 票据字段不匹配。")
            if record.state != "ready":
                raise DeviceFileTransferError("TTS 参考音频还没有完成设备上传。")
            self._verify_ready_file(record)
            if (record.bytes != expected_bytes or record.sha256 != expected_sha256
                    or not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256)):
                raise DeviceFileTransferError("TTS 参考音频长度或 SHA-256 与 DFT 票据不匹配。")
            try:
                data = record.path.read_bytes()
            except OSError as exc:
                raise DeviceFileTransferError("已核验 TTS 音频暂存文件无法读取。") from exc
            if len(data) != expected_bytes or hashlib.sha256(data).hexdigest() != expected_sha256:
                raise DeviceFileTransferError("TTS 音频暂存内容在提交前发生变化。")
            record.touched_at = time.monotonic()
            binding = dict(expected)
            binding["expected_sha256"] = expected_sha256
            return data, binding

    def mark_tts_asset_import_committed(self, *, transfer_id: str, device_id: str,
                                       connection_generation: int, executor_generation: int,
                                       metadata: dict[str, Any]) -> None:
        """标记原 DFT TTS 票据已由 VoiceLibrary 原子持久化。"""
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.tts_import_ticket != metadata or record.state != "ready":
                raise DeviceFileTransferError("TTS 导入提交结果无法绑定回原 DFT 票据。")
            record.tts_import_committed = True
            record.touched_at = time.monotonic()

    def abort_tts_asset_import(self, *, transfer_id: str, device_id: str,
                               connection_generation: int, executor_generation: int,
                               parent_request_id: str) -> bool:
        """回滚未提交的 TTS 上传暂存；已入库票据不会删除音频素材。"""
        with self._lock:
            record = self._records.get(transfer_id)
            if record is None:
                closed = self._closed_tts_imports.get(transfer_id)
                if closed is not None and closed[:4] == (
                        device_id, connection_generation, executor_generation, parent_request_id):
                    return False
                raise DeviceFileTransferError("TTS abort 请求与原 DFT 导入票据不匹配。")
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if (record.direction != "device_upload" or record.tts_import_ticket is None
                    or record.tts_import_ticket.get("parent_request_id") != parent_request_id):
                raise DeviceFileTransferError("TTS abort 请求与原 DFT 导入票据不匹配。")
            if record.tts_import_committed:
                return False
            return self.close(transfer_id=transfer_id, device_id=device_id,
                connection_generation=connection_generation, executor_generation=executor_generation)

    def close_tts_asset_import(self, *, transfer_id: str, device_id: str,
                               connection_generation: int, executor_generation: int,
                               parent_request_id: str, model_id: str, purpose: str,
                               filename: str, expected_bytes: int,
                               expected_sha256: str) -> bool:
        """关闭仅在 VoiceLibrary（音色库）已提交后确认匹配的 TTS DFT（文件传输）暂存。"""
        expected_ticket = {
            "transfer_id": transfer_id,
            "device_id": device_id,
            "connection_generation": connection_generation,
            "executor_generation": executor_generation,
            "parent_request_id": parent_request_id,
            "model_id": model_id,
            "purpose": purpose,
            "filename": filename,
            "expected_bytes": expected_bytes,
        }
        if (not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256)
                or not isinstance(transfer_id, str) or not _TRANSFER_ID.fullmatch(transfer_id)):
            raise DeviceFileTransferError("TTS 完成清理的原票据编号或摘要无效。")
        self._expire()
        with self._lock:
            record = self._records.get(transfer_id)
            if record is None:
                return True
            if (record.direction != "device_upload" or record.tts_import_ticket != expected_ticket
                    or record.state != "ready"):
                raise DeviceFileTransferError("TTS 完成清理与已核验 DFT 票据不匹配。")
            self._verify_ready_file(record)
            if record.bytes != expected_bytes or record.sha256 != expected_sha256:
                raise DeviceFileTransferError("TTS 完成清理的长度或摘要与已核验票据不匹配。")
            self._closed_tts_imports[transfer_id] = (
                device_id, connection_generation, executor_generation,
                parent_request_id, time.monotonic(),
            )
            return self.close(transfer_id=transfer_id, device_id=device_id,
                connection_generation=connection_generation, executor_generation=executor_generation)

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
        """复核原设备上传票据的完整长度和设备文件通道摘要。"""
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
                raise DeviceFileTransferError("设备没有提交完整文件通道内容。")
            actual_bytes, actual_sha = self._digest(record.path)
            if actual_bytes != expected_bytes or actual_sha != expected_sha256:
                raise DeviceFileTransferError("设备文件通道暂存内容长度或 SHA-256 摘要不匹配。")
            stat = record.path.stat()
            record.bytes, record.sha256, record.modified_ns = actual_bytes, actual_sha, stat.st_mtime_ns
            record.state, record.touched_at = "ready", time.monotonic()
            return record.snapshot()

    def open_device_file_channel(self, *, transfer_id: str, device_id: str,
                                 connection_generation: int, executor_generation: int,
                                 operation: str, maximum_bytes: int,
                                 expected_sha256: str | None = None) -> dict[str, Any]:
        """为既有设备传输票据建立 FILE（固定文件通道）能力令牌，不创建新票据。"""
        if (operation not in {"file.sftp.upload", "file.sftp.download"}
                or type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_TRANSFER_BYTES
                or (expected_sha256 is not None and (not isinstance(expected_sha256, str)
                    or not _SHA256.fullmatch(expected_sha256)))):
            raise DeviceFileTransferError("固定文件通道操作、大小上限或摘要格式无效。")
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            expected_direction = "device_upload" if operation == "file.sftp.upload" else "core_upload"
            if record.direction != expected_direction or record.expected_bytes is None:
                raise DeviceFileTransferError("固定文件通道操作与已审批文件传输方向不匹配。")
            if record.expected_bytes > maximum_bytes:
                raise DeviceFileTransferError("固定文件通道上限低于已登记的审批文件长度。")
            if operation == "file.sftp.download":
                self._verify_ready_file(record)
                if expected_sha256 != record.sha256:
                    raise DeviceFileTransferError("固定文件通道下载摘要与已审批附件摘要不匹配。")
            elif expected_sha256 is not None:
                raise DeviceFileTransferError("设备读取通道不能由客户端覆盖服务端登记摘要。")
            now = time.monotonic()
            if (record.channel_token and not record.channel_completed
                    and now - record.channel_touched_at < FILE_CHANNEL_TTL_SECONDS):
                raise DeviceFileTransferError("该文件传输票据已有活动的固定文件通道。")
            if record.channel_token:
                record.channel_token = None
                record.channel_operation = None
                record.channel_touched_at = 0.0
            if record.channel_operation and record.channel_operation != operation:
                raise DeviceFileTransferError("文件传输票据已绑定其他固定通道操作。")
            if operation == "file.sftp.upload" and record.state not in {"waiting_device", "receiving", "ready"}:
                raise DeviceFileTransferError("设备上传票据当前状态不能打开固定文件通道。")
            token = secrets.token_urlsafe(32)
            record.channel_token = token
            record.channel_operation = operation
            record.channel_touched_at = now
            record.channel_completed = record.state == "ready" and operation == "file.sftp.upload"
            record.channel_next_offset = 0
            record.channel_chunk_receipts.clear()
            record.touched_at = now
            offset = record.bytes if operation == "file.sftp.upload" else 0
            return {"transfer_id": transfer_id, "channel_token": token,
                    "operation": operation, "total_bytes": record.expected_bytes,
                    "sha256": record.sha256,
                    "next_offset": offset, "ready": record.state == "ready",
                    "fields": {"transfer_id": "原 DSH 文件传输编号", "channel_token": "绑定设备、代次、方向和传输票据的短时 FILE 能力令牌",
                        "operation": "原获批文件读取或写入操作编号", "total_bytes": "服务端传输票据锁定的完整字节数",
                        "sha256": "服务端已校验或锁定的文件 SHA-256 摘要", "next_offset": "上传续传时下一期望的字节偏移",
                        "ready": "服务端是否已有完整且已核验的文件"}}

    def append_device_channel_chunk(self, *, transfer_id: str, device_id: str,
                                    connection_generation: int, executor_generation: int,
                                    channel_token: str, offset: int, data_base64: str,
                                    chunk_sha256: str) -> dict[str, Any]:
        """写入设备读取分块；复用 DeviceFileTransferStore（设备文件传输存储）的票据和偏移。"""
        raw = self._decode_channel_chunk(offset, data_base64)
        if not isinstance(chunk_sha256, str) or not _SHA256.fullmatch(chunk_sha256):
            raise DeviceFileTransferError("固定文件通道分块摘要格式无效。")
        actual_chunk_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_chunk_sha256 != chunk_sha256:
            raise DeviceFileTransferError("固定文件通道分块摘要不匹配。")
        with self._lock:
            record = self._get_channel(transfer_id, device_id, connection_generation,
                                       executor_generation, channel_token, "file.sftp.upload")
            if record.state == "ready":
                self._verify_ready_file(record)
                if offset < record.bytes:
                    previous = record.chunk_receipts.get(offset)
                    if previous is not None and previous[:2] == (len(raw), actual_chunk_sha256):
                        return {"transfer_id": transfer_id, "next_offset": previous[2],
                                "fields": {"transfer_id": "原文件传输编号", "next_offset": "下一分块所需字节偏移"}}
                raise DeviceFileTransferError("已完成设备上传不能再写入新分块。")
            chunk_digest = actual_chunk_sha256
            previous = record.chunk_receipts.get(offset)
            if previous is not None:
                if previous[:2] != (len(raw), chunk_digest):
                    raise DeviceFileTransferError("同一设备文件分块偏移收到冲突内容。")
                record.channel_touched_at = record.touched_at = time.monotonic()
                return {"transfer_id": transfer_id, "next_offset": previous[2],
                        "fields": {"transfer_id": "原文件传输编号", "next_offset": "下一分块所需字节偏移"}}
            if (record.state not in {"waiting_device", "receiving"} or offset != record.bytes
                    or record.bytes + len(raw) > record.expected_bytes
                    or record.bytes + len(raw) > MAX_TRANSFER_BYTES):
                raise DeviceFileTransferError("设备文件分块偏移重复、越序或超过已审批长度。")
            self._ensure_root()
            if self._is_reparse(record.partial_path):
                raise DeviceFileTransferError("设备文件分块路径不能是链接。")
            if not record.partial_path.exists():
                with record.partial_path.open("xb"):
                    pass
            try:
                with record.partial_path.open("ab") as handle:
                    handle.write(raw)
                    handle.flush()
            except OSError as exc:
                raise DeviceFileTransferError("设备文件分块暂存写入失败。") from exc
            record.bytes += len(raw)
            record.chunk_receipts[offset] = (len(raw), chunk_digest, record.bytes)
            record.state = "receiving"
            record.channel_touched_at = record.touched_at = time.monotonic()
            return {"transfer_id": transfer_id, "next_offset": record.bytes,
                    "fields": {"transfer_id": "原文件传输编号", "next_offset": "下一分块所需字节偏移"}}

    def read_device_channel_chunk(self, *, transfer_id: str, device_id: str,
                                  connection_generation: int, executor_generation: int,
                                  channel_token: str, offset: int,
                                  limit: int = MAX_CHUNK_BYTES) -> dict[str, Any]:
        """读取设备写入分块；下载内容来自原附件票据的已核验暂存文件。"""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_CHUNK_BYTES:
            raise DeviceFileTransferError("固定文件通道读取偏移或分块上限无效。")
        with self._lock:
            record = self._get_channel(transfer_id, device_id, connection_generation,
                                       executor_generation, channel_token, "file.sftp.download")
            self._verify_ready_file(record)
            if offset > record.bytes:
                raise DeviceFileTransferError("固定文件通道读取偏移超过审批附件长度。")
            with record.path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(min(limit, record.bytes - offset))
            chunk_digest = hashlib.sha256(chunk).hexdigest()
            previous = record.channel_chunk_receipts.get(offset)
            if offset < record.channel_next_offset:
                if previous is None or previous[:2] != (len(chunk), chunk_digest):
                    raise DeviceFileTransferError("固定文件通道重复读取块与已确认内容不匹配。")
                record.channel_touched_at = record.touched_at = time.monotonic()
                return {"transfer_id": transfer_id, "offset": offset, "bytes": len(chunk),
                        "total_bytes": record.bytes, "sha256": record.sha256,
                        "chunk_sha256": chunk_digest, "data_base64": base64.b64encode(chunk).decode("ascii"),
                        "fields": {"transfer_id": "原文件传输编号", "offset": "当前原文件字节偏移",
                            "bytes": "本分块字节数", "total_bytes": "完整文件字节数", "sha256": "完整文件摘要",
                            "chunk_sha256": "当前分块摘要", "data_base64": "规范 Base64 文件分块"}}
            if offset != record.channel_next_offset or not chunk and offset < record.bytes:
                raise DeviceFileTransferError("固定文件通道下载块偏移重复、越序或文件内容不完整。")
            next_offset = offset + len(chunk)
            record.channel_chunk_receipts[offset] = (len(chunk), chunk_digest, next_offset)
            record.channel_next_offset = next_offset
            record.channel_touched_at = record.touched_at = time.monotonic()
            return {"transfer_id": transfer_id, "offset": offset, "bytes": len(chunk),
                    "total_bytes": record.bytes, "sha256": record.sha256,
                    "chunk_sha256": chunk_digest,
                    "data_base64": base64.b64encode(chunk).decode("ascii"),
                    "fields": {"transfer_id": "原文件传输编号", "offset": "当前原文件字节偏移",
                        "bytes": "本分块字节数", "total_bytes": "完整文件字节数", "sha256": "完整文件摘要",
                        "chunk_sha256": "当前分块摘要", "data_base64": "规范 Base64 文件分块"}}

    def finish_device_channel_upload(self, *, transfer_id: str, device_id: str,
                                     connection_generation: int, executor_generation: int,
                                     channel_token: str, expected_bytes: int,
                                     expected_sha256: str) -> dict[str, Any]:
        """完成设备读取上传并复用原设备上传票据执行长度与摘要核验。"""
        with self._lock:
            record = self._get_channel(transfer_id, device_id, connection_generation,
                                       executor_generation, channel_token, "file.sftp.upload")
            if record.state == "ready":
                self._verify_ready_file(record)
                if record.bytes != expected_bytes or record.sha256 != expected_sha256:
                    raise DeviceFileTransferError("重复设备上传完成摘要与原票据不一致。")
            else:
                self._verify_common(record, expected_bytes, expected_sha256)
                if record.state not in {"waiting_device", "receiving"}:
                    raise DeviceFileTransferError("设备上传票据状态不能完成。")
                if self._is_reparse(record.partial_path):
                    raise DeviceFileTransferError("设备上传暂存临时路径不能是链接。")
                if record.expected_bytes == 0 and not record.partial_path.exists():
                    with record.partial_path.open("xb"):
                        pass
                if not record.partial_path.is_file():
                    raise DeviceFileTransferError("设备上传没有收到完整文件内容。")
                try:
                    with record.partial_path.open("ab") as handle:
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError as exc:
                    raise DeviceFileTransferError("设备上传暂存内容无法持久化。") from exc
                actual_bytes, actual_sha = self._digest(record.partial_path)
                if actual_bytes != expected_bytes or actual_sha != expected_sha256:
                    raise DeviceFileTransferError("设备上传完成后的长度或 SHA-256 摘要不匹配。")
                if self._is_reparse(record.path) or record.path.exists():
                    raise DeviceFileTransferError("设备上传最终暂存路径已被占用，拒绝覆盖。")
                record.partial_path.rename(record.path)
                stat = record.path.stat()
                record.bytes, record.sha256, record.modified_ns = actual_bytes, actual_sha, stat.st_mtime_ns
                record.state = "ready"
            record.channel_completed = True
            record.channel_touched_at = record.touched_at = time.monotonic()
            return self._device_channel_snapshot(record)

    def finish_device_channel_download(self, *, transfer_id: str, device_id: str,
                                       connection_generation: int, executor_generation: int,
                                       channel_token: str, expected_bytes: int,
                                       expected_sha256: str) -> dict[str, Any]:
        """确认客户端已完整读取获批附件；不提交或修改原附件票据。"""
        with self._lock:
            record = self._get_channel(transfer_id, device_id, connection_generation,
                                       executor_generation, channel_token, "file.sftp.download")
            self._verify_ready_file(record)
            if record.bytes != expected_bytes or record.sha256 != expected_sha256:
                raise DeviceFileTransferError("设备下载完成回执与获批附件长度或摘要不一致。")
            if record.channel_next_offset != expected_bytes:
                raise DeviceFileTransferError("设备未按顺序读取完整的已审批附件。")
            record.channel_completed = True
            record.channel_touched_at = record.touched_at = time.monotonic()
            return self._device_channel_snapshot(record)

    def cancel_device_file_channel(self, *, transfer_id: str, device_id: str,
                                  connection_generation: int, executor_generation: int,
                                  channel_token: str) -> bool:
        """取消当前 FILE 会话；不删除 DSH 已审批的核心附件票据。"""
        with self._lock:
            record = self._get_channel_any_operation(transfer_id, device_id, connection_generation,
                                                    executor_generation, channel_token)
            if record.channel_completed:
                record.channel_token = None
                record.channel_operation = None
                record.channel_touched_at = 0.0
                return True
            if record.channel_operation == "file.sftp.upload" and record.state != "ready":
                try:
                    if record.partial_path.is_file() and not self._is_reparse(record.partial_path):
                        record.partial_path.unlink()
                except OSError as exc:
                    raise DeviceFileTransferError("取消设备上传时无法清理分块暂存。") from exc
                record.bytes = 0
                record.chunk_receipts.clear()
                record.state = "waiting_device"
            if record.channel_operation == "file.sftp.download":
                record.channel_next_offset = 0
                record.channel_chunk_receipts.clear()
            record.channel_token = None
            record.channel_operation = None
            record.channel_touched_at = 0.0
            record.touched_at = time.monotonic()
            return True

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

    def device_file_download_ticket(self, *, transfer_id: str, device_id: str,
                                    connection_generation: int, executor_generation: int) -> dict[str, Any]:
        """为固定文件通道返回获批目标、长度与摘要；不返回服务端暂存路径。"""
        with self._lock:
            record = self._get(transfer_id, device_id, connection_generation, executor_generation)
            if record.direction != "core_upload":
                raise DeviceFileTransferError("文件暂存租约不是等待设备下载的核心附件。")
            self._verify_ready_file(record)
            return {"target_path": record.target_path, "bytes": record.bytes, "sha256": record.sha256,
                    "fields": {"target_path": "一次性审批绑定的设备目标路径", "bytes": "已校验原始字节数",
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
            legacy_partial = Path(f"{path}.{transfer_id}.part")
            if path.exists() or partial.exists() or legacy_partial.exists():
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

    @staticmethod
    def _device_channel_snapshot(record: _Transfer) -> dict[str, Any]:
        """返回不包含暂存或设备目标路径的 FILE 通道收据。"""
        return {"transfer_id": record.transfer_id, "state": record.state,
                "bytes": record.bytes, "sha256": record.sha256,
                "fields": {"transfer_id": "原 DSH 文件传输编号", "state": "原票据暂存状态",
                    "bytes": "已核验的原始字节数", "sha256": "已核验的原始文件摘要"}}

    def _get_channel(self, transfer_id: str, device_id: str, connection_generation: int,
                     executor_generation: int, channel_token: str, operation: str) -> _Transfer:
        record = self._get(transfer_id, device_id, connection_generation, executor_generation)
        if (not isinstance(channel_token, str) or not 32 <= len(channel_token) <= 128
                or record.channel_token is None
                or not secrets.compare_digest(record.channel_token, channel_token)
                or record.channel_operation != operation
                or time.monotonic() - record.channel_touched_at >= FILE_CHANNEL_TTL_SECONDS):
            raise DeviceFileTransferError("固定文件通道能力令牌无效、过期或操作不匹配。")
        return record

    def _get_channel_any_operation(self, transfer_id: str, device_id: str,
                                   connection_generation: int, executor_generation: int,
                                   channel_token: str) -> _Transfer:
        record = self._get(transfer_id, device_id, connection_generation, executor_generation)
        if (not isinstance(channel_token, str) or not 32 <= len(channel_token) <= 128
                or record.channel_token is None
                or not secrets.compare_digest(record.channel_token, channel_token)
                or record.channel_operation not in {"file.sftp.upload", "file.sftp.download"}
                or time.monotonic() - record.channel_touched_at >= FILE_CHANNEL_TTL_SECONDS):
            raise DeviceFileTransferError("固定文件通道能力令牌无效或已过期。")
        return record

    @staticmethod
    def _decode_channel_chunk(offset: int, data_base64: str) -> bytes:
        if type(offset) is not int or offset < 0 or not isinstance(data_base64, str):
            raise DeviceFileTransferError("固定文件通道分块偏移或编码无效。")
        try:
            raw = base64.b64decode(data_base64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise DeviceFileTransferError("固定文件通道分块 Base64 编码无效。") from exc
        if (not raw or len(raw) > MAX_CHUNK_BYTES
                or base64.b64encode(raw).decode("ascii") != data_base64):
            raise DeviceFileTransferError("固定文件通道分块为空、超限或不是规范 Base64。")
        return raw

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
                        raise DeviceFileTransferError("暂存文件超过设备文件传输总长度上限。")
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
            closed = [key for key, item in self._closed_tts_imports.items()
                      if now - item[4] >= TRANSFER_TTL_SECONDS]
            for key in closed:
                self._closed_tts_imports.pop(key, None)
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

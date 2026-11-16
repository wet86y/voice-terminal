"""设备信任登记：仅保存获准设备的公开身份，不保存配对码或私钥。"""

from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


_SCHEMA_VERSION = 1
_MAX_STORE_BYTES = 1024 * 1024
_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PUBLIC_KEY_PREFIX = b"ssh-ed25519"


class DeviceTrustError(RuntimeError):
    """设备信任状态错误；遇到未知或损坏数据时采用 fail-closed 行为。"""


@dataclass(frozen=True)
class TrustedDevice:
    """TrustedDevice（受信设备）：只包含设备公钥及其登记状态。"""

    device_id: str
    public_key: bytes
    fingerprint: str
    state: str
    trusted_at_ms: int
    revoked_at_ms: int | None


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeviceTrustError(f"信任登记包含重复字段：{key}")
        result[key] = value
    return result


def _read_string(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(data):
        raise ValueError("Ed25519 公钥编码被截断。")
    size = int.from_bytes(data[offset:offset + 4], "big")
    offset += 4
    if size == 0 or size > 4096 or offset + size > len(data):
        raise ValueError("Ed25519 公钥字段长度无效。")
    return data[offset:offset + size], offset + size


def parse_ed25519_public_key(value: str) -> tuple[bytes, str]:
    """解析 OpenSSH Ed25519 公钥并返回原始密钥与标准 SHA256 指纹。"""
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Ed25519 公钥格式无效。")
    parts = value.strip().split()
    if len(parts) not in (2, 3) or parts[0] != "ssh-ed25519":
        raise ValueError("仅接受 OpenSSH Ed25519 公钥。")
    try:
        blob = base64.b64decode(parts[1].encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("Ed25519 公钥 Base64 编码无效。") from exc
    algorithm, offset = _read_string(blob, 0)
    raw_key, offset = _read_string(blob, offset)
    if algorithm != _PUBLIC_KEY_PREFIX or len(raw_key) != 32 or offset != len(blob):
        raise ValueError("Ed25519 公钥内容无效。")
    # OpenSSH SHA256 指纹对完整 wire 编码求摘要，而不是对裸密钥求摘要。
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return raw_key, f"SHA256:{digest}"


class DeviceTrustStore:
    """DeviceTrustStore（设备信任存储）：原子登记设备公钥并支持撤销。"""

    def __init__(self, runtime_data_dir: Path) -> None:
        self._root = Path(runtime_data_dir).absolute()
        self._state_dir = self._root / "state"
        self._path = self._state_dir / "device-trust.json"
        self._lock = threading.RLock()
        self._records: list[TrustedDevice] = []
        self._state = "missing"
        self._error: str | None = None
        self._disk_digest: bytes | None = None
        self._load()

    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    def snapshot(self) -> dict[str, Any]:
        """snapshot（快照）：返回不含私钥、签名或凭据的管理视图。"""
        with self._lock:
            return {
                "schema_version": _SCHEMA_VERSION,
                "state": self._state,
                "devices": [
                    {
                        "device_id": item.device_id,
                        "fingerprint": item.fingerprint,
                        "state": item.state,
                        "trusted_at_ms": item.trusted_at_ms,
                        "revoked_at_ms": item.revoked_at_ms,
                    }
                    for item in self._records
                ],
                **({"error": self._error} if self._error else {}),
            }

    def trusted_key(self, device_id: str) -> bytes | None:
        """trusted_key（查询受信公钥）：撤销设备始终返回空值。"""
        with self._lock:
            for item in self._records:
                if item.device_id == device_id and item.state == "trusted":
                    return item.public_key
        return None

    def authorize(self, device_id: str, public_key: str, message: bytes, signature: bytes) -> bool:
        """authorize（验证身份）：要求登记公钥、设备编号和 Ed25519 签名全部匹配。"""
        try:
            candidate, _ = parse_ed25519_public_key(public_key)
            expected = self.trusted_key(device_id)
            if expected is None or not hmac.compare_digest(expected, candidate):
                return False
            return self.verify_signature(device_id, message, signature)
        except (ValueError, TypeError, InvalidSignature, DeviceTrustError):
            return False

    def verify_signature(self, device_id: str, message: bytes, signature: bytes) -> bool:
        """verify_signature（验证受信设备签名）：撤销公钥立即失去请求权限。"""
        try:
            self._validate_device_id(device_id)
            if not isinstance(message, bytes) or not isinstance(signature, bytes) or len(signature) != 64:
                return False
            expected = self.trusted_key(device_id)
            if expected is None:
                return False
            Ed25519PublicKey.from_public_bytes(expected).verify(signature, message)
            return True
        except (ValueError, TypeError, InvalidSignature, DeviceTrustError):
            return False

    def approve(self, device_id: str, public_key: str, *, trusted_at_ms: int | None = None) -> TrustedDevice:
        """approve（明确批准）：只允许调用端在主机用户接受后登记。"""
        self._validate_device_id(device_id)
        raw_key, fingerprint = parse_ed25519_public_key(public_key)
        timestamp = self._timestamp(trusted_at_ms)
        with self._lock:
            if self._state == "invalid":
                raise DeviceTrustError("信任登记无效，已保留原文件并拒绝写入。")
            self._check_disk_unchanged()
            for item in self._records:
                if item.public_key == raw_key and item.device_id != device_id:
                    raise DeviceTrustError("同一设备公钥不能登记到多个设备编号。")
                if (item.device_id == device_id and item.public_key != raw_key
                        and item.state == "trusted"):
                    raise DeviceTrustError("设备公钥变化；请先撤销旧身份，再明确重新配对。")
            record = TrustedDevice(
                device_id=device_id,
                public_key=raw_key,
                fingerprint=fingerprint,
                state="trusted",
                trusted_at_ms=timestamp,
                revoked_at_ms=None,
            )
            records = [item for item in self._records if item.device_id != device_id]
            records.append(record)
            self._persist(records)
            self._records = records
            self._state = "valid"
            self._error = None
            return record

    def revoke(self, device_id: str, *, revoked_at_ms: int | None = None) -> bool:
        """revoke（撤销信任）：保留原公钥审计记录但立即停止授权。"""
        self._validate_device_id(device_id)
        timestamp = self._timestamp(revoked_at_ms)
        with self._lock:
            if self._state == "invalid":
                raise DeviceTrustError("信任登记无效，已保留原文件并拒绝写入。")
            self._check_disk_unchanged()
            found = False
            records: list[TrustedDevice] = []
            for item in self._records:
                if item.device_id == device_id and item.state == "trusted":
                    records.append(TrustedDevice(
                        item.device_id, item.public_key, item.fingerprint,
                        "revoked", item.trusted_at_ms, timestamp,
                    ))
                    found = True
                else:
                    records.append(item)
            if not found:
                return False
            self._persist(records)
            self._records = records
            self._state = "valid"
            return True

    @staticmethod
    def _validate_device_id(device_id: str) -> None:
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise DeviceTrustError("设备编号格式无效。")

    @staticmethod
    def _timestamp(value: int | None) -> int:
        if value is None:
            import time
            return time.time_ns() // 1_000_000
        if type(value) is not int or value < 0:
            raise DeviceTrustError("设备信任时间必须是非负整数毫秒。")
        return value

    @staticmethod
    def _is_link(path: Path) -> bool:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())

    def _ensure_safe_parent(self, *, create: bool) -> None:
        if self._is_link(self._root):
            raise DeviceTrustError("用户数据根目录不能是符号链接或目录联接。")
        if create:
            try:
                self._state_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DeviceTrustError("无法创建设备信任登记目录。") from exc
        if self._is_link(self._state_dir):
            raise DeviceTrustError("设备信任登记目录不能是符号链接或目录联接。")
        try:
            if self._state_dir.exists() and not self._state_dir.resolve().is_relative_to(self._root.resolve()):
                raise DeviceTrustError("设备信任登记目录越出用户数据根目录。")
        except OSError as exc:
            raise DeviceTrustError("无法验证设备信任登记路径。") from exc
        if self._is_link(self._path):
            raise DeviceTrustError("设备信任登记文件不能是符号链接或目录联接。")

    def _load(self) -> None:
        try:
            self._ensure_safe_parent(create=False)
            if not self._path.exists():
                self._state = "missing"
                self._disk_digest = None
                return
            raw = self._path.read_bytes()
            if len(raw) > _MAX_STORE_BYTES:
                raise DeviceTrustError("设备信任登记超过大小限制。")
            parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_fields)
            self._records = self._parse_document(parsed)
            self._state = "valid"
            self._disk_digest = hashlib.sha256(raw).digest()
        except (OSError, UnicodeError, json.JSONDecodeError, DeviceTrustError, ValueError, TypeError) as exc:
            self._state = "invalid"
            self._error = str(exc)[:240] or "设备信任登记无法读取。"

    def _parse_document(self, value: Any) -> list[TrustedDevice]:
        if not isinstance(value, dict) or set(value) != {"schema_version", "devices"}:
            raise DeviceTrustError("设备信任登记根结构无效。")
        if type(value.get("schema_version")) is not int or value["schema_version"] != _SCHEMA_VERSION:
            raise DeviceTrustError("设备信任登记版本未知。")
        devices = value.get("devices")
        if not isinstance(devices, list) or len(devices) > 1024:
            raise DeviceTrustError("设备信任登记清单无效。")
        records: list[TrustedDevice] = []
        ids: set[str] = set()
        keys: set[bytes] = set()
        expected_fields = {"device_id", "public_key", "fingerprint", "state", "trusted_at_ms", "revoked_at_ms"}
        for row in devices:
            if not isinstance(row, dict) or set(row) != expected_fields:
                raise DeviceTrustError("设备信任登记项字段无效。")
            device_id = row["device_id"]
            self._validate_device_id(device_id)
            raw_key, fingerprint = parse_ed25519_public_key(row["public_key"])
            state = row["state"]
            trusted_at_ms = row["trusted_at_ms"]
            revoked_at_ms = row["revoked_at_ms"]
            if (device_id in ids or raw_key in keys or row["fingerprint"] != fingerprint
                    or state not in ("trusted", "revoked")
                    or type(trusted_at_ms) is not int or trusted_at_ms < 0
                    or (state == "trusted" and revoked_at_ms is not None)
                    or (state == "revoked" and (type(revoked_at_ms) is not int or revoked_at_ms < trusted_at_ms))):
                raise DeviceTrustError("设备信任登记项重复或状态矛盾。")
            ids.add(device_id)
            keys.add(raw_key)
            records.append(TrustedDevice(
                device_id, raw_key, fingerprint, state, trusted_at_ms, revoked_at_ms,
            ))
        return records

    def _check_disk_unchanged(self) -> None:
        self._ensure_safe_parent(create=False)
        if not self._path.exists():
            if self._disk_digest is not None:
                raise DeviceTrustError("设备信任登记在外部发生变化，拒绝覆盖。")
            return
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise DeviceTrustError("无法重新读取设备信任登记。") from exc
        current_digest = hashlib.sha256(raw).digest()
        if self._disk_digest is None or not hmac.compare_digest(current_digest, self._disk_digest):
            raise DeviceTrustError("设备信任登记在外部发生变化，拒绝覆盖。")

    def _persist(self, records: list[TrustedDevice]) -> None:
        self._ensure_safe_parent(create=True)
        document = {
            "schema_version": _SCHEMA_VERSION,
            "devices": [
                {
                    "device_id": item.device_id,
                    "public_key": "ssh-ed25519 " + base64.b64encode(
                        self._public_key_blob(item.public_key),
                    ).decode("ascii"),
                    "fingerprint": item.fingerprint,
                    "state": item.state,
                    "trusted_at_ms": item.trusted_at_ms,
                    "revoked_at_ms": item.revoked_at_ms,
                }
                for item in records
            ],
        }
        encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > _MAX_STORE_BYTES:
            raise DeviceTrustError("设备信任登记超过大小限制。")
        fd = -1
        temp_name: str | None = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".device-trust-", suffix=".tmp", dir=self._state_dir)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if self._is_link(self._path):
                raise DeviceTrustError("设备信任登记文件在写入期间变为链接。")
            os.replace(temp_name, self._path)
            temp_name = None
            self._disk_digest = hashlib.sha256(encoded).digest()
        except OSError as exc:
            raise DeviceTrustError("无法原子写入设备信任登记。") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    @staticmethod
    def _public_key_blob(raw_key: bytes) -> bytes:
        return (len(_PUBLIC_KEY_PREFIX).to_bytes(4, "big") + _PUBLIC_KEY_PREFIX
                + len(raw_key).to_bytes(4, "big") + raw_key)


@dataclass(frozen=True)
class PendingPairing:
    """PendingPairing（待批准配对请求）：内存态且不包含配对码。"""

    request_id: str
    device_id: str
    fingerprint: str
    created_at_ms: int
    expires_at_ms: int


def pairing_proof_message(device_id: str, fingerprint: str, pairing_code: str, client_nonce: bytes) -> bytes:
    """构造设备签名内容，绑定编号、指纹、当次配对码与客户端随机数。"""
    DeviceTrustStore._validate_device_id(device_id)
    if (not isinstance(fingerprint, str) or not fingerprint.startswith("SHA256:")
            or not isinstance(pairing_code, str) or not re.fullmatch(r"[0-9]{8}", pairing_code)
            or not isinstance(client_nonce, bytes) or not 16 <= len(client_nonce) <= 64):
        raise DeviceTrustError("配对证明字段无效。")
    fields = (device_id.encode("ascii"), fingerprint.encode("ascii"), pairing_code.encode("ascii"), client_nonce)
    return b"voice-terminal-device-pairing-v1\0" + b"".join(
        len(value).to_bytes(4, "big") + value for value in fields
    )


class DevicePairingManager:
    """DevicePairingManager（设备配对管理器）：限时接收、人工批准，不自动授信。"""

    PAIRING_TTL_MIN_SECONDS = 30
    PAIRING_TTL_MAX_SECONDS = 600
    PENDING_TTL_MS = 5 * 60 * 1000
    MAX_PENDING = 32
    MAX_INVALID_CODE_ATTEMPTS = 10

    def __init__(self, trust_store: DeviceTrustStore) -> None:
        self._trust_store = trust_store
        self._lock = threading.RLock()
        self._code_digest: bytes | None = None
        self._window_expires_at_ms = 0
        self._invalid_code_attempts = 0
        self._pending: dict[str, tuple[PendingPairing, str, bytes, bytes]] = {}

    def open_window(self, *, ttl_seconds: int = 120, now_ms: int | None = None) -> tuple[str, int]:
        """open_window（开启配对窗口）：返回一次性显示码，仅在此对象内保存其摘要。"""
        if type(ttl_seconds) is not int or not self.PAIRING_TTL_MIN_SECONDS <= ttl_seconds <= self.PAIRING_TTL_MAX_SECONDS:
            raise DeviceTrustError("配对窗口时长必须为 30 至 600 秒。")
        now = self._now(now_ms)
        code = f"{secrets.randbelow(100_000_000):08d}"
        expires = now + ttl_seconds * 1000
        with self._lock:
            # 打开新窗口会撤销旧码及其未处理请求，避免旧界面继续批准过期挑战。
            self._pending.clear()
            self._code_digest = hashlib.sha256(code.encode("ascii")).digest()
            self._window_expires_at_ms = expires
            self._invalid_code_attempts = 0
        return code, expires

    def close_window(self) -> None:
        """close_window（关闭配对窗口）：清除配对码摘要和所有未批准请求。"""
        with self._lock:
            self._code_digest = None
            self._window_expires_at_ms = 0
            self._invalid_code_attempts = 0
            self._pending.clear()

    def submit_request(
        self,
        device_id: str,
        public_key: str,
        pairing_code: str,
        client_nonce: bytes,
        signature: bytes,
        *,
        now_ms: int | None = None,
    ) -> PendingPairing:
        """submit_request（提交配对请求）：验码与验签后只进入待批准列表。"""
        DeviceTrustStore._validate_device_id(device_id)
        raw_key, fingerprint = parse_ed25519_public_key(public_key)
        if not isinstance(client_nonce, bytes) or not 16 <= len(client_nonce) <= 64:
            raise DeviceTrustError("客户端随机数长度无效。")
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise DeviceTrustError("设备配对签名长度无效。")
        if not isinstance(pairing_code, str) or not re.fullmatch(r"[0-9]{8}", pairing_code):
            raise DeviceTrustError("配对请求未获授权或配对窗口已关闭。")
        proof = pairing_proof_message(device_id, fingerprint, pairing_code, client_nonce)
        try:
            Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, proof)
        except (InvalidSignature, ValueError) as exc:
            raise DeviceTrustError("设备配对签名验证失败。") from exc

        now = self._now(now_ms)
        with self._lock:
            self._purge_expired(now)
            candidate_digest = hashlib.sha256(pairing_code.encode("ascii")).digest()
            if (self._code_digest is None or now >= self._window_expires_at_ms
                    or not hmac.compare_digest(self._code_digest, candidate_digest)):
                if self._code_digest is not None and now < self._window_expires_at_ms:
                    self._invalid_code_attempts += 1
                    if self._invalid_code_attempts >= self.MAX_INVALID_CODE_ATTEMPTS:
                        self._code_digest = None
                        self._window_expires_at_ms = 0
                raise DeviceTrustError("配对请求未获授权或配对窗口已关闭。")
            for pending, pending_key, pending_nonce, _pending_raw_key in self._pending.values():
                if pending.device_id == device_id:
                    if pending_key == fingerprint and pending_nonce == client_nonce:
                        return pending
                    raise DeviceTrustError("该设备已有待批准请求。")
            if len(self._pending) >= self.MAX_PENDING:
                raise DeviceTrustError("待批准设备数量已达上限。")
            request_id = secrets.token_hex(16)
            pending = PendingPairing(request_id, device_id, fingerprint, now, now + self.PENDING_TTL_MS)
            self._pending[request_id] = (pending, fingerprint, client_nonce, raw_key)
            return pending

    def pending_requests(self, *, now_ms: int | None = None) -> list[dict[str, Any]]:
        """pending_requests（待批准快照）：仅公开待审元数据，不返回公钥、随机数或配对码。"""
        now = self._now(now_ms)
        with self._lock:
            self._purge_expired(now)
            values = sorted((item[0] for item in self._pending.values()), key=lambda item: item.created_at_ms)
            return [
                {
                    "request_id": item.request_id,
                    "device_id": item.device_id,
                    "fingerprint": item.fingerprint,
                    "created_at_ms": item.created_at_ms,
                    "expires_at_ms": item.expires_at_ms,
                }
                for item in values
            ]

    def status(self, *, now_ms: int | None = None) -> dict[str, Any]:
        """status（主机配对状态）：不返回配对码、公钥或签名材料。"""
        now = self._now(now_ms)
        with self._lock:
            self._purge_expired(now)
            window_open = self._code_digest is not None and now < self._window_expires_at_ms
            expires_at_ms = self._window_expires_at_ms if window_open else None
            pending = self.pending_requests(now_ms=now)
            trust = self._trust_store.snapshot()
            return {
                "window_open": window_open,
                "window_expires_at_ms": expires_at_ms,
                "failed_attempts_remaining": max(0, self.MAX_INVALID_CODE_ATTEMPTS - self._invalid_code_attempts),
                "pending_requests": pending,
                "trust": trust,
                "fields": {
                    "window_open": "主机配对窗口是否开放",
                    "window_expires_at_ms": "配对窗口的 Unix 毫秒到期时间；关闭时为空",
                    "failed_attempts_remaining": "错误配对码触发自动关闭前剩余尝试次数",
                    "pending_requests": "已验签但尚未批准的设备申请",
                    "trust": "已批准或撤销设备的公开信任登记状态",
                },
            }

    def approve(self, request_id: str, *, now_ms: int | None = None) -> TrustedDevice:
        """approve（主机明确批准）：批准时才将该公钥写入受信存储。"""
        now = self._now(now_ms)
        with self._lock:
            self._purge_expired(now)
            entry = self._pending.get(request_id)
            if entry is None:
                raise DeviceTrustError("配对请求不存在或已过期。")
            pending, fingerprint, _nonce, raw_key = entry
            # 已验签公钥只留在本次进程内存；批准调用不能再次提交或替换该公钥。
            if self._fingerprint_for_raw_key(raw_key) != fingerprint:
                raise DeviceTrustError("待批准设备身份在内存中不一致。")
            key_text = "ssh-ed25519 " + base64.b64encode(DeviceTrustStore._public_key_blob(raw_key)).decode("ascii")
            record = self._trust_store.approve(pending.device_id, key_text, trusted_at_ms=now)
            del self._pending[request_id]
            return record

    def reject(self, request_id: str) -> bool:
        """reject（拒绝请求）：删除待批准请求且不改动已登记设备。"""
        with self._lock:
            return self._pending.pop(request_id, None) is not None

    @staticmethod
    def _now(now_ms: int | None) -> int:
        if now_ms is None:
            return time.time_ns() // 1_000_000
        if type(now_ms) is not int or now_ms < 0:
            raise DeviceTrustError("时间必须是非负整数毫秒。")
        return now_ms

    def _purge_expired(self, now_ms: int) -> None:
        if self._code_digest is not None and now_ms >= self._window_expires_at_ms:
            self._code_digest = None
            self._window_expires_at_ms = 0
            self._invalid_code_attempts = 0
        expired = [request_id for request_id, (item, _key, _nonce, _raw_key) in self._pending.items()
                   if now_ms >= item.expires_at_ms]
        for request_id in expired:
            del self._pending[request_id]

    @staticmethod
    def _fingerprint_for_raw_key(raw_key: bytes) -> str:
        blob = DeviceTrustStore._public_key_blob(raw_key)
        digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
        return f"SHA256:{digest}"


_DEVICE_REQUEST_PATHS = frozenset({
    "/v1/status", "/v1/devices", "/v1/session/open", "/v1/session/poll",
    "/v1/session/complete", "/v1/session/reconcile", "/v1/session/heartbeat", "/v1/session/close",
    "/v1/interaction/prepare", "/v1/interaction/abort", "/v1/media",
})
_CORE_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _valid_device_request_target(method: str, path: str) -> bool:
    """_valid_device_request_target（设备签名目标）：业务请求仅 POST，媒体升级仅 GET。"""
    return ((method == "POST" and path in _DEVICE_REQUEST_PATHS and path != "/v1/media")
            or (method == "GET" and path == "/v1/media"))


def device_request_proof_message(
    *, method: str, path: str, device_id: str, core_session_id: str,
    challenge: str, timestamp_ms: int, payload: dict[str, Any],
) -> bytes:
    """Create the stable signed envelope for a device gateway request."""
    DeviceTrustStore._validate_device_id(device_id)
    if (not _valid_device_request_target(method, path)
            or not isinstance(core_session_id, str) or not _CORE_SESSION_ID.fullmatch(core_session_id)
            or not isinstance(challenge, str) or not _CHALLENGE.fullmatch(challenge)
            or type(timestamp_ms) is not int or timestamp_ms < 0
            or not isinstance(payload, dict)):
        raise DeviceTrustError("设备业务请求签名字段无效。")
    try:
        canonical_payload = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DeviceTrustError("设备业务请求无法规范化签名。") from exc
    fields = (
        method.encode("ascii"), path.encode("ascii"), device_id.encode("ascii"),
        core_session_id.encode("ascii"), challenge.encode("ascii"),
        str(timestamp_ms).encode("ascii"), hashlib.sha256(canonical_payload).digest(),
    )
    return b"voice-terminal-device-request-v1\0" + b"".join(
        len(value).to_bytes(4, "big") + value for value in fields
    )


class DeviceRequestAuthenticator:
    """DeviceRequestAuthenticator（设备请求认证）：一次性挑战绑定核心代次、请求和设备。"""

    CHALLENGE_TTL_SECONDS = 30
    MAX_CHALLENGES = 4096
    TIMESTAMP_TOLERANCE_MS = 5 * 60 * 1000

    def __init__(self, trust_store: DeviceTrustStore, *, core_session_id: str | None = None) -> None:
        self._trust_store = trust_store
        self._core_session_id = core_session_id or secrets.token_hex(16)
        if not _CORE_SESSION_ID.fullmatch(self._core_session_id):
            raise DeviceTrustError("设备核心会话编号格式无效。")
        self._lock = threading.RLock()
        self._challenges: OrderedDict[str, tuple[str, str, str, float]] = OrderedDict()

    @property
    def core_session_id(self) -> str:
        return self._core_session_id

    def issue_challenge(
        self, device_id: str, method: str, path: str, *,
        now_ms: int | None = None, now_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """issue_challenge（签名挑战）：仅为固定设备网关路径签发一次性随机数。"""
        DeviceTrustStore._validate_device_id(device_id)
        if not _valid_device_request_target(method, path):
            raise DeviceTrustError("设备挑战只允许受限版本化网关路径。")
        wall_now = self._now_ms(now_ms)
        monotonic_now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        challenge = secrets.token_urlsafe(32)
        expires_at = monotonic_now + self.CHALLENGE_TTL_SECONDS
        with self._lock:
            self._purge_expired(monotonic_now)
            if len(self._challenges) >= self.MAX_CHALLENGES:
                raise DeviceTrustError("设备签名挑战暂时已满，请稍后重试。")
            self._challenges[challenge] = (device_id, method, path, expires_at)
        return {
            "protocol_version": 1,
            "core_session_id": self._core_session_id,
            "challenge": challenge,
            "expires_at_ms": wall_now + self.CHALLENGE_TTL_SECONDS * 1000,
            "fields": {
                "protocol_version": "设备网关协议版本",
                "core_session_id": "本次权威核心运行编号，重启后变化",
                "challenge": "绑定设备、方法和路径的一次性随机挑战",
                "expires_at_ms": "挑战的 Unix 毫秒到期时间",
            },
        }

    def authorize_request(
        self, *, method: str, path: str, device_id: str, core_session_id: str,
        challenge: str, timestamp_ms: int, signature: bytes, payload: dict[str, Any],
        now_ms: int | None = None, now_monotonic: float | None = None,
    ) -> bool:
        """authorize_request（验证业务请求）：验时效、挑战绑定、受信签名和一次性使用。"""
        try:
            DeviceTrustStore._validate_device_id(device_id)
            if (not _valid_device_request_target(method, path)
                    or not isinstance(core_session_id, str) or not _CORE_SESSION_ID.fullmatch(core_session_id)
                    or not isinstance(challenge, str) or not _CHALLENGE.fullmatch(challenge)
                    or type(timestamp_ms) is not int or not isinstance(signature, bytes) or len(signature) != 64):
                return False
            current_ms = self._now_ms(now_ms)
            if abs(current_ms - timestamp_ms) > self.TIMESTAMP_TOLERANCE_MS:
                return False
            monotonic_now = time.monotonic() if now_monotonic is None else float(now_monotonic)
            with self._lock:
                self._purge_expired(monotonic_now)
                record = self._challenges.pop(challenge, None)
            if (record is None or core_session_id != self._core_session_id
                    or record[:3] != (device_id, method, path) or monotonic_now >= record[3]):
                return False
            message = device_request_proof_message(
                method=method, path=path, device_id=device_id,
                core_session_id=core_session_id, challenge=challenge,
                timestamp_ms=timestamp_ms, payload=payload,
            )
            return self._trust_store.verify_signature(device_id, message, signature)
        except (DeviceTrustError, TypeError, ValueError, UnicodeError):
            return False

    @staticmethod
    def _now_ms(value: int | None) -> int:
        if value is None:
            return time.time_ns() // 1_000_000
        if type(value) is not int or value < 0:
            raise DeviceTrustError("设备请求时间必须是非负整数毫秒。")
        return value

    def _purge_expired(self, now_monotonic: float) -> None:
        expired = [challenge for challenge, record in self._challenges.items() if now_monotonic >= record[3]]
        for challenge in expired:
            self._challenges.pop(challenge, None)

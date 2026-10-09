"""Official Desktop runtime（官方桌面运行包）paths and integrity checks.

The official Windows installer is the immutable source artifact.  Its version
is compared with the project's compatibility lock; no upstream source commit
is inferred from the installer metadata.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlsplit

from ..distribution import safe_path, verify
from .bridge import BRIDGE_PROTOCOL_VERSION

OFFICIAL_SOURCE_KIND = "official_desktop"
OFFICIAL_EXECUTABLE = "DeepSeek Harness.exe"
OFFICIAL_ASAR = "resources/app.asar"
OFFICIAL_ASAR_UNPACKED = "resources/app.asar.unpacked/dsh"
OFFICIAL_CLI = "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js"
OFFICIAL_DESKTOP_CLI = "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"  # OFFICIAL_DESKTOP_CLI（官方桌面命令入口）：接入随包 pnpm（包管理器）。
OFFICIAL_SCHEDULE = "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-schedule/lib/index.js"
OFFICIAL_OFFICE_CLI = (
    "resources/app.asar.unpacked/dsh/node_modules/@deepseek-ai/libreoffice-kit/lib/cli.js"
)
OFFICIAL_OFFICE_ENGINE = (
    "resources/app.asar.unpacked/dsh/node_modules/"
    "@deepseek-ai/libreoffice-kit-win32-x64"
)
_SHA512_HEX = re.compile(r"^[0-9a-f]{128}$")


@dataclass(frozen=True)
class OfficialArtifact:
    """OfficialArtifact（官方固定附件）：大小与 SHA-512 均来自随包锁定的资源目录。"""

    version: str
    compatibility_commit: str
    url: str
    filename: str
    size_bytes: int
    sha512_base64: str
    sha512_hex: str


@dataclass(frozen=True)
class OfficialRuntimePaths:
    """OfficialRuntimePaths（官方运行路径）：physical（物理文件）与 ASAR 虚拟路径分开。"""

    executable: Path
    cli_entry: Path
    desktop_cli_entry: Path  # desktop_cli_entry（官方桌面命令入口）：与普通命令入口共用已校验归档。
    asar: Path
    schedule_entry: str
    office_cli: Path
    office_engine: Path
    metadata: dict
    manifest: dict


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"官方 DSH 元数据无法读取：{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"官方 DSH 元数据格式无效：{path.name}")
    return value


def load_official_artifact(program_root: Path) -> OfficialArtifact:
    """Read the fixed artifact from runtime.lock/catalog（锁文件和资源目录）."""
    lock = _read_json(program_root / "config/dsh/runtime.lock.json")
    catalog = _read_json(program_root / "config/resources/catalog.json")
    rows = catalog.get("resources")
    resource = next((row for row in rows if isinstance(row, dict) and row.get("resource_id") == "dsh"), None) if isinstance(rows, list) else None
    source_value = resource.get("source", {}) if isinstance(resource, dict) else {}
    source = source_value if isinstance(source_value, dict) else {}
    transfer = source.get("transfer", {}) if isinstance(source, dict) else {}
    if not isinstance(transfer, dict):
        transfer = {}
    artifact = transfer.get("artifact", {}) if isinstance(transfer, dict) else {}
    if not isinstance(artifact, dict):
        artifact = {}
    version = lock.get("version")
    compatibility_commit = lock.get("commit")
    repository = lock.get("repository")
    tag = lock.get("tag")
    url = artifact.get("url") if isinstance(artifact, dict) else None
    filename = artifact.get("filename") if isinstance(artifact, dict) else None
    size_bytes = artifact.get("size_bytes") if isinstance(artifact, dict) else None
    sha512_base64 = artifact.get("sha512_base64") if isinstance(artifact, dict) else None
    try:
        address = urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        address = None
    expected_filename = f"deepseek-harness-{version}-win-x64.exe" if isinstance(version, str) else None
    if (not isinstance(version, str) or not version
            or repository != "https://github.com/deepseek-ai/deepseek-harness.git"
            or tag != f"dsh-v{version}"
            or resource is None or resource.get("kind") != "dsh" or resource.get("version") != version
            or source.get("platform") != "deepseek_official"
            or source.get("repo_id") != "deepseek-ai/deepseek-harness"
            or source.get("revision") != tag
            or transfer.get("kind") != "native_official_installer"
            or not isinstance(compatibility_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", compatibility_commit)
            or address is None or address.scheme != "https" or address.hostname != "download.deepseek.com"
            or address.username or address.password or address.port not in (None, 443)
            or address.query or address.fragment
            or not isinstance(filename, str) or filename != expected_filename or Path(filename).name != filename
            or address.path != f"/dsh-desk/bin/win-x64/{filename}"
            or type(size_bytes) is not int or size_bytes <= 0
            or not isinstance(sha512_base64, str)):
        raise ValueError("官方 DSH 固定附件未在锁定资源目录中完整声明。")
    try:
        digest = base64.b64decode(sha512_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("官方 DSH 附件 SHA-512（文件摘要）编码无效。") from exc
    if len(digest) != hashlib.sha512().digest_size:
        raise ValueError("官方 DSH 附件 SHA-512（文件摘要）长度无效。")
    return OfficialArtifact(
        version=version,
        compatibility_commit=compatibility_commit,
        url=url,
        filename=filename,
        size_bytes=size_bytes,
        sha512_base64=sha512_base64,
        sha512_hex=digest.hex(),
    )


def hash_official_installer(path: Path, artifact: OfficialArtifact,
                            progress: Callable[[int, int], None] | None = None) -> str:
    """Check exact installer bytes（固定安装器字节）and report bounded progress."""
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError("官方 DSH 安装器必须是普通文件。")
    if path.name.casefold() != artifact.filename.casefold():
        raise ValueError("官方 DSH 安装器文件名与固定版本不匹配。")
    if path.stat().st_size != artifact.size_bytes:
        raise ValueError("官方 DSH 安装器大小与固定版本不匹配。")
    digest = hashlib.sha512()
    completed = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
            completed += len(block)
            if progress is not None:
                progress(completed, artifact.size_bytes)
    actual = digest.hexdigest()
    if actual != artifact.sha512_hex:
        raise ValueError("官方 DSH 安装器 SHA-512（文件摘要）与固定版本不匹配。")
    return actual


def _asar_virtual_path(value: object, expected: str, *, field: str) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError(f"官方 DSH {field}（路径字段）无效。")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or ":" in value
            or not value.startswith(expected)):
        raise ValueError(f"官方 DSH {field}（ASAR 归档路径）超出受管目录。")
    return value


def validate_official_runtime(runtime: Path, artifact: OfficialArtifact,
                              *, verify_required: bool = True) -> OfficialRuntimePaths:
    """Validate an installed official tree（官方目录）without launching Electron."""
    if runtime.is_symlink() or runtime.is_junction():
        raise ValueError("官方 DSH 运行目录不能是符号链接或目录联接。")
    runtime = runtime.resolve()
    if not runtime.is_dir():
        raise ValueError("官方 DSH 运行目录缺失。")
    metadata = _read_json(runtime / "runtime.json")
    record = _read_json(runtime / "manifest.json")
    if (metadata.get("schema") != 2
            or metadata.get("kind") != OFFICIAL_SOURCE_KIND
            or metadata.get("source_kind") != OFFICIAL_SOURCE_KIND
            or metadata.get("version") != artifact.version
            or metadata.get("bridge_protocol") != BRIDGE_PROTOCOL_VERSION
            or metadata.get("compat_expected_commit") != artifact.compatibility_commit
            or metadata.get("source_sha512") != artifact.sha512_hex
            or not _SHA512_HEX.fullmatch(str(metadata.get("source_sha512", "")))):
        raise ValueError("官方 DSH 运行产物版本或桥接协议与项目兼容锁不匹配。")
    if (metadata.get("executable") != OFFICIAL_EXECUTABLE
            or metadata.get("app_asar") != OFFICIAL_ASAR
            or metadata.get("entry") != OFFICIAL_CLI
            or metadata.get("schedule") != OFFICIAL_SCHEDULE
            or metadata.get("office_cli") != OFFICIAL_OFFICE_CLI
            or metadata.get("office_engine") != OFFICIAL_OFFICE_ENGINE
            or metadata.get("asar_unpacked_root") != OFFICIAL_ASAR_UNPACKED):
        raise ValueError("官方 DSH Electron（桌面框架）入口或运行路径与适配版本不匹配。")
    if record.get("schema") != 1 or record.get("kind") != "dsh" or not isinstance(record.get("files"), list):
        raise ValueError("官方 DSH 运行清单格式无效。")

    executable = safe_path(runtime, OFFICIAL_EXECUTABLE)
    asar = safe_path(runtime, OFFICIAL_ASAR)
    office_cli = safe_path(runtime, OFFICIAL_OFFICE_CLI)
    office_engine = safe_path(runtime, OFFICIAL_OFFICE_ENGINE)
    asar_unpacked = safe_path(runtime, OFFICIAL_ASAR_UNPACKED)
    if (not all(path.is_file() for path in (executable, asar, office_cli))
            or not asar_unpacked.is_dir() or not office_engine.is_dir()):
        raise ValueError("官方 DSH Electron、ASAR 或 Office（文档转换）组件不完整。")

    required = {
        "runtime.json", OFFICIAL_EXECUTABLE, OFFICIAL_ASAR, OFFICIAL_OFFICE_CLI,
        OFFICIAL_OFFICE_ENGINE + "/prebuilds.json",
    }
    files = record["files"]
    entries = [row for row in files if isinstance(row, dict) and row.get("path") in required]
    if {row.get("path") for row in entries} != required:
        raise ValueError("官方 DSH 完整性清单缺少 Electron、ASAR 或 Office 入口。")
    if verify_required:
        verify(runtime, {**record, "files": entries}, exact=False)
    cli_entry = runtime.joinpath(*PurePosixPath(OFFICIAL_CLI).parts)
    schedule_entry = _asar_virtual_path(OFFICIAL_SCHEDULE, OFFICIAL_ASAR + "/dsh/", field="schedule")
    _asar_virtual_path(OFFICIAL_CLI, OFFICIAL_ASAR + "/dsh/", field="entry")
    return OfficialRuntimePaths(
        executable=executable,
        cli_entry=cli_entry,
        desktop_cli_entry=runtime.joinpath(*PurePosixPath(OFFICIAL_DESKTOP_CLI).parts),
        asar=asar,
        schedule_entry=schedule_entry,
        office_cli=office_cli,
        office_engine=office_engine,
        metadata=metadata,
        manifest=record,
    )

"""Model receipts（模型安装收据）：区分已安装可用内容与随包目标内容。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

RECEIPT_NAME = "resource-install.json"  # RECEIPT_NAME（模型安装收据文件名）。
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class InstalledModel:
    """InstalledModel（已安装模型）：收据或旧根清单中的受管文件。"""

    version: str | None  # version（已安装版本）；旧清单没有版本时为空。
    files: tuple[dict[str, Any], ...]  # files（已安装的文件摘要记录）。
    error: str | None = None  # error（缺失、损坏或路径错误）。


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("模型收据包含无效路径。")
    path = PurePosixPath(value)
    if path.is_absolute() or ":" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("模型收据路径必须位于模型目录内。")
    return value


def _safe_file(root: Path, relative: str) -> Path:
    path = root
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError("模型目录不能是链接。")
    for part in PurePosixPath(_relative(relative)).parts:
        path = path / part
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("模型文件路径不能包含链接。")
    if not path.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise ValueError("模型文件路径越出模型目录。")
    return path


def receipt_digest(resource_id: str, files: list[dict[str, Any]]) -> str:
    """receipt_digest（收据内容摘要）：与原生资源管理器共用的排序编码。"""
    value = {"resource_id": resource_id, "files": sorted(files, key=lambda row: row["path"])}
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def model_receipt(resource_id: str, version: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    """model_receipt（模型安装收据）：只记录实际安装的模型文件。"""
    files = sorted(files, key=lambda row: row["path"])
    return {"schema": 1, "resource_id": resource_id, "version": version,
            "content_sha256": receipt_digest(resource_id, files), "files": files,
            "fields": {"schema": "收据格式版本", "resource_id": "模型资源编号", "version": "已安装模型版本",
                       "content_sha256": "已安装文件记录的 SHA-256（内容摘要）", "files": "已安装模型文件",
                       "path": "模型目录内相对路径", "size": "文件字节数", "sha256": "文件 SHA-256（摘要）",
                       "optional": "是否为可选文件", "fields": "字段中文说明"}}


def inspect_installed_model(models_root: Path, destination: str, resource_id: str,
                            *, verify_hashes: bool = False) -> InstalledModel:
    """inspect_installed_model（核验已安装模型）：兼容升级前没有收据的根清单。"""
    version = None
    records: list[dict[str, Any]] = []
    try:
        root = _safe_file(models_root, destination)
        receipt_path = _safe_file(root, RECEIPT_NAME)
        if receipt_path.is_file():
            value = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
            if (not isinstance(value, dict) or value.get("schema") != 1
                    or value.get("resource_id") != resource_id or not isinstance(value.get("version"), str)
                    or not isinstance(value.get("files"), list)):
                raise ValueError("模型安装收据格式无效。")
            version = value["version"]
            records = value["files"]
            if value.get("content_sha256") != receipt_digest(resource_id, records):
                raise ValueError("模型安装收据内容摘要不匹配。")
        else:
            manifest = json.loads(_safe_file(models_root, "manifest.json").read_text(encoding="utf-8-sig"))
            if (not isinstance(manifest, dict) or manifest.get("schema") != 1
                    or manifest.get("kind") != "models" or not isinstance(manifest.get("files"), list)):
                raise ValueError("模型安装清单格式无效。")
            prefix = _relative(destination) + "/"
            for row in manifest["files"]:
                if isinstance(row, dict) and isinstance(row.get("path"), str) and row["path"].startswith(prefix):
                    relative = row["path"][len(prefix):]
                    if relative != RECEIPT_NAME:
                        records.append({"path": relative, "size": row.get("size"),
                                        "sha256": row.get("sha256"), "optional": False})
        if not records:
            raise ValueError("模型安装清单没有该组件的文件记录。")
        seen: set[str] = set()
        for row in records:
            if not isinstance(row, dict):
                raise ValueError("模型安装文件记录格式无效。")
            relative = _relative(row.get("path"))
            key = relative.casefold()
            if (key in seen or relative == RECEIPT_NAME or type(row.get("size")) is not int
                    or row["size"] < 0 or not _DIGEST.fullmatch(str(row.get("sha256", "")))
                    or type(row.get("optional", False)) is not bool):
                raise ValueError("模型安装文件记录无效或重复。")
            seen.add(key)
            path = _safe_file(root, relative)
            if not path.is_file() and row.get("optional", False):
                continue
            if not path.is_file() or path.stat().st_size != row["size"]:
                raise ValueError(f"已安装模型文件缺失或大小不符：{relative}。")
            if verify_hashes:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != row["sha256"]:
                    raise ValueError(f"已安装模型文件摘要不符：{relative}。")
        return InstalledModel(version, tuple(records))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return InstalledModel(version, tuple(records), str(exc))


def model_update_available(spec: Any, installed: InstalledModel) -> bool:
    """model_update_available（模型可更新）：版本变化及已装文件变化独立于就绪状态。"""
    if installed.error or not installed.files:
        return False
    if installed.version is not None and installed.version != spec.version:
        return True
    current = {row["path"]: row for row in installed.files}
    target = {row.path: row for row in spec.files}
    for item in spec.files:
        prior = current.get(item.path)
        if prior is None:
            if not item.optional:
                return True
            continue
        if prior["size"] != item.size_bytes or prior["sha256"] != item.sha256:
            return True
    # 旧清单可能包含模型示例及来源说明；只清理明确由收据登记的旧可选资产。
    return any(row.get("optional", False) and row["path"] not in target for row in installed.files)

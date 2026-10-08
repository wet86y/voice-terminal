#!/usr/bin/env python3
"""重写迁移后已知应用数据中的旧工作区路径；不触碰原工作区或任意用户文档。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any


class RelocationError(Exception):
    """带稳定 code（错误编号）的安全迁移错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RelocationError("invalid_arguments", "参数缺失或格式无效。") from None


# PATH_KEYS（路径字段）：只改写这些 JSON 字段中以旧工作区根开头的绝对路径。
PATH_KEYS = frozenset({
    "path", "root", "cwd", "workspace", "workspace_root", "model_dir", "home_dir",
    "directory", "work_dir", "voice_path", "dsh_path", "manifest_path", "source_path",
    "source_ref", "archive_path", "keywords_file",
})

JSON_FILES = (
    "data/config/product-settings.json",
    "data/config/model-registry.json",
    "data/config/wake-tasks.json",
    "data/config/continuity-handoff.json",
    "data/config/continuity-state.json",
    "data/dsh/storages/voice-terminal-wake-tasks.json",
    "data/dsh/session-mappings.json",
    "data/history/pending-transfer.json",
)


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _check_path_chain(root: Path, relative: Path) -> Path:
    """Reject reparse points（重解析点）in every known path component."""
    current = root
    for part in relative.parts:
        current = current / part
        if _is_reparse(current):
            raise RelocationError("linked_target", "待处理路径经过链接或目录联接。")
    return current


def _validate_roots(source: Path, target: Path) -> tuple[Path, Path]:
    if not source.is_absolute() or not target.is_absolute():
        raise RelocationError("absolute_roots_required", "旧、新工作区根目录必须是绝对路径。")
    if _is_reparse(source) or _is_reparse(target):
        raise RelocationError("root_invalid", "旧、新工作区根目录不能是链接或目录联接。")
    try:
        source = source.resolve(strict=True)
        target = target.resolve(strict=True)
    except OSError as exc:
        raise RelocationError("root_unavailable", "旧或新工作区根目录不可用。") from exc
    if (not source.is_dir() or not target.is_dir()
            or _is_reparse(source) or _is_reparse(target)):
        raise RelocationError("root_invalid", "旧、新工作区根目录必须是普通目录。")
    source_text = os.path.normcase(os.path.normpath(str(source)))
    target_text = os.path.normcase(os.path.normpath(str(target)))
    try:
        common = os.path.normcase(os.path.normpath(os.path.commonpath((source_text, target_text))))
    except ValueError:
        common = ""
    if common in {source_text, target_text}:
        raise RelocationError("roots_overlap", "旧、新工作区根目录不能相同或互相包含。")
    return source, target


def _rewrite_root_prefix(value: str, source: Path, target: Path) -> tuple[str, bool]:
    """Rewrite one absolute path（绝对路径）whose path boundary is the source root."""
    old = str(source).rstrip("\\/") or source.anchor
    normalized_value = value.replace("/", "\\").casefold()
    normalized_old = old.replace("/", "\\").casefold()
    if not (normalized_value == normalized_old
            or normalized_value.startswith(normalized_old + "\\")):
        return value, False
    matched_prefix = value[:len(old)]
    suffix = value[len(old):]
    forward_style = "/" in matched_prefix and "\\" not in matched_prefix
    replacement = target.as_posix().rstrip("/") if forward_style else str(target).rstrip("\\/")
    return replacement + suffix, True


def _rewrite_json_value(value: Any, key: str, source: Path, target: Path) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        for child_key, child in list(value.items()):
            rewritten, item_changed = _rewrite_json_value(child, str(child_key), source, target)
            if item_changed:
                value[child_key] = rewritten
                changed = True
        return value, changed
    if isinstance(value, list):
        changed = False
        for index, child in enumerate(value):
            rewritten, item_changed = _rewrite_json_value(child, key, source, target)
            if item_changed:
                value[index] = rewritten
                changed = True
        return value, changed
    if isinstance(value, str) and key in PATH_KEYS:
        return _rewrite_root_prefix(value, source, target)
    return value, False


def _archive_prefixes(root: Path) -> tuple[str, str]:
    archive_root = root / "data" / "history" / "archives"
    native = str(archive_root).rstrip("\\/")
    forward = archive_root.as_posix().rstrip("/")
    return native, forward


def _rewrite_generated_archive_links(document: str, source: Path, target: Path) -> tuple[str, bool]:
    """Only rewrite generated archive file links（生成归档链接）inside the continuity note."""
    old_native, old_forward = _archive_prefixes(source)
    new_native, new_forward = _archive_prefixes(target)
    changed = False
    for old, new in dict.fromkeys(((old_native, new_native), (old_forward, new_forward))):
        pattern = re.compile(
            re.escape(old)
            + r"(?=[\\/][0-9a-f]{32}(?:[\\/]|$))",
            re.IGNORECASE,
        )
        document, count = pattern.subn(lambda _match: new, document)
        changed = changed or count > 0
    return document, changed


def _decode_json(path: Path) -> tuple[Any, bool]:
    content = path.read_bytes()
    has_bom = content.startswith(b"\xef\xbb\xbf")
    try:
        value = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RelocationError("json_invalid", "已知 JSON 配置格式无效，停止重写。") from exc
    return value, has_bom


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace one file atomically（原子替换单个文件）in its existing directory."""
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".relocating", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rewrite_json_file(path: Path, source: Path, target: Path, *, continuity_note: bool = False) -> bool:
    value, has_bom = _decode_json(path)
    value, changed = _rewrite_json_value(value, "", source, target)
    if continuity_note and isinstance(value, dict) and isinstance(value.get("document"), str):
        document, note_changed = _rewrite_generated_archive_links(value["document"], source, target)
        if note_changed:
            value["document"] = document
            changed = True
    if not changed:
        return False
    text = json.dumps(value, ensure_ascii=False, indent=2)
    original = path.read_bytes()
    if original.endswith((b"\n", b"\r")):
        text += "\n"
    content = (b"\xef\xbb\xbf" if has_bom else b"") + text.encode("utf-8")
    _atomic_write(path, content)
    return True


def _yaml_scalar_replacement(raw: str, style: str | None, value: str) -> str:
    if raw.startswith(("*", "&")):
        raise RelocationError("yaml_alias_unsupported", "路径字段使用 YAML 别名或锚点，无法安全局部重写。")
    if style == "'":
        return "'" + value.replace("'", "''") + "'"
    # JSON（结构化数据格式）字符串也是合法 YAML 双引号标量；仅替换目标值节点，注释和其他文本不动。
    return json.dumps(value, ensure_ascii=False)


def _rewrite_local_yaml(path: Path, source: Path, target: Path) -> bool:
    try:
        import yaml
        from yaml.nodes import MappingNode, ScalarNode, SequenceNode
    except ImportError as exc:
        raise RelocationError("yaml_unavailable", "随包 YAML 解析库不可用，未改写本机 YAML 配置。") from exc

    original_bytes = path.read_bytes()
    has_bom = original_bytes.startswith(b"\xef\xbb\xbf")
    try:
        text = original_bytes.decode("utf-8-sig")
        root = yaml.compose(text)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise RelocationError("yaml_invalid", "本机 YAML 配置格式无效，停止重写。") from exc
    if root is None:
        return False

    nodes: list[ScalarNode] = []

    def add_scalar(node: Any) -> None:
        if isinstance(node, ScalarNode) and node.tag == "tag:yaml.org,2002:str":
            nodes.append(node)

    def walk(node: Any, keys: tuple[str, ...] = ()) -> None:
        if not isinstance(node, MappingNode):
            if isinstance(node, SequenceNode):
                for child in node.value:
                    walk(child, keys)
            return
        for key_node, child in node.value:
            key = str(key_node.value) if isinstance(key_node, ScalarNode) else ""
            if keys == ("wake",) and key in {"model_dir", "keywords_file"}:
                add_scalar(child)
            elif keys == ("shadow_agent", "tools") and key == "allowed_roots":
                if isinstance(child, SequenceNode):
                    for item in child.value:
                        add_scalar(item)
            walk(child, keys + (key,))

    walk(root)
    replacements: list[tuple[int, int, str]] = []
    seen_spans: set[tuple[int, int]] = set()
    for node in nodes:
        rewritten, changed = _rewrite_root_prefix(node.value, source, target)
        if not changed:
            continue
        start, end = node.start_mark.index, node.end_mark.index
        if (start, end) in seen_spans:
            raise RelocationError("yaml_alias_unsupported", "路径字段通过 YAML 别名共享，无法安全局部重写。")
        seen_spans.add((start, end))
        raw = text[start:end]
        replacements.append((start, end, _yaml_scalar_replacement(raw, node.style, rewritten)))
    if not replacements:
        return False
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    _atomic_write(path, (b"\xef\xbb\xbf" if has_bom else b"") + text.encode("utf-8"))
    return True


def _known_files(target: Path) -> list[tuple[Path, bool]]:
    result: list[tuple[Path, bool]] = []
    for relative in JSON_FILES:
        result.append((Path(relative), relative == "data/config/continuity-state.json"))
    result.append((Path("data/config/local.yaml"), False))

    archive_root = Path("data/history/archives")
    archive_directory = _check_path_chain(target, archive_root)
    if archive_directory.exists():
        if not archive_directory.is_dir():
            raise RelocationError("archive_path_invalid", "历史归档路径不是目录。")
        for directory in sorted(archive_directory.iterdir(), key=lambda item: item.name.casefold()):
            if _is_reparse(directory):
                raise RelocationError("linked_target", "历史归档目录包含链接或目录联接。")
            if directory.is_dir():
                manifest = directory / "manifest.json"
                if manifest.exists():
                    result.append((archive_root / directory.name / "manifest.json", False))
    # config/local.yaml（本机 YAML 配置）使用字段级节点重写，不经过 JSON 处理。
    return result


def relocate(source_arg: Path, target_arg: Path) -> dict[str, Any]:
    source, target = _validate_roots(source_arg, target_arg)
    known = _known_files(target)
    changed_files: list[str] = []
    checked_files = 0
    for relative, continuity_note in known:
        path = _check_path_chain(target, relative)
        if not path.exists():
            continue
        if not path.is_file() or _is_reparse(path):
            raise RelocationError("known_file_invalid", "待处理配置不是普通文件或文件本身是链接。")
        checked_files += 1
        if relative.as_posix() == "data/config/local.yaml":
            changed = _rewrite_local_yaml(path, source, target)
        else:
            changed = _rewrite_json_file(path, source, target, continuity_note=continuity_note)
        if changed:
            changed_files.append(relative.as_posix())
    return {"changed_files": changed_files, "checked_files": checked_files}


def _write_json(value: dict[str, Any]) -> None:
    value.setdefault("fields", {
        "ok": "操作是否成功",
        "changed_files": "本次重写的目标工作区相对文件路径",
        "checked_files": "核对的已知配置文件数",
        "error": "安全错误对象",
        "code": "稳定错误编号",
        "message": "不包含配置内容或密钥的说明",
    })
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _parser() -> argparse.ArgumentParser:
    parser = Parser(description="重写迁移后已知配置内指向旧工作区的绝对路径。")
    parser.add_argument("--source-root", type=Path, required=True, help="source-root（旧工作区绝对根目录）")
    parser.add_argument("--target-root", type=Path, required=True, help="target-root（新工作区绝对根目录）")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = relocate(args.source_root, args.target_root)
        _write_json({"ok": True, "result": result})
        return 0
    except RelocationError as exc:
        _write_json({"ok": False, "error": {"code": exc.code, "message": exc.message}})
        return 2
    except OSError:
        _write_json({"ok": False, "error": {
            "code": "io_error", "message": "已知路径重写失败；未输出本机路径或配置内容。",
        }})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

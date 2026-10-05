"""Managed profile（受管配置档）：从自研产物生成默认模板，不读取个人宿主设置。"""
from __future__ import annotations

import json
from copy import deepcopy
import os
from pathlib import Path
import re
import shutil
import tempfile

from ..distribution import files
from ..paths import atomic_json

BUNDLES = ("voice-terminal-shadow-bridge", "voice-terminal-shadow-preset", "voice-terminal-wake-tasks")
NATIVE_SCHEDULE_BUNDLE = "@deepseek-ai/dsh-experimental-schedule-bundle"


def native_schedule_enabled(profile: Path) -> bool:
    """读取官方会话任务 Bundle（功能包）选择，与项目语音链路任务独立。"""
    path = profile / "package.json"
    if path.is_file():
        package = json.loads(path.read_text(encoding="utf-8-sig"))
        bundles = package.get("dsh", {}).get("profile", {}).get("bundles", [])
        return NATIVE_SCHEDULE_BUNDLE in bundles
    return False


def merge_profile_package(managed: dict, existing: dict) -> dict:
    """更新受管包版本，同时保留用户选择的可选 Bundle（功能包）和其他清单设置。"""
    result = deepcopy(existing)
    result.update({key: value for key, value in managed.items() if key not in ("dependencies", "dsh")})
    result["dependencies"] = {**existing.get("dependencies", {}), **managed.get("dependencies", {})}
    current_dsh, managed_dsh = existing.get("dsh", {}), managed.get("dsh", {})
    current_profile, managed_profile = current_dsh.get("profile", {}), managed_dsh.get("profile", {})
    selected = [*managed_profile.get("bundles", []), *current_profile.get("bundles", [])]
    profile = {**current_profile, **managed_profile, "bundles": list(dict.fromkeys(selected))}
    result["dsh"] = {**current_dsh, **managed_dsh, "profile": profile}
    return result


def migrate_schedule_patch(path: Path, *, native_enabled: bool = False) -> None:
    """原生功能包关闭时仅清理旧禁用行；原生功能包启用时保留它的用户补丁。"""
    if native_enabled or not path.is_file():
        return
    original = path.read_text(encoding="utf-8-sig")
    text = original
    for name in ("schedule", "ui-schedule"):
        def remove_legacy(match: re.Match) -> str:
            block = match.group()
            lines = [line.split("#", 1)[0].rstrip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]
            return "" if len(lines) == 2 and re.fullmatch(r"[ \t]+disabled:[ \t]+true", lines[1]) else block
        text = re.sub(rf"(?ms)^- id: {re.escape(name)}[ \t]*\r?\n.*?(?=^- id:|\Z)", remove_legacy, text)
    if text != original:
        descriptor, temporary = tempfile.mkstemp(prefix=".schedule-migration-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text.rstrip() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def build_profile(program: Path, bundles: Path, destination: Path) -> None:
    """构建仅含默认补丁及已编译插件的配置档；不复制旧依赖和用户凭据。"""
    manifest_path = destination / "package.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path.is_file() else {}
    dependencies = {}
    for name in BUNDLES:
        source = bundles / name
        package = json.loads((source / "package.json").read_text(encoding="utf-8-sig"))
        package_name = package["name"]
        target = destination / "node_modules" / package_name
        for path in files(source):
            relative = path.relative_to(source)
            if "node_modules" in relative.parts or relative.suffix == ".map":
                continue
            copy = target / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, copy)
        dependencies[package_name] = package["version"]
    managed = {
        "name": "voice-terminal-managed-profile", "private": True,
        "dependencies": dependencies,
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", *dependencies], "patchReload": "live"}},
    }
    atomic_json(manifest_path, merge_profile_package(managed, existing))
    text = (program / "config/dsh/web-profile.patch.yml").read_text(encoding="utf-8-sig")
    patch = destination / "cordis.patch.yml"
    if not patch.exists():
        patch.write_text(text, encoding="utf-8")
    migrate_schedule_patch(patch, native_enabled=native_schedule_enabled(destination))

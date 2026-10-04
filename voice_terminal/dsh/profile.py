"""Managed profile（受管配置档）：从自研产物生成默认模板，不读取个人宿主设置。"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

from ..distribution import files
from ..paths import atomic_json

BUNDLES = ("voice-terminal-shadow-bridge", "voice-terminal-shadow-preset", "voice-terminal-wake-tasks")


def build_profile(program: Path, bundles: Path, destination: Path) -> None:
    """构建仅含默认补丁及已编译插件的配置档；不复制旧依赖和用户凭据。"""
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
    atomic_json(destination / "package.json", {
        "name": "voice-terminal-managed-profile", "private": True,
        "dependencies": dependencies,
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", *dependencies], "patchReload": "live"}},
    })
    text = (program / "config/dsh/web-profile.patch.yml").read_text(encoding="utf-8-sig")
    for name in ("schedule", "ui-schedule"):
        pattern = rf"(?ms)^- id: {re.escape(name)}[ \t]*\r?\n.*?(?=^- id:|\Z)"
        replacement = f"- id: {name}\n  disabled: true\n"
        text = re.sub(pattern, replacement, text) if re.search(pattern, text) else text.rstrip() + "\n\n" + replacement
    patch = destination / "cordis.patch.yml"
    if not patch.exists():
        patch.write_text(text, encoding="utf-8")

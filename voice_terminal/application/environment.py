"""Business environment（业务环境）：脚本与独立应用读取同一激活记录。"""
from __future__ import annotations

import json
from pathlib import Path
import shutil


def activated_python(root: Path) -> str | None:
    profile = root / ".runtime/application/runtime.json"
    if not profile.is_file():
        return None
    try:
        data = json.loads(profile.read_text(encoding="utf-8-sig"))
        value = data.get("python")  # 已激活的业务程序运行环境路径。
        if not isinstance(value, str) or not value.strip():
            raise ValueError("缺少业务程序运行环境路径")
        executable = (root / value).resolve()
        marker = executable.parent.parent / "voice-runtime-ready.json"
        ready = json.loads(marker.read_text(encoding="utf-8-sig"))
        if not executable.is_file() or ready.get("validated") is not True:
            raise ValueError("业务程序运行环境尚未完成依赖校验")
        return str(executable)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("已激活的业务环境不可用；请修复环境或通过 --python（明确指定程序运行环境）回退") from exc


def resolve_python(root: Path, explicit: str = "") -> str:
    if explicit:
        choices = [explicit]
    else:
        active = activated_python(root)
        managed = root / ".venv/Scripts/python.exe"
        ready = root / ".venv/voice-runtime-ready.json"
        choices = [active or "", str(managed) if ready.is_file() else "", shutil.which("python") or ""]
    for candidate in choices:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
        if candidate and shutil.which(candidate):
            return shutil.which(candidate) or candidate
    raise RuntimeError("未找到业务 Python（程序运行环境），请通过 --python 指定现有 Python 3.12。")

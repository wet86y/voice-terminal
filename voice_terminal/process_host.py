"""Process host（项目进程入口）：统一受管解释器身份，保留实际运行库路径。"""
from __future__ import annotations

import os
from pathlib import Path


def interpreter_command(executable: str, *arguments: str) -> list[str]:
    """interpreter_command（解释器命令）：桌面派生进程复用产品宿主，独立脚本沿用解释器。"""
    host = os.environ.get("VOICE_PROCESS_HOST")  # VOICE_PROCESS_HOST（当前桌面宿主的绝对路径）。
    if os.name == "nt" and host:
        host_path = Path(host)
        if not host_path.is_absolute() or host_path.name.lower() != "voiceterminaldesktop.exe" or not host_path.is_file():
            raise RuntimeError("项目工作进程宿主路径无效。")
        python = Path(executable)
        # 导入的外部环境可能没有相邻动态库，继续使用原有解释器，避免改变环境支持范围。
        if python.is_absolute() and python.is_file() and (python.parent / "python312.dll").is_file():
            return [str(host_path), "--worker-host", str(python), *map(str, arguments)]
    return [str(executable), *map(str, arguments)]

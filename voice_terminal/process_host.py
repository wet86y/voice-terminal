"""Process host（项目进程入口）：统一受管解释器身份，保留实际运行库路径。"""
from __future__ import annotations

import os
from pathlib import Path


def configure_background_subprocesses() -> None:
    """后台解释器统一隐藏控制台子进程，覆盖业务和第三方依赖的调用。"""
    if os.name != "nt":
        return
    import functools
    import inspect
    import subprocess

    original = subprocess.Popen.__init__
    if getattr(original, "_voice_background", False):
        return
    signature = inspect.signature(original)

    @functools.wraps(original)
    def initialize(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        flags = bound.arguments.get("creationflags", 0)  # creationflags（进程创建标志）。
        # CREATE_NEW_CONSOLE（新建控制台）和 DETACHED_PROCESS（脱离控制台）表示调用方的显式选择。
        if not flags & (subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS):
            bound.arguments["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
        return original(*bound.args, **bound.kwargs)

    initialize._voice_background = True  # _voice_background（后台策略已安装，避免重复包装）。
    subprocess.Popen.__init__ = initialize


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

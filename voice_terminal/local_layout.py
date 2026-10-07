"""Local layout（本机布局）：唯一配置、固定组件位置与仓库定位。"""
from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA = 2  # schema（便携配置格式）。
COMPONENTS = {"app": "app", "python": "runtimes/python/python.exe", "models": "models",
              "dsh": "runtimes/dsh", "webview2": "runtimes/webview2"}


def project_root(start: Path | None = None) -> Path:
    """按工程标识定位根目录，避免脚本搬迁改变父目录层数。"""
    current = (start or Path(__file__)).resolve()
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").is_file() and (parent / "voice_terminal").is_dir():
            return parent
    raise FileNotFoundError("无法定位 Voice Terminal（语音终端）工程根目录")


def local_root() -> Path:
    """运行时使用安装上下文；源码工具默认使用项目内 .local（本机资源）。"""
    if value := os.environ.get("VOICE_INSTALL_ROOT"):
        return Path(value).resolve()
    program = project_root()
    if (program / "apps/desktop/Cargo.toml").is_file():
        return program / ".local"
    if (program.parent / "portable.json").is_file():
        return program.parent
    return program / ".local"


def default_portable() -> dict:
    return {"schema": SCHEMA, "data_dir": "data", **COMPONENTS,
            "fields": {"schema": "便携配置格式", "data_dir": "唯一用户数据目录", "app": "正式程序目录",
                       "python": "后端解释器", "models": "必要模型集合", "dsh": "代理生产宿主",
                       "webview2": "固定网页渲染引擎"}}


def assert_launch_ready(root: Path) -> None:
    """启动事务门槛：中断或尚未收尾的更新不能作为业务组合使用。"""
    if (root / 'tmp/update-transaction.json').exists():
        raise RuntimeError('存在未完成更新；先执行 npm run desktop:publish（覆盖发布）恢复或收尾，再启动应用')


def read_portable(root: Path) -> dict:
    """只读取 portable.json（便携配置），不读取启用副本或外部资源退路。"""
    root = root.resolve()
    value = json.loads((root / "portable.json").read_text(encoding="utf-8-sig"))
    if value.get("schema") != SCHEMA:
        raise ValueError("需要 schema=2（第二版）便携配置；旧布局请先执行迁移")
    for key, relative in {"data_dir": "data", **COMPONENTS}.items():
        if value.get(key) != relative:
            raise ValueError(f"固定组件路径不符：{key}（路径字段）")
        path = root / relative
        if path.resolve().is_relative_to(root) is False:
            raise ValueError(f"组件路径越界：{key}（路径字段）")
    return value

"""Local files（本机文件管理）：范围限定清理与唯一持久数据备份。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat

from .paths import atomic_json

PERSISTENT = ("config", "persona", "speaker", "history", "dsh", "workspace", "imports", "state", ".env.local")
EXCLUDED = {"node_modules", "cache", "logs", "__pycache__", ".git", ".pnpm-store"}


def reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def extended(path: Path) -> str:
    value = str(path.absolute())
    return "\\\\?\\" + value if os.name == "nt" and not value.startswith("\\\\?\\") else value


def remove_tree(path: Path, boundary: Path) -> None:
    """先核对绝对路径；逐层删除重解析点本身，不进入其目标。"""
    path = Path(os.path.abspath(path))
    boundary = boundary.resolve()
    if path == boundary or not path.is_relative_to(boundary):
        raise ValueError("清理目标必须位于指定根目录内")
    for parent in path.parents:
        if parent == boundary:
            break
        if parent.exists() and reparse(parent):
            raise ValueError("清理路径的父目录不能是重解析点")
    if not os.path.lexists(extended(path)):
        return
    stack = [(extended(path), False)]
    while stack:
        name, visited = stack.pop()
        info = os.lstat(name)
        is_link = stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
        if is_link:
            (os.rmdir if stat.S_ISDIR(info.st_mode) else os.unlink)(name)
        elif stat.S_ISDIR(info.st_mode):
            if visited:
                os.rmdir(name)
            else:
                stack.append((name, True))
                with os.scandir(name) as entries:
                    stack.extend((entry.path, False) for entry in entries)
        else:
            try:
                os.unlink(name)
            except PermissionError:
                os.chmod(name, stat.S_IWRITE)
                os.unlink(name)


def persistent_files(data: Path):
    """持久文件白名单；依赖联接、纯缓存及日志不进入数据备份。"""
    for item in PERSISTENT:
        entry = data / item
        if not entry.exists() or reparse(entry):
            continue
        stack = [entry]
        while stack:
            path = stack.pop()
            if reparse(path) or path.name in EXCLUDED:
                continue
            if path.is_dir():
                stack.extend(path.iterdir())
            elif path.is_file() and path.suffix not in {".pyc", ".log"}:
                yield path


def backup_user_data(root: Path, *, data: Path | None = None) -> dict:
    """唯一滚动备份：新内容校验成功后替换旧内容，重命名不复制旧备份。"""
    from .distribution import digest
    data = data or root / "data"
    stage = root / "tmp/user-backup.new"
    target = root / "backups/user-data"
    old = root / "tmp/user-backup.old"
    # 中断恢复：旧备份仍是唯一已验证副本时，先复原。
    if old.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        old.rename(target)
    remove_tree(stage, root)
    stage.mkdir(parents=True)
    records = []
    for path in persistent_files(data):
        relative = path.relative_to(data)
        copy = stage / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)
        checksum = digest(path)
        if digest(copy) != checksum:
            raise ValueError("用户数据备份校验失败")
        records.append({"path": relative.as_posix(), "size": path.stat().st_size, "sha256": checksum})
    record = {"schema": 1, "source": str(data.resolve()), "files": records, "fields": {"schema": "备份格式", "source": "权威数据来源路径", "files": "持久文件清单",
              "path": "相对用户数据路径", "size": "字节数", "sha256": "内容摘要"}}
    atomic_json(stage / "backup.json", record)
    target.parent.mkdir(parents=True, exist_ok=True)
    remove_tree(old, root)
    if target.exists():
        target.rename(old)
    try:
        stage.rename(target)
    except BaseException:
        if old.exists() and not target.exists():
            old.rename(target)
        raise
    remove_tree(old, root)
    return record


def clean_component_bytecode(root: Path) -> int:
    """只回收清单外的解释器字节码；其它陈旧或未知文件必须交给发布校验拒绝。"""
    from .distribution import files
    from .local_layout import read_portable
    selected = read_portable(root)
    directory = (root / selected['python']).parent
    record = json.loads((directory / 'manifest.json').read_text(encoding='utf-8-sig'))
    expected = {row['path'] for row in record['files']} | {'manifest.json'}
    extra = [p for p in files(directory) if p.relative_to(directory).as_posix() not in expected]
    if any(p.suffix != '.pyc' or '__pycache__' not in p.relative_to(directory).parts for p in extra):
        raise ValueError('解释器存在清单外的非缓存文件；拒绝自动清理')
    for path in extra:
        remove_tree(path, root)
    return len(extra)

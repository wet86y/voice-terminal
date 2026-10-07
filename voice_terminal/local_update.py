"""Update transaction（覆盖更新事务）：同盘换入、原位恢复、中断收尾。"""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path

from .distribution import safe_path, verify_install
from .local_files import remove_tree, reparse
from .paths import atomic_json

ALLOWED = {'app', 'runtimes/python', 'runtimes/dsh', 'runtimes/webview2', 'models',
           'VoiceTerminal.exe', 'VoiceTerminalUIDemo.exe', 'Verify-Package.ps1', '使用与环境说明.md', 'portable.json'}


def managed_path(root: Path, relative: str) -> Path:
    if relative not in ALLOWED:
        raise ValueError('更新目标不在固定组件白名单内')
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.exists() and reparse(current):
            raise ValueError('更新组件不能使用重解析点')
    return safe_path(root, relative)


@contextmanager
def update_lock(root: Path):
    """Lock（更新互斥锁）：校验进程身份后清理中断遗留，阻止并发发布。"""
    import psutil
    path = root / 'tmp/update.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        try:
            process = psutil.Process(previous['pid'])
            if process.create_time() == previous['created']:
                raise RuntimeError('另一个发布事务正在运行')
        except psutil.NoSuchProcess:
            pass
        path.unlink()
    with path.open('x', encoding='utf-8') as stream:
        # pid（进程标识）、created（创建时间）共同标识锁持有者。
        json.dump({'pid': os.getpid(), 'created': psutil.Process().create_time()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def recover(root: Path) -> str:
    """调用方持有互斥锁；记录未提交则恢复旧组合，已提交则完成回收。"""
    journal = root / 'tmp/update-transaction.json'
    if not journal.exists():
        return 'clean'
    record = json.loads(journal.read_text(encoding='utf-8'))
    if record.get('schema') != 1 or record.get('state') not in {'prepared', 'installing', 'committed'}:
        raise ValueError('更新事务记录格式无效')
    stage = root / 'tmp/publish'
    # 先验证全部路径，不能执行部分恢复后才发现记录越界。
    for row in record['items']:
        managed_path(root, row['path'])
    if record['state'] != 'committed':
        for row in reversed(record['items']):
            target = managed_path(root, row['path'])
            old = stage / 'old' / row['path']
            incoming = stage / 'new' / row['path']
            if old.exists():
                remove_tree(target, root)
                old.rename(target)
            elif not row['original'] and target.exists() and not incoming.exists():
                remove_tree(target, root)
    remove_tree(stage, root)
    journal.unlink()
    return 'finalized' if record['state'] == 'committed' else 'restored'


def install(root: Path, paths: list[str], *, validate=verify_install, stopped=lambda: None,
            checkpoint=lambda step: None) -> None:
    """候选必须先完整校验；checkpoint（故障注入检查点）用于真实事务验收。"""
    root = root.resolve()
    stage = root / 'tmp/publish'
    rows = []
    for relative in paths:
        target = managed_path(root, relative)
        if not (stage / 'new' / relative).exists():
            raise FileNotFoundError('待换入组件缺失')
        rows.append({'path': relative, 'original': target.exists()})
    if len(set(paths)) != len(paths):
        raise ValueError('更新目标不能重复')
    stopped()
    journal = root / 'tmp/update-transaction.json'
    if journal.exists():
        raise RuntimeError('必须先恢复未完成事务，再构建新候选')
    record = {'schema': 1, 'state': 'prepared', 'items': rows,
              'fields': {'state': '事务状态', 'items': '固定换入路径', 'path': '组件相对路径', 'original': '原组件是否存在'}}
    atomic_json(journal, record)
    try:
        record['state'] = 'installing'
        atomic_json(journal, record)
        for row in rows:
            relative = row['path']
            target = managed_path(root, relative)
            old = stage / 'old' / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            if row['original']:
                target.rename(old)
            checkpoint('renamed:' + relative)
            (stage / 'new' / relative).rename(target)
            checkpoint('installed:' + relative)
        validate(root)
        checkpoint('verified')
        record['state'] = 'committed'
        atomic_json(journal, record)
    except Exception:
        recover(root)
        raise
    recover(root)

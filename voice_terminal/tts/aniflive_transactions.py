"""AnifLive 组件专用的跨进程互斥与隔离依赖目录恢复。"""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import time
from .aniflive_native import contained


@contextmanager
def task_guard(local: Path):
    local = Path(local).absolute()
    path = contained(local, local / 'tmp/aniflive-task.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        waiting_reported = False
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not waiting_reported:
                    print(json.dumps({'phase': '等待现有 AnifLive（本地语音）组件任务结束',
                                      'completed_bytes': 0, 'total_bytes': 0}, ensure_ascii=False), flush=True)
                    waiting_reported = True
                time.sleep(.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def recover_dependencies(runtime: Path):
    """仅由显式组件任务恢复；普通启动遇到未完成事务会要求组件修复。"""
    journal = contained(runtime, runtime / 'dependency-transaction.json')
    if not journal.exists():
        return
    value = json.loads(journal.read_text(encoding='utf-8'))
    if value.get('schema') != 1 or value.get('state') not in {'prepared', 'committed'}:
        raise ValueError('隔离依赖恢复记录无效，已保留现场。')
    rows = value.get('items')
    if not isinstance(rows, list) or not rows:
        raise ValueError('隔离依赖恢复记录缺少目录。')
    for row in rows:
        if row.get('target') not in {'packages', 'export-packages', 'source'} or type(row.get('original')) is not bool:
            raise ValueError('隔离依赖恢复目标不在固定范围。')
        for key in ('backup', 'stage'):
            name = row.get(key)
            if not isinstance(name, str) or '/' in name or '\\' in name or not name.startswith('.' + row['target'] + '-'):
                raise ValueError('隔离依赖恢复临时目录身份不符。')
            contained(runtime, runtime / name)
    for row in reversed(rows):
        target, backup = (contained(runtime, runtime / row[key]) for key in ('target', 'backup'))
        if value['state'] == 'prepared':
            if backup.exists():
                if target.exists():
                    shutil.rmtree(target)
                os.rename(backup, target)
            elif not row['original'] and target.exists():
                shutil.rmtree(target)
        elif backup.exists():
            shutil.rmtree(backup)
        stage = contained(runtime, runtime / row['stage'])
        if stage.exists():
            shutil.rmtree(stage)
    journal.unlink()

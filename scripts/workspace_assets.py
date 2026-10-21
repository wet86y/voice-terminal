"""Development assets（开发资产）：受控任务生命周期及只读容量测量。"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def task_folder(kind: str) -> str:
    """task_folder（任务类型短目录）：为 Windows（操作系统）深路径留出空间。"""
    if not re.fullmatch('[a-z][a-z0-9-]{0,63}', kind):
        raise ValueError('任务名称只接受英文小写、数字及连字符')
    return {'backend-suite': 'b', 'app-update-handoff': 'u', 'tts-voice-design': 'v'}.get(
        kind, hashlib.sha256(kind.encode()).hexdigest()[:8])


def maintenance_warning(error) -> None:
    print('资产维护提示（不改变验证结果）：' + str(error), file=sys.stderr)


def guarded(root: Path, relative: str) -> Path:
    """guarded（边界检查）：拒绝越界及任一现存祖先或目标重解析点。"""
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(root / relative))
    if not path.is_relative_to(root):
        raise ValueError('资产路径越出工作区')
    for item in [root, *list(path.relative_to(root).parents)[::-1], path]:
        item = item if item.is_absolute() else root / item
        if os.path.lexists(item):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('资产路径或祖先是重解析点：' + str(item))
    return path


def write_record(root: Path, relative: str, value: dict) -> None:
    from voice_terminal.paths import atomic_json
    path = guarded(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)


def policy(root: Path = ROOT) -> dict:
    value = json.loads(guarded(root, 'config/development-assets.json').read_text(encoding='utf-8'))
    if value['schema'] != 1:
        raise ValueError('不支持的开发资产策略版本')
    return value


def measure(path: Path) -> dict:
    """measure（逻辑体积）：不跟随目录联接或文件符号链接。"""
    result = {'bytes': 0, 'files': 0, 'reparse_points': 0, 'errors': []}
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                result['reparse_points'] += 1
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as entries:
                    stack.extend(Path(entry.path) for entry in entries)
            elif stat.S_ISREG(info.st_mode):
                result['bytes'] += info.st_size
                result['files'] += 1
        except FileNotFoundError:
            continue
        except OSError as error:
            result['errors'].append(str(error))
    return result


def owner_alive(pid: int) -> bool:
    """owner_alive（持有进程存活）：无法确认时保留，绝不向进程发送终止信号。"""
    if pid <= 0:
        return True
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER（进程不存在）。
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def sweep_tasks(root: Path = ROOT, *, apply: bool = False, now: float | None = None, kind: str | None = None) -> dict:
    """sweep_tasks（登记现场回收）：未知、存活、路径异常的现场始终保留。"""
    from voice_terminal.local_files import remove_tree
    now = time.time() if now is None else now
    limits = policy(root)['tasks']
    base = guarded(root, '.local/tmp/tasks')
    result = {'candidates': [], 'removed': [], 'skipped': [], 'fields': {
        'candidates': '满足保留期限、数量或容量策略的已登记现场', 'removed': '已回收的具体目录',
        'skipped': '活动、未知或无法安全处理的目录', 'path': '工作区内具体路径', 'reason': '处理原因'}}
    if not base.exists():
        return result
    selected = [guarded(root, '.local/tmp/tasks/' + task_folder(kind))] if kind is not None else list(base.iterdir())
    for kind in selected:
        if not kind.exists():
            continue
        try:
            guarded(root, kind.relative_to(root).as_posix())
            if not kind.is_dir() or not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', kind.name):
                continue
            rows = []
            for run in kind.iterdir():
                relative = run.relative_to(root).as_posix()
                try:
                    if not re.fullmatch('(?:[0-9a-f]{8}|[0-9a-f]{32})', run.name):
                        raise ValueError('未知任务目录')
                    record_path = guarded(root, relative + '/asset-run.json')
                    record = json.loads(record_path.read_text(encoding='utf-8'))
                    if (record.get('schema') != 1 or record.get('owner') != 'voice-terminal-development'
                            or kind.name not in {record.get('kind'), task_folder(record.get('kind', ''))}
                            or record.get('id') != run.name
                            or record.get('state') not in {'running', 'failed', 'succeeded'}):
                        raise ValueError('未识别的任务登记')
                    if owner_alive(int(record['pid'])):
                        raise ValueError('持有进程仍存活或无法确认')
                    if record['state'] == 'running':
                        raise ValueError('中断任务未确认子进程退出，保留现场供显式复核')
                    size = measure(run)
                    if size['errors']:
                        raise ValueError('目录测量失败')
                    rows.append((float(record.get('finished_at', record['started_at'])), relative, record, size['bytes']))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    result['skipped'].append({'path': relative, 'reason': str(error)})
            retained = total = 0
            for ended, relative, record, size in sorted(rows, reverse=True):
                expired = now - ended > limits['max_age_days'] * 86400
                discard = (record['state'] == 'succeeded' and not record.get('keep_success', False)) or expired
                if not discard:
                    discard = retained + 1 > limits['keep_failed'] or total + size > limits['max_failed_bytes']
                    if not discard:
                        retained += 1
                        total += size
                if discard:
                    result['candidates'].append(relative)
                    if apply:
                        try:
                            guarded(root, relative)
                            fresh_path = guarded(root, relative + '/asset-run.json')
                            fresh = json.loads(fresh_path.read_text(encoding='utf-8'))
                            if fresh != record or owner_alive(int(fresh['pid'])):
                                raise ValueError('任务登记或持有进程状态已改变')
                            remove_tree(root / relative, root / '.local/tmp/tasks')
                            result['removed'].append(relative)
                        except (OSError, ValueError) as error:
                            result['skipped'].append({'path': relative, 'reason': str(error)})
        except (OSError, ValueError) as error:
            result['skipped'].append({'path': kind.relative_to(root).as_posix(), 'reason': str(error)})
    return result


class ManagedRun:
    """ManagedRun（受管验证任务）：成功回收，失败登记，后续入口限量回收。"""

    def __init__(self, kind: str, root: Path = ROOT, *, keep_success: bool = False):
        if not re.fullmatch('[a-z][a-z0-9-]{0,63}', kind):
            raise ValueError('任务名称只接受英文小写、数字及连字符')
        self.root = Path(root)
        self.kind = kind
        try:
            sweep_tasks(self.root, apply=True, kind=kind)
        except (OSError, ValueError, KeyError, TypeError) as error:
            maintenance_warning(error)
        for attempt in range(5):
            self.relative = f'.local/tmp/tasks/{task_folder(kind)}/{uuid.uuid4().hex[:8]}'
            self.path = guarded(self.root, self.relative)
            try:
                self.path.mkdir(parents=True)
                break
            except FileExistsError:
                continue
        else:
            raise OSError('无法分配独立的短路径任务目录')
        self.record = {'schema': 1, 'owner': 'voice-terminal-development', 'kind': kind,
                       'id': self.path.name, 'pid': os.getpid(), 'started_at': time.time(), 'state': 'running',
                       'keep_success': keep_success,
                       'fields': {'schema': '任务登记版本', 'owner': '资产管理所有者', 'kind': '任务类型',
                                  'id': '独立任务标识', 'pid': '持有进程编号，存活期间不回收', 'started_at': '开始时间戳',
                                  'finished_at': '结束时间戳', 'state': '运行中、失败或成功', 'exit_code': '退出码',
                                  'keep_success': '显式保留成功现场供听音或人工检查，仍受已结束现场预算约束'}}
        self.registered = False
        try:
            self._write()
            self.registered = True
        except (OSError, ValueError) as error:
            maintenance_warning(error)

    def _write(self):
        write_record(self.root, self.relative + '/asset-run.json', self.record)

    def finish(self, exit_code: int = 0):
        from voice_terminal.local_files import remove_tree
        if not self.registered:
            maintenance_warning('未登记现场保留供复核：' + self.relative)
            return
        self.record.update(state='succeeded' if exit_code == 0 else 'failed',
                           finished_at=time.time(), exit_code=exit_code)
        try:
            self._write()
            # 固定摘要覆盖写；维护失败仅提示，不能掩盖原始测试结果或异常。
            write_record(self.root, f'.local/cache/reports/tasks/{self.kind}/latest.json', self.record)
            if exit_code == 0 and not self.record['keep_success']:
                guarded(self.root, self.relative)
                remove_tree(self.path, self.root / '.local/tmp/tasks')
        except (OSError, ValueError) as error:
            maintenance_warning(error)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None and not issubclass(kind, Exception):
            # 强制中断或异步取消不能证明所属子进程已退出；保持运行中登记，等待显式复核。
            return
        self.finish(1 if kind else 0)

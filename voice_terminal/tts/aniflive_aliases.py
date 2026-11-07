"""Windows（操作系统）临时盘符的前置归属收据及强制退出后回收。"""
from __future__ import annotations
import ctypes
import json
import os
import math
from pathlib import Path
import re
import uuid
from .aniflive_native import contained, _alias_mutex


def record_alias(local: Path, drive: str, target: str) -> Path:
    """映射建立前持久登记；崩溃不能留下没有收据的本任务映射。"""
    import psutil
    if not re.fullmatch('[D-Z]:', drive):
        raise ValueError('临时盘符不在允许范围。')
    contained(local, Path(target.removeprefix('\\??\\')))
    folder = contained(local, local / 'tmp/aniflive-parser-aliases')
    folder.mkdir(parents=True, exist_ok=True)
    path = contained(local, folder / (drive[0] + '.json'))
    value = {'schema': 1, 'pid': os.getpid(), 'created_at': psutil.Process().create_time(),
             'drive': drive, 'target': target, 'task_id': uuid.uuid4().hex,
             'fields': {'pid': '创建别名的进程编号', 'created_at': '进程创建时间',
                        'drive': '本任务临时盘符', 'target': '精确原始目标', 'task_id': '本次映射归属编号'}}
    temporary = contained(local, folder / ('.' + drive[0] + '-' + value['task_id'] + '.pending'))
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        # Windows（操作系统）rename（重命名）在目标已存在时拒绝替换。
        # 最终收据出现之前已经完整落盘；强杀写入只留下无映射的临时文件。
        os.rename(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _query(kernel, drive):
    query = kernel.QueryDosDeviceW
    query.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    query.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    if query(drive, buffer, len(buffer)):
        return buffer.value
    if ctypes.get_last_error() == 2:
        return None
    raise ctypes.WinError(ctypes.get_last_error())


def cleanup_aliases(local: Path) -> dict:
    """只回收失活进程且当前目标精确匹配的收据；活动和无权核对的定义保留。"""
    result = {'removed': [], 'active': [], 'fields': {'removed': '本次精确回收的失活盘符',
                                                   'active': '进程仍活动或无权核对而保留的盘符'}}
    if os.name != 'nt':
        return result
    import psutil
    local = Path(local).absolute()
    folder = contained(local, local / 'tmp/aniflive-parser-aliases')
    if not folder.exists():
        return result
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    define = kernel.DefineDosDeviceW
    define.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p]
    define.restype = ctypes.c_int
    with _alias_mutex():
        for path in sorted(folder.glob('*.json')):
            contained(local, path)
            value = json.loads(path.read_text(encoding='utf-8'))
            drive, target, pid, born = (value.get(key) for key in ('drive', 'target', 'pid', 'created_at'))
            if (value.get('schema') != 1 or not isinstance(drive, str) or not re.fullmatch('[D-Z]:', drive)
                    or path.name != drive[0] + '.json' or not isinstance(target, str) or not target.startswith('\\??\\')
                    or type(pid) is not int or pid <= 0 or type(born) not in (int, float) or not math.isfinite(born) or born <= 0
                    or not re.fullmatch('[a-f0-9]{32}', str(value.get('task_id', '')))):
                raise ValueError('临时盘符归属收据无效，已保留现场。')
            contained(local, Path(target[4:]))
            try:
                if abs(psutil.Process(pid).create_time() - born) < .01:
                    result['active'].append(drive)
                    continue
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                result['active'].append(drive)
                continue
            actual = _query(kernel, drive)
            if actual is not None:
                if actual != target:
                    raise ValueError('临时盘符实际目标与本任务收据不符，拒绝撤销其他映射。')
                if not define(7, drive, target):
                    raise ctypes.WinError(ctypes.get_last_error())
                if _query(kernel, drive) == target:
                    raise RuntimeError('本任务临时盘符撤销后仍存在。')
            path.unlink()
            result['removed'].append(drive)
    return result

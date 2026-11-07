"""真实 Windows（操作系统）进程强杀后的临时盘符归属与回收。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
from voice_terminal.tts.aniflive_aliases import cleanup_aliases

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows（操作系统）临时盘符验证')

CHILD = r'''
import ctypes,json,sys,time
from pathlib import Path
from voice_terminal.tts.aniflive_aliases import record_alias,_query
from voice_terminal.tts.aniflive_native import _alias_mutex
root=Path(sys.argv[1]); target=root/'中文计算图'; target.mkdir()
(target/'model.onnx').write_bytes(b'model')
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
define=kernel.DefineDosDeviceW
define.argtypes=[ctypes.c_uint,ctypes.c_wchar_p,ctypes.c_wchar_p]; define.restype=ctypes.c_int
with _alias_mutex():
    drive=next(letter+':' for letter in 'ZYXWVUTSRQPONMLKJIHGFED' if _query(kernel,letter+':') is None)
    raw='\\??\\'+str(target)
    receipt=record_alias(root,drive,raw)
    if not define(1,drive,raw): raise ctypes.WinError(ctypes.get_last_error())
    (root/'ready.json').write_text(json.dumps({'drive':drive,'target':raw,'receipt':str(receipt)}),encoding='utf-8')
time.sleep(60)
'''


@pytest.fixture
def mapped_process(tmp_path):
    process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-c', CHILD, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    marker = tmp_path / 'ready.json'
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists(), '映射子进程没有就绪：' + (process.stderr.read().decode('utf-8', 'replace') if process.poll() is not None else '超时')
        yield process, json.loads(marker.read_text(encoding='utf-8')), tmp_path
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        # 即使中间断言失败，也不能把测试映射留在真实登录会话。
        receipt = tmp_path / 'tmp/aniflive-parser-aliases'
        if marker.exists():
            original = json.loads(marker.read_text(encoding='utf-8'))
            path = Path(original['receipt'])
            if path.exists():
                value = json.loads(path.read_text(encoding='utf-8')); value['target'] = original['target']
                path.write_text(json.dumps(value), encoding='utf-8')
        cleanup_aliases(tmp_path)
        process.stderr.close()


def test_killed_process_alias_is_reclaimed_but_live_owner_is_preserved(mapped_process):
    process, value, root = mapped_process
    alias = Path(value['drive'] + '\\model.onnx')
    assert alias.read_bytes() == b'model'
    retained = cleanup_aliases(root)
    assert retained['removed'] == [] and retained['active'] == [value['drive']]
    process.kill(); process.wait(timeout=10)
    assert alias.exists(), '测试必须确实复现强杀后映射仍存在'
    assert cleanup_aliases(root)['removed'] == [value['drive']]
    assert not alias.exists() and not Path(value['receipt']).exists()


def test_mismatched_target_is_never_removed(mapped_process):
    process, value, root = mapped_process
    process.kill(); process.wait(timeout=10)
    path = Path(value['receipt'])
    receipt = json.loads(path.read_text(encoding='utf-8'))
    receipt['target'] = '\\??\\' + str(root / '另一个目录')
    path.write_text(json.dumps(receipt), encoding='utf-8')
    with pytest.raises(ValueError, match='拒绝撤销其他映射'):
        cleanup_aliases(root)
    assert Path(value['drive'] + '\\model.onnx').exists() and path.exists()


def test_kill_before_receipt_publication_does_not_block_next_task(tmp_path):
    hook = '''import voice_terminal.tts.aniflive_aliases as aliases
def before_publication(source, destination):
    (root/'ready.json').write_text(json.dumps({'drive':drive,'pending':str(source),'receipt':str(destination)}),encoding='utf-8')
    time.sleep(60)
aliases.os.rename = before_publication
'''
    code = CHILD.replace('with _alias_mutex():', hook + '\nwith _alias_mutex():')
    process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-c', code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        marker = tmp_path / 'ready.json'
        deadline = time.monotonic() + 15
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists(), '前置收据写入测试未就绪'
        value = json.loads(marker.read_text(encoding='utf-8'))
        assert Path(value['pending']).is_file() and not Path(value['receipt']).exists()
        process.kill(); process.wait(timeout=10)
        result = cleanup_aliases(tmp_path)
        assert result['removed'] == [] and result['active'] == []
        from voice_terminal.tts.aniflive_aliases import record_alias
        receipt = record_alias(tmp_path, value['drive'], '\\??\\' + str(tmp_path / '中文计算图'))
        try:
            assert json.loads(receipt.read_text(encoding='utf-8'))['pid'] == os.getpid()
            with pytest.raises(FileExistsError):
                record_alias(tmp_path, value['drive'], '\\??\\' + str(tmp_path / '中文计算图'))
            assert receipt.exists()
        finally:
            receipt.unlink()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10); process.stderr.close()

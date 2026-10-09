"""Tests（后端测试）：复用正式解释器，只加载独立的轻量测试工具。"""
from pathlib import Path
import sys
import os
import tempfile

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path[:0] = [str(ROOT), str(ROOT / '.local/tools/testing')]
import subprocess
from voice_terminal.local_files import remove_tree

if __name__ == '__main__':
    scratch = ROOT / '.local/tmp/backend-suite'
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    os.environ.update(TEMP=str(scratch), TMP=str(scratch))
    # TIKTOKEN_CACHE_DIR（分词器缓存目录）持久留在可回收缓存，避免每轮测试重复下载。
    os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(ROOT / '.local/cache/tiktoken'))
    temporary = ROOT / '.local/tmp/pytest'
    if '--child' in sys.argv:
        import pytest
        raise SystemExit(pytest.main(['--basetemp', str(temporary), '-o', f'cache_dir={ROOT / ".local/cache/pytest"}', *[a for a in sys.argv[1:] if a != '--child']]))
    # 子进程真实退出后再回收，确保数据库、模型映射等句柄均已释放。
    code = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--child', *sys.argv[1:]], cwd=ROOT).returncode
    if not code:
        remove_tree(temporary, ROOT / '.local')
        remove_tree(scratch, ROOT / '.local')
    raise SystemExit(code)

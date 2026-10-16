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
    # 每轮独占临时根目录；子进程沿用父进程分配的路径，避免并行会话互相清理数据库。
    temporary_root = ROOT / '.local/tmp'
    temporary_root.mkdir(parents=True, exist_ok=True)
    inherited_run = os.environ.get('VOICE_BACKEND_TEST_RUN_DIR') if '--child' in sys.argv else None
    run_directory = (Path(inherited_run).resolve() if inherited_run else
                     Path(tempfile.mkdtemp(prefix='backend-suite-', dir=temporary_root)).resolve())
    if not run_directory.is_relative_to(temporary_root.resolve()) or run_directory == temporary_root.resolve():
        raise ValueError('后端测试临时目录必须是本机临时根目录下的独立子目录。')
    os.environ['VOICE_BACKEND_TEST_RUN_DIR'] = str(run_directory)
    scratch = run_directory / 'scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    os.environ.update(TEMP=str(scratch), TMP=str(scratch))
    # TIKTOKEN_CACHE_DIR（分词器缓存目录）持久留在可回收缓存，避免每轮测试重复下载。
    os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(ROOT / '.local/cache/tiktoken'))
    temporary = run_directory / 'pytest'
    if '--child' in sys.argv:
        import pytest
        raise SystemExit(pytest.main(['--basetemp', str(temporary), '-o', f'cache_dir={run_directory / "cache"}', *[a for a in sys.argv[1:] if a != '--child']]))
    # 子进程真实退出后再回收，确保数据库、模型映射等句柄均已释放。
    code = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--child', *sys.argv[1:]], cwd=ROOT).returncode
    if not code:
        remove_tree(run_directory, ROOT / '.local')
    raise SystemExit(code)

"""Installation smoke（插件安装冒烟检查）：同一解释器加载隔离测试夹具，成功即回收。"""
from pathlib import Path
import subprocess
ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
subprocess.run([str(ROOT / '.local/runtimes/python/python.exe'), '-B', '-X', 'utf8', str(ROOT / 'scripts/test-backend.py'), '-q',
                'tests/test_dsh_setup_backup.py', 'tests/test_dsh_runtime.py', 'tests/test_desktop_packaging.py'], cwd=ROOT, check=True)

"""Testing tools（轻量测试工具）：版本锁定，只安装测试框架，不另建计算环境。"""
from pathlib import Path
import argparse
import subprocess
import sys
import os

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
parser = argparse.ArgumentParser(description='安装轻量测试工具，不安装大型业务依赖')
parser.add_argument('--python', default=sys.executable, help='python（带安装工具的构建解释器）')
args = parser.parse_args()
os.environ['PIP_CACHE_DIR'] = str(ROOT / '.local/cache/pip')  # PIP_CACHE_DIR（依赖安装缓存）。
subprocess.run([args.python, '-m', 'pip', 'install', '--upgrade', '--target', str(ROOT / '.local/tools/testing'),
                'pytest==8.3.4', 'pytest-asyncio==0.24.0', 'pluggy==1.5.0', 'iniconfig==2.0.0', 'packaging==24.2', 'colorama==0.4.6'], check=True)

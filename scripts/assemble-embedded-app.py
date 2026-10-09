"""Legacy assembly alias（旧组装命令别名）：统一组装完整正式程序包，不再排除宿主或复制自身。"""
from pathlib import Path
import runpy
if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).with_name('assemble-program-package.py')), run_name='__main__')

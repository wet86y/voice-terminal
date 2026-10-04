"""Backend entry（后端显式入口）：兼容可嵌入 Python（程序运行环境）。"""
from pathlib import Path
import os
import runpy
import sys

program = Path(__file__).resolve().parents[1]
if os.environ.get("VOICE_DATA_DIR"):
    sys.pycache_prefix = str(Path(os.environ["VOICE_DATA_DIR"]) / "cache/python")
sys.path.insert(0, str(program))
os.environ.setdefault("VOICE_PROGRAM_ROOT", str(program))
if len(sys.argv) < 2 or not sys.argv[1].startswith(("voice_terminal.", "apps.server.")):
    raise SystemExit("需要指定受管后端模块")
module = sys.argv.pop(1)
sys.argv[0] = module
runpy.run_module(module, run_name="__main__")

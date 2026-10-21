"""Backend entry（后端显式入口）：兼容可嵌入 Python（程序运行环境）。"""
from pathlib import Path
import os
import runpy
import sys

program = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
if os.environ.get("VOICE_INSTALL_ROOT"):
    sys.pycache_prefix = str(Path(os.environ["VOICE_INSTALL_ROOT"]) / "cache/python")
sys.path.insert(0, str(program))
os.environ.setdefault("VOICE_PROGRAM_ROOT", str(program))
if len(sys.argv) < 2 or not sys.argv[1].startswith(("voice_terminal.", "apps.server.")):
    raise SystemExit("需要指定受管后端模块")
module = sys.argv.pop(1)
sys.argv[0] = module
from voice_terminal.tensor_runtime import activate_tensor_runtime
activate_tensor_runtime("cuda" if module == "voice_terminal.hardware_probe" else None)
from voice_terminal.startup_logging import startup_event
startup_event("后端入口已执行，开始导入模块", module=module)
runpy.run_module(module, run_name="__main__")

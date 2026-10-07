"""统一依赖诊断的命令行入口。"""
from pathlib import Path
import sys
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())))
from voice_terminal.dependencies import main

if __name__ == "__main__":
    main()

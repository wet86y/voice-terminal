"""准备固定 AnifLive（本地语音引擎）源码；不会更换播放目标。"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.tts.aniflive_native import prepare_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="archive（固定官方源码归档）")
    args = parser.parse_args()
    print(json.dumps(prepare_source(args.archive, ROOT / ".local"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

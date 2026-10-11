"""Prepare desktop CRT（独立桌面 C++ 运行库）：从已授权来源准备分发输入。"""
from pathlib import Path
import argparse
import json
import shutil
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import digest, manifest, verify
from voice_terminal.paths import atomic_json

def prepare(source: Path, source_manifest: Path | None = None) -> Path:
    source = source.resolve()
    names = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
    if source_manifest:
        record = json.loads(source_manifest.read_text("utf-8-sig"))
        rows = {row["path"]: row for row in record["files"]}
        for name in names:
            path = source / name
            if name not in rows or path.stat().st_size != rows[name]["size"] or digest(path) != rows[name]["sha256"]:
                raise ValueError("已发布桌面 C++（编译语言）运行库摘要不符")
    if not all((source / name).is_file() for name in names):
        raise ValueError("C++（编译语言）运行库来源不完整")
    output = ROOT / ".local/build/tools/desktop-crt"
    output.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(source / name, output / name)
    for name in ("VC-Runtime-Notice.txt", "License.txt"):
        if (source / name).is_file():
            shutil.copyfile(source / name, output / name)
    atomic_json(output / "manifest.json", manifest(output, kind="desktop-crt", build="verified-redist"))
    verify(output, json.loads((output / "manifest.json").read_text("utf-8")))
    return output

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".local/app", help="source（已授权的 C++ 运行库来源）")
    parser.add_argument("--manifest", type=Path, help="manifest（已发布来源的文件摘要记录）")
    args = parser.parse_args()
    record = args.manifest or (args.source / "release.json" if (args.source / "release.json").is_file() else None)
    print(prepare(args.source, record))

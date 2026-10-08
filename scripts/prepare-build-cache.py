"""Controlled cache（受控构建缓存）：锁或工具链变化时替换固定缓存。"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.local_files import remove_tree, reparse
from voice_terminal.paths import atomic_json


def prepare(kind, toolchain):
    local = ROOT / '.local'
    inputs = [ROOT / 'apps/desktop/Cargo.lock'] if kind == 'desktop' else [ROOT / 'config/dsh/runtime.lock.json', local / 'build/dsh/source/pnpm-lock.yaml']
    stamp = hashlib.sha256(toolchain.encode() + b''.join(p.read_bytes() for p in inputs)).hexdigest()
    marker = local / f'build/{kind}-cache.json'
    before = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else None
    if before and before['fingerprint'] != stamp:
        if kind != 'desktop':
            source = local / 'build/dsh/source'
            # 保留上游检出及用户源码修改，只回收未跟踪的依赖和编译输出。
            tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z']).decode('utf-8').split('\0')
            pending = [source]
            while pending:
                parent = pending.pop()
                for path in parent.iterdir():
                    if reparse(path) or not path.is_dir() or path.name == '.git':
                        continue
                    relative = path.relative_to(source).as_posix() + '/'
                    if path.name in {'node_modules', 'lib', '.cache', '.temp'} and not any(p.startswith(relative) for p in tracked):
                        remove_tree(path, source)
                    else:
                        pending.append(path)
            remove_tree(local / 'build/dsh/bundles', local)
        print('已登记更新的构建依赖：' + kind, flush=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(marker, {'fingerprint': stamp, 'fields': {'fingerprint': '当前依赖锁和工具链摘要'}})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='单个固定缓存，首次登记，版本变化才清理')
    parser.add_argument('kind', choices=['desktop', 'dsh'], help='kind（原生或代理构建缓存）')
    parser.add_argument('--toolchain', required=True, help='toolchain（当前编译器或包管理器版本）')
    args = parser.parse_args()
    prepare(args.kind, args.toolchain)

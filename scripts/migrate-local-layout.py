"""Migration（一次性迁移）：读取旧布局、保护持久数据，同盘搬入固定目录。"""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import digest, require_stopped
from voice_terminal.local_files import backup_user_data, persistent_files, reparse
from voice_terminal.local_layout import default_portable
from voice_terminal.paths import atomic_json


def migrate(source: Path, target: Path) -> None:
    source, target = source.resolve(), target.resolve()
    if not source.is_relative_to(ROOT) or target != ROOT / '.local':
        raise ValueError('迁移范围只能是当前工程内部的旧安装和 .local（本机资源）')
    target.mkdir(exist_ok=True)
    if (target / 'data/state/layout-migration.json').is_file():
        print('一次性迁移已完成；现用配置及数据不会再次改写。')
        return
    require_stopped(ROOT, target)
    record_path = target / 'tmp/layout-migration.json'
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding='utf-8'))
    else:
        require_stopped(source, target)
        portable = json.loads((source / 'portable.json').read_text(encoding='utf-8-sig'))
        data = source / portable['data_dir']
        active = data / 'state/active.json'
        selection = json.loads(active.read_text(encoding='utf-8-sig')) if active.exists() else portable
        before = {p.relative_to(data).as_posix(): digest(p) for p in persistent_files(data)}
        backup_user_data(target, data=data)
        # 旧数据仅按摘要收集唯一内容；相同名称不同内容放入导入区，不能覆盖现用配置。
        seen = set(before.values())
        origins = []
        runtime = ROOT / '.runtime'
        for directory, children, _ in os.walk(runtime, followlinks=False):
            parent = Path(directory)
            children[:] = [name for name in children if name not in {'node_modules', 'source', '.git', 'cache', 'logs', 'models', 'runtimes', 'target', 'python', 'full', 'cosyvoice'} and not reparse(parent / name)]
            if parent != data and (parent / 'config').is_dir() and any((parent / n).is_dir() for n in ('persona', 'history', 'speaker')):
                origins.append((parent, 'data'))
                children[:] = []
        for home in (runtime / 'dsh').glob('home*'):
            if home.is_dir() and not reparse(home):
                origins.append((home, 'dsh'))
        recovered = []
        for origin, kind in origins:
            candidates = persistent_files(origin) if kind == 'data' else persistent_files_for_home(origin)
            for path in candidates:
                checksum = digest(path)
                if checksum in seen:
                    continue
                relative = path.relative_to(origin)
                destination = data / 'imports/legacy-recovery' / (checksum + path.suffix)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                if digest(destination) != checksum:
                    raise ValueError('旧来源唯一数据复制校验失败')
                recovered.append({'source': path.relative_to(ROOT).as_posix(), 'path': destination.relative_to(data).as_posix(), 'sha256': checksum})
                seen.add(checksum)
        mapping = [(str(source / selection['app']), 'app'),
                   (str((source / selection['python']).parent), 'runtimes/python'),
                   (str(source / selection['models']), 'models'),
                   (str(source / selection['dsh']), 'runtimes/dsh'),
                   (str(source / selection['webview2']), 'runtimes/webview2'),
                   (str(data), 'data'),
                   (str(runtime / 'dsh/source'), 'build/dsh/source'),
                   (str(runtime / 'dsh/bundles'), 'build/dsh/bundles'),
                   (str(runtime / 'desktop-tools'), 'tools/rust'),
                   (str(ROOT / 'apps/desktop/target'), 'build/desktop'),
                   (str(ROOT / 'apps/frontend/dist'), 'build/frontend')]
        record = {'schema': 1, 'before': before, 'recovered': recovered, 'moves': mapping, 'completed': [],
                  'fields': {'before': '现用持久数据迁移前摘要', 'recovered': '唯一旧数据来源与摘要', 'moves': '具体搬迁路径', 'completed': '已完成同盘重命名'}}
        atomic_json(record_path, record)
    for old, relative in record['moves']:
        destination = target / relative
        if relative in record['completed']:
            continue
        old = Path(old)
        if old.exists():
            if destination.exists() or reparse(old):
                raise ValueError(f'迁移目标冲突或来源是重解析点：{relative}')
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                old.rename(destination)
            except PermissionError:
                if relative != 'build/desktop':
                    raise
                # 目录观察器可持有缓存根的句柄；逐项重命名，不复制构建文件。
                destination.mkdir()
                for child in old.iterdir():
                    child.rename(destination / child.name)
        elif not destination.exists():
            continue
        record['completed'].append(relative)
        atomic_json(record_path, record)
    for name in ('VoiceTerminal.exe', 'Verify-Package.ps1', '使用与环境说明.md'):
        if (source / name).exists():
            shutil.copy2(source / name, target / name)
    demo = ROOT / '.runtime/delivery/VoiceTerminalUIDemo.exe'
    if demo.exists():
        shutil.copy2(demo, target / demo.name)
    for relative, checksum in record['before'].items():
        if digest(target / 'data' / relative) != checksum:
            raise ValueError('现用持久文件迁移摘要不一致')
    # 联接只重建于单一构建缓存内部；实际业务模块联接由运行管理器按生产清单重建。
    node = json.loads((target / 'runtimes/dsh/runtime.json').read_text(encoding='utf-8'))['node']
    subprocess.run([str(target / 'runtimes/dsh' / node), str(ROOT / 'scripts/rewire-build-links.mjs'), str(ROOT / '.runtime/dsh/source'), str(target / 'build/dsh/source')], check=True)
    backup_user_data(target)
    atomic_json(target / 'portable.json', default_portable())
    atomic_json(target / 'data/state/layout-migration.json', record)
    print(f'现用持久数据摘要验证通过：{len(record["before"])} 个文件；保全唯一旧文件：{len(record["recovered"])} 个。')


def persistent_files_for_home(home: Path):
    excluded = {'profiles', 'node_modules', 'cache', 'logs', '.pnpm-store', '__pycache__'}
    for directory, children, names in os.walk(home, followlinks=False):
        parent = Path(directory)
        children[:] = [n for n in children if n not in excluded and not reparse(parent / n)]
        for name in names:
            path = parent / name
            if not reparse(path) and path.suffix not in {'.log', '.pyc'}:
                yield path
    patch = home / 'profiles/web/cordis.patch.yml'
    if patch.is_file():
        yield patch


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='一次性旧布局迁移；可从事务记录继续')
    parser.add_argument('--source', type=Path, default=ROOT / '.runtime/delivery/VoiceTerminal')
    args = parser.parse_args()
    migrate(args.source, ROOT / '.local')

"""Program package（统一正式程序包）：本地发布与安装器使用同一份固定本体。"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('desktop_assembler', ROOT / 'scripts/assemble-desktop.py')
assembler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assembler)
from voice_terminal import __version__
from voice_terminal.distribution import digest, files, manifest, verify
from voice_terminal.paths import atomic_json

def bundle_speaker_seed(program: Path) -> dict:
    """CAM++升级种子：官方摘要校验后随本体交付，安装时离线装入外置模型目录。"""
    from voice_terminal.resource_distribution import ResourceCatalog
    model = ResourceCatalog(ROOT).specs['campplus-zh-cn-common']
    source = ROOT / '.local/models' / model.destination
    target = program / 'resources/model-seeds/campplus-zh-cn-common'
    target.mkdir(parents=True)
    for row in model.required_files:
        weight = source / row.path
        if not weight.is_file() or weight.stat().st_size != row.size_bytes or digest(weight) != row.sha256:
            raise ValueError('CAM++官方升级权重缺失或摘要不匹配，请先修复模型资源')
        destination = target / row.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(weight, destination)
        if digest(destination) != row.sha256:
            raise ValueError('CAM++升级种子复制后摘要不匹配')
    return {'revision': 1, 'from_model': 'wespeaker-cnceleb-resnet34', 'to_model': model.resource_id,
            'delete_legacy_profiles': True,
            'fields': {'revision': '声纹升级规则版本', 'from_model': '要卸载的旧声纹模型',
                       'to_model': '升级目标模型', 'delete_legacy_profiles': '升级成功后删除旧声纹档案'}}

def assemble(desktop: Path, build: str = '') -> Path:
    cache = ROOT / '.local/build/program-package'
    cache.mkdir(parents=True, exist_ok=True)
    stage = cache / 'payload'
    if stage.exists():
        if stage.is_symlink() or stage.is_junction() or stage.resolve().parent != cache.resolve():
            raise ValueError('正式程序包暂存目录越界')
        shutil.rmtree(stage)
    stage.mkdir()
    assembler.collect_program(stage / 'app', desktop)
    speaker_migration = bundle_speaker_seed(stage / 'app')
    guide = stage / 'app/docs/模型运行环境配置指南.md'
    guide.parent.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'docs/guides/模型运行环境配置指南.md', guide)
    lock = json.loads((stage / 'app/config/runtime/voice-runtime.lock.json').read_text('utf-8'))
    for row in lock['wheels']:
        if row.get('bundled'):
            target = stage / 'app' / row['bundled']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / '.local/build/runtime-lock' / row['file'], target)
            if digest(target) != row['sha256']:
                raise ValueError('内置必要安装依赖摘要不符')
    roots = assembler.root_files(stage, desktop)
    inventory = [{'path': p.relative_to(stage).as_posix(), 'size': p.stat().st_size, 'sha256': digest(p)} for p in files(stage)]
    build = build or 'product-' + hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()[:16]
    release = manifest(stage / 'app', kind='app', build=build)
    release.update(display_version=__version__, role='product', required_components=[], optional_components=['python', 'models', 'dsh', 'webview2'], components=[], root_files=roots,
                   speaker_migration=speaker_migration,
                   compatibility={'desktop_protocol': 1, 'data_format': 1},
                   fields={'build': '构建编号，产品版本保持原值', 'role': 'product（统一正式程序）', 'display_version': '产品显示版本', 'files': '本体固定文件', 'path': '相对路径', 'size': '字节数', 'sha256': 'SHA-256（文件摘要）', 'components': '外置组件登记；整体语音服务启动时逐项校验', 'root_files': '正式入口及固定支持文件', 'required_components': '管理页启动所需组件，独立于整体语音服务就绪门槛', 'optional_components': '由管理页安装的外置组件；整体语音服务运行必需', 'compatibility': '接口及数据格式兼容版本'})
    atomic_json(stage / 'app/release.json', release)
    verify(stage / 'app', release)
    record = {'schema': 1, 'role': 'product', 'version': __version__, 'build': build,
              'files': [{'path': p.relative_to(stage).as_posix(), 'size': p.stat().st_size, 'sha256': digest(p)} for p in files(stage)],
              'fields': {'schema': '统一程序包格式', 'role': 'product（正式程序）', 'version': '产品版本', 'build': '构建编号', 'files': '固定文件集合，组件登记单独核验', 'path': '相对路径', 'size': '字节数', 'sha256': 'SHA-256（文件摘要）'}}
    atomic_json(stage / 'program-package.json', record)
    archive = cache / 'program.zip'
    candidate = archive.with_suffix('.pending.zip')
    with zipfile.ZipFile(candidate, 'w', zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as output:
        for path in files(stage):
            output.write(path, path.relative_to(stage).as_posix())
    candidate.replace(archive)
    atomic_json(cache / 'package-info.json', {'schema': 1, 'build': build, 'version': __version__, 'size': archive.stat().st_size, 'sha256': digest(archive), 'files': len(record['files']),
                'fields': {'schema': '程序包记录格式', 'build': '构建编号', 'version': '产品版本', 'size': '压缩包字节数', 'sha256': '压缩包摘要', 'files': '固定本体文件数'}})
    print(f'统一正式程序包：{archive}；构建 {build}；{archive.stat().st_size / 1024**2:.1f} MiB（兆二进制字节）。')
    return archive

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop', type=Path, default=ROOT / '.local/build/desktop/product/release', help='desktop（统一正式宿主构建目录）')
    parser.add_argument('--build', default='', help='build（构建编号，缺省按固定文件摘要生成）')
    args = parser.parse_args()
    assemble(args.desktop, args.build)

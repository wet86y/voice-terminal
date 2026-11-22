"""本地语音依赖用途分层：同盘搬迁、文件收据核验和中断回滚。"""
from __future__ import annotations
import json
import os
from pathlib import Path
from .aniflive_native import contained, digest
from .aniflive_install import atomic_json

CONVERSION_PACKAGES = frozenset({
    'torchprofile', 'polygraphy', 'onnxsim', 'onnx', 'ml-dtypes', 'onnx-graphsurgeon',
    'nvidia-modelopt', 'pulp', 'orloge', 'cpsat-logutils', 'ninja',
    'onnxconverter-common', 'onnxscript', 'onnx-ir',
})
MODULES = frozenset(name.replace('-', '_') for name in CONVERSION_PACKAGES) | {'modelopt'}

def conversion_file(relative):
    """conversion_file（转换专属文件）：固定包模块、元数据和构建器动态库。"""
    parts = Path(relative).parts
    if len(parts) == 2 and parts[0] == 'tensorrt_libs' and parts[1].startswith('nvinfer_builder_resource_') and parts[1].endswith('_11.dll'):
        return 'conversion-libs/' + parts[1]
    top = parts[0]
    if top in MODULES or any(top.startswith(name + '-') and top.endswith('.dist-info') for name in MODULES):
        return 'export-packages/' + relative
    return None

def split(local):
    """split（显式组件任务调用）：扫描和普通模型启动不得调用。"""
    root = contained(Path(local), Path(local) / 'runtimes/aniflive-tts')
    from .aniflive_transactions import recover_dependencies
    recover_dependencies(root)
    journal = contained(root, root / 'layout-transaction.json')
    if journal.exists():
        recover(root)
    runtime_path = contained(root, root / 'packages/aniflive-packages.json')
    export_path = contained(root, root / 'export-packages/aniflive-packages.json')
    if not runtime_path.is_file() or not export_path.is_file():
        return
    runtime = json.loads(runtime_path.read_text('utf-8'))
    export = json.loads(export_path.read_text('utf-8'))
    moves = []
    for row in runtime.get('files', []):
        target = conversion_file(row['path'])
        if not target:
            continue
        source = contained(root, root / 'packages' / row['path'])
        destination = contained(root, root / target)
        if source.stat().st_size != row['size'] or digest(source) != row['sha256']:
            raise ValueError('依赖分层遇到内容变化，保留现用依赖：' + row['path'])
        if destination.exists():
            raise ValueError('依赖分层目标已存在，保留原件：' + target)
        moves.append({'source': 'packages/' + row['path'], 'target': target, 'record': row})
    if not moves:
        return
    value = {'schema': 1, 'state': 'prepared', 'moves': moves, 'runtime': runtime, 'export': export, 'fields': {'schema':'布局事务格式','state':'准备或已提交','moves':'同盘文件搬迁清单','source':'原位置','target':'转换层位置','record':'原收据文件身份','runtime':'原推理收据','export':'原转换收据'}}
    atomic_json(journal, value)
    try:
        for move in moves:
            destination = contained(root, root / move['target'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(contained(root, root / move['source']), destination)
        removed = {move['record']['path'] for move in moves}
        runtime_new = {**runtime, 'schema': 2, 'conversion_installed': True,
                       'conversion_libraries': [move['record'] for move in moves if move['target'].startswith('conversion-libs/')], 'files': [row for row in runtime['files'] if row['path'] not in removed],
                       'packages': {key: val for key, val in runtime['packages'].items() if key not in CONVERSION_PACKAGES},
                       'fields': {**runtime.get('fields',{}), 'conversion_installed':'转换框架已配置', 'conversion_libraries':'转换动态库的原包路径、大小与摘要'}}
        export_new = {**export, 'schema': 2, 'packages': {**{key: val for key,val in runtime['packages'].items() if key in CONVERSION_PACKAGES}, **export['packages']},
                      'files': export['files'] + [move['record'] for move in moves if move['target'].startswith('export-packages/')],
                      'libraries': [move['record'] for move in moves if move['target'].startswith('conversion-libs/')],
                      'fields': {**export.get('fields',{}), 'libraries':'转换专用动态库的原包路径、大小与摘要'}}
        atomic_json(runtime_path, runtime_new)
        atomic_json(export_path, export_new)
        atomic_json(journal, {**value, 'state': 'committed'})
    except BaseException:
        recover(root)
        raise
    recover(root)

def recover(root):
    """recover（恢复分层事务）：未提交回滚，已提交只收尾。"""
    journal = contained(root, root / 'layout-transaction.json')
    value = json.loads(journal.read_text('utf-8'))
    if value.get('schema') != 1 or value.get('state') not in {'prepared', 'committed'}:
        raise ValueError('依赖分层事务无效，保留现场')
    for move in value['moves']:
        relative = move['record']['path']
        if move['source'] != 'packages/' + relative or move['target'] != conversion_file(relative):
            raise ValueError('依赖分层事务路径不在固定范围')
        contained(root, root / move['source']); contained(root, root / move['target'])
    if value['state'] == 'prepared':
        for move in reversed(value['moves']):
            source = contained(root, root / move['source'])
            target = contained(root, root / move['target'])
            if target.exists():
                if source.exists():
                    raise ValueError('依赖分层恢复存在双份原件，保留现场')
                source.parent.mkdir(parents=True, exist_ok=True)
                os.rename(target, source)
        atomic_json(root / 'packages/aniflive-packages.json', value['runtime'])
        atomic_json(root / 'export-packages/aniflive-packages.json', value['export'])
    journal.unlink()


def prepare_libraries(local, fetch):
    """prepare_libraries（恢复转换动态库）：只从固定原包提取已登记的构建器文件。"""
    import hashlib
    import tempfile
    import zipfile
    from .aniflive_transactions import recover_dependencies
    from ..local_files import remove_tree
    root = contained(Path(local), Path(local) / 'runtimes/aniflive-tts')
    recover_dependencies(root)
    receipt = json.loads((root/'packages/aniflive-packages.json').read_text('utf-8'))
    records = receipt.get('conversion_libraries', [])
    if not records:
        return
    target = contained(root, root/'conversion-libs')
    for row in records:
        if conversion_file(row['path']) != 'conversion-libs/' + Path(row['path']).name:
            raise ValueError('转换动态库登记包含未知路径')
    if all((target/Path(row['path']).name).is_file() and (target/Path(row['path']).name).stat().st_size == row['size']
           and digest(target/Path(row['path']).name) == row['sha256'] for row in records):
        return
    if target.exists():
        expected = {Path(row['path']).name for row in records}
        for path in target.iterdir():
            contained(root,path)
            if path.name not in expected:
                raise ValueError('转换动态库目录存在未知文件，未覆盖')
    archive = fetch()
    stage = Path(tempfile.mkdtemp(prefix='.conversion-libs-stage-', dir=root))
    backup = root/('.conversion-libs-old-' + stage.name.rsplit('-',1)[-1])
    journal = root/'dependency-transaction.json'
    try:
        with zipfile.ZipFile(archive) as package:
            for row in records:
                matches = [name for name in package.namelist() if name == row['path'] or name.endswith('/'+row['path'])]
                if len(matches) != 1:
                    raise ValueError('固定依赖包缺少唯一的转换动态库')
                target_file = stage/Path(row['path']).name
                sha = hashlib.sha256()
                with package.open(matches[0]) as source, target_file.open('xb') as output:
                    for block in iter(lambda:source.read(1024*1024), b''):
                        sha.update(block); output.write(block)
                if target_file.stat().st_size != row['size'] or sha.hexdigest() != row['sha256']:
                    raise ValueError('转换动态库与已校验原包身份不符')
        value = {'schema':1,'state':'prepared','items':[{'target':'conversion-libs','stage':stage.name,'backup':backup.name,'original':target.exists()}]}
        atomic_json(journal,value)
        if target.exists(): os.rename(target,backup)
        os.rename(stage,target)
        atomic_json(journal,{**value,'state':'committed'})
        recover_dependencies(root)
    except BaseException:
        if journal.exists(): recover_dependencies(root)
        raise
    finally:
        if stage.exists(): remove_tree(stage,root)

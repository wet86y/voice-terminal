"""受管旧模型清理：先核验内容，使用同盘暂存及元数据事务保留恢复能力。"""
from __future__ import annotations
import base64
import tempfile
import hashlib
import json
import os
from pathlib import Path
from .tts.aniflive_native import contained
from .tts.aniflive_install import atomic_json
from .local_files import remove_tree

JOURNAL = 'tmp/storage-model-cleanup.json'
BACKUP = 'tmp/storage-model-cleanup-old'
RETIRED = {'models/analysis/sensevoice': 'sensevoice-small', 'models/audiocpp/runtime':'tts-audiocpp-runtime'}

def scope(relative):
    """scope（固定模型维护范围）：绝不接受任意模型目录。"""
    parts = relative.split('/')
    if relative in RETIRED:
        return None
    if (len(parts) == 6 and parts[:3] == ['models','aniflive-tts','packages']
            and len(parts[4]) == 24 and all(ch in '0123456789abcdef' for ch in parts[4])
            and parts[5].startswith('.export-stages-') and len(parts[5]) > 15):
        from .tts.settings import valid_local_model
        if valid_local_model(parts[3]):
            return '/'.join(parts[:5])
    raise ValueError('清理目标不属于已登记的旧模型或导出暂存')

def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2)).encode('utf-8')

def atomic_bytes(path, content):
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def read(root, relative):
    return json.loads(contained(root, root / relative).read_text('utf-8-sig'))

def allowed_metadata(relative):
    package = scope(relative)
    paths = {'models/manifest.json','app/release.json'}
    if package:
        paths.update(package + '/' + name for name in ('application-receipt.json','checksums.json','resource-install.json'))
    return paths

def recover(root):
    journal = contained(root, root / JOURNAL)
    value = read(root, JOURNAL)
    scope(value['relative'])
    if value.get('schema') != 1 or value.get('state') not in {'prepared','committed'}:
        raise ValueError('模型清理事务格式无效，保留现场')
    if not isinstance(value.get('originals'), dict) or not set(value['originals']) <= allowed_metadata(value['relative']):
        raise ValueError('模型清理事务包含未授权元数据')
    backup = contained(root, root / BACKUP)
    target = contained(root, root / value['relative'])
    if value['state'] == 'prepared':
        if backup.exists():
            if target.exists():
                raise ValueError('模型清理恢复存在双份内容，保留现场')
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(backup, target)
        elif not target.exists():
            raise ValueError('原模型和恢复内容均缺失，保留事务')
        for relative, original in value['originals'].items():
            atomic_bytes(contained(root, root / relative), base64.b64decode(original, validate=True))
    else:
        def remaining_bytes():
            if not backup.exists():
                return 0
            total = 0
            for path in backup.rglob('*'):
                contained(root, path)
                if path.is_file():
                    total += path.stat().st_size
            return total
        before = remaining_bytes()
        try:
            remove_tree(backup, root)
        except OSError as error:
            return {'removed_bytes': max(0, before - remaining_bytes()),
                    'error': '部分暂存未清理，请继续收尾：' + str(error)}
    journal.unlink()
    return {'removed_bytes': before if value['state'] == 'committed' else 0}

def execute(root, relative):
    """execute（原生维护任务调用）：调用者已取得组件互斥并暂停本机业务。"""
    root = Path(root).absolute()
    if (root / JOURNAL).exists():
        if relative != JOURNAL:
            raise ValueError('请先选择并完成上次模型清理事务')
        return recover(root)
    package = scope(relative)
    target = contained(root, root / relative)
    backup = contained(root, root / BACKUP)
    if backup.exists():
        raise ValueError('模型清理恢复目录已存在，未覆盖')
    manifest = read(root, 'models/manifest.json')
    release = read(root, 'app/release.json')
    prefix = relative.removeprefix('models/') + '/'
    rows = [row for row in manifest['files'] if row['path'].startswith(prefix)]
    if not rows:
        raise ValueError('模型总清单没有待清理内容的归属记录')
    expected = {row['path']: row for row in rows}
    actual = set()
    for item in target.rglob('*'):
        contained(root, item)
        if item.is_file():
            name = item.relative_to(root / 'models').as_posix()
            row = expected.get(name)
            with item.open('rb') as stream:
                matches = row and item.stat().st_size == row['size'] and hashlib.file_digest(stream, 'sha256').hexdigest() == row['sha256']
            if not matches:
                raise ValueError('旧模型内容身份不符，保留文件：' + name)
            actual.add(name)
    if actual != set(expected):
        raise ValueError('旧模型文件集合变化，未清理')
    if relative in RETIRED:
        catalog = read(root, 'app/config/resources/catalog.json')
        if any(row['resource_id'] == RETIRED[relative] for row in catalog['resources']):
            raise ValueError('当前版本仍登记此组件，不能作为垃圾清理')
        settings_path = root/'data/config/product-settings.json'
        if settings_path.exists():
            settings = read(root, 'data/config/product-settings.json')
            identities = {relative, RETIRED[relative]}
            identities.update({'audiocpp', 'audio.cpp', 'tts_audiocpp'} if relative == 'models/audiocpp/runtime' else {'sensevoice'})
            def referenced(value):
                if isinstance(value, str):
                    normalized = value.replace('\\', '/').lower()
                    return normalized in identities or normalized.endswith('/' + relative)
                if isinstance(value, dict):
                    return any(referenced(item) for item in value.values())
                if isinstance(value, list):
                    return any(referenced(item) for item in value)
                return False
            if referenced(settings):
                raise ValueError('当前或遗留设置仍引用此旧组件，请先完成设置迁移')
        if relative == 'models/analysis/sensevoice':
            from .analysis.snapshot import verify_snapshot
            for directory, resource in [('funasr/paraformer-zh-offline','paraformer-offline'),('analysis/emotion2vec-plus-base','emotion2vec-plus-base'),('analysis/yamnet','yamnet')]:
                verify_snapshot(root / 'models' / directory, resource)
    originals = {'models/manifest.json':manifest, 'app/release.json':release}
    changes = {}
    if package:
        tail = relative[len(package)+1:] + '/'
        for filename in ('application-receipt.json','checksums.json','resource-install.json'):
            name = package + '/' + filename
            if not (root/name).is_file():
                if filename != 'resource-install.json': raise ValueError('模型收据缺失，未清理')
                continue
            value = read(root, name)
            originals[name] = value
            if filename == 'checksums.json':
                value = {key: sha for key, sha in value.items() if not key.startswith(tail)}
                for changed, content in changes.items():
                    local = changed[len(package)+1:]
                    if local in value: value[local] = hashlib.sha256(content).hexdigest()
            else:
                value = {**value, 'files':[row for row in value['files'] if not row['path'].startswith(tail)]}
                for row in value['files']:
                    changed = package + '/' + row['path']
                    if changed in changes:
                        row = row.copy()
                        row.update(size=len(changes[changed]), sha256=hashlib.sha256(changes[changed]).hexdigest())
                        value['files'] = [row if item['path'] == row['path'] else item for item in value['files']]
                if filename == 'resource-install.json':
                    from .model_installation import receipt_digest
                    value['content_sha256'] = receipt_digest(value['resource_id'], value['files'])
            changes[name] = encoded(value)
    new_manifest = {**manifest, 'files':[row.copy() for row in manifest['files'] if not row['path'].startswith(prefix)]}
    for row in new_manifest['files']:
        name = 'models/' + row['path']
        if name in changes: row.update(size=len(changes[name]), sha256=hashlib.sha256(changes[name]).hexdigest())
    changes['models/manifest.json'] = encoded(new_manifest)
    new_release = json.loads(json.dumps(release))
    for component in new_release.get('components',[]):
        if component.get('kind') == 'models':
            component['manifest_sha256'] = hashlib.sha256(changes['models/manifest.json']).hexdigest()
    changes['app/release.json'] = encoded(new_release)
    originals = {name: base64.b64encode((root/name).read_bytes()).decode('ascii') for name in originals}
    value = {'schema':1,'state':'prepared','relative':relative,'bytes':sum(row['size'] for row in rows),'originals':originals,'fields':{'schema':'模型维护事务格式','state':'准备或已提交','relative':'核验通过的受管相对目录','bytes':'待回收逻辑字节数','originals':'原始元数据的可逐字节恢复编码'}}
    atomic_json(root / JOURNAL, value)
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.rename(target, backup)
        for name, content in changes.items():
            # atomic_json（原子结构化写入）避免暴露半个收据。
            atomic_bytes(contained(root, root / name), content)
        atomic_json(root / JOURNAL, {**value, 'state':'committed'})
    except BaseException:
        recover(root)
        raise
    return recover(root)

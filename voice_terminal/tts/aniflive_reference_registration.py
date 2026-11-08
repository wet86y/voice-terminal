"""AnifLive（本地语音）默认音色与导入参考的统一登记。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .aniflive_native import contained, digest


def register_model_default(library, package: Path) -> dict:
    """从已验证模型包登记其可编辑默认音色。"""
    package = Path(package).absolute()
    profile_path = contained(package, package / 'voices/default/profile.json')
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    relative = profile.get('reference_audio')
    if not isinstance(relative, str):
        raise ValueError('模型默认音色缺少参考录音。')
    reference = contained(profile_path.parent, profile_path.parent / relative)
    transcript = profile.get('reference_text')
    language = profile.get('reference_language')
    if not reference.is_file() or not isinstance(transcript, str) or not transcript.strip() or language not in {'zh', 'yue', 'en', 'ja', 'ko'}:
        raise ValueError('模型默认参考资料不完整。')
    return library.register_default(reference.read_bytes(), transcript.strip(), language=language)


def register_reference_bundle(library, model_id: str, package: Path, bundle: Path) -> dict:
    """将已校验的模型默认参考与持久导入资料登记到版本化音色库。"""
    package, bundle = Path(package).absolute(), Path(bundle).absolute()
    manifest_path = contained(bundle, bundle / 'reference-candidates.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    task_id = manifest.get('task_id')
    if manifest.get('model_id') != model_id or not isinstance(task_id, str) or len(task_id) != 32:
        raise ValueError('参考资料与目标模型不匹配。')
    files = {}
    for item in manifest.get('persisted_files', []):
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            raise ValueError('持久参考文件清单无效。')
        path = contained(bundle, bundle / item['path'])
        if not path.is_file() or path.stat().st_size != item.get('size') or digest(path) != item.get('sha256'):
            raise ValueError('持久参考文件内容不匹配。')
        files[item['source_path']] = path
    with library._lock:
        done = library._index['migrations'].get('reference_imports', {}).get(task_id)
        if done:
            return dict(done)
        default = register_model_default(library, package)
        # register_default（登记默认参考）可能返回旧用户编辑版本，不能覆盖其修改。
        voice = library.get_voice(default['id'])
        entries = list(voice.get('references', []))
        existing = {item['id'] for item in entries}
        existing_by_id = {item['id']: item for item in entries}
        counts = {'ready': 0, 'pending': 0}
        imported_source = []
        for row in manifest.get('references', []):
            if not isinstance(row, dict) or not isinstance(row.get('path'), str):
                raise ValueError('参考候选记录格式无效。')
            if row['path'] == manifest.get('default_audio'):
                continue
            identity = hashlib.sha256(('\0'.join(str(value or '') for value in (
                manifest.get('source_type'), manifest.get('source'), manifest.get('revision'),
                row['path'], row.get('source_config')))).encode()).hexdigest()[:32]
            if identity in existing:
                # 重试统计反映登记后的真实状态，不把去重误报为零条参考。
                counts['ready' if existing_by_id[identity].get('status') == 'ready' else 'pending'] += 1
                continue
            path = files.get(row['path'])
            ready = row.get('status') == 'ready' and path is not None
            source_asset, asset = None, None
            if path is not None:
                raw = path.read_bytes()
                if ready:
                    asset = library.put_import(raw, path.name)['asset_id']
                else:
                    source_asset = library.put_source(raw, path.name)['asset_id']
            error = '' if ready else (row.get('error') or '来源参考尚未完成校验或需要重新导入。')
            entry = {'id': identity, 'name': row.get('name') or row['tag'], 'tag': row['tag'],
                            'asset_id': asset, 'source_asset_id': source_asset,
                            'reference_text': row.get('reference_text', ''),
                            'reference_language': row.get('reference_language', ''),
                            'intensity': row.get('intensity', .5), 'status': 'ready' if ready else 'pending',
                            'error': error, 'verification_source': 'automatic',
                            'provenance': {'task_id': task_id, 'source_path': row['path'],
                                           'source_config': row.get('source_config'), 'revision': manifest.get('revision')}}
            entries.append(entry)
            imported_source.append(entry)
            existing.add(identity)
            counts['ready' if ready else 'pending'] += 1
        if entries != voice.get('references', []):
            voice = library.update_voice(voice['id'], voice['revision'], {**voice, 'references': entries})
        updated = json.loads(json.dumps(library._index))
        source = updated['migrations'].get('default_source')
        if isinstance(source, dict):
            known = {entry['id'] for entry in source.get('references', [])}
            source.setdefault('references', []).extend(entry for entry in imported_source if entry['id'] not in known)
        updated['migrations'].setdefault('reference_imports', {})[task_id] = {
            **counts, 'voice_id': voice['id'], 'revision': voice['revision']}
        library._save_index(updated)
        return dict(updated['migrations']['reference_imports'][task_id])

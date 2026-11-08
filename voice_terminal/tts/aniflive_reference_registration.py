"""AnifLive（本地语音）默认音色与导入参考的统一登记。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .aniflive_native import contained, digest


def register_model_default(library, package: Path, *, registered: bool | None = None) -> dict:
    """从已验证模型包登记其可编辑默认音色。"""
    package = Path(package).absolute()
    if registered is None:
        receipt = contained(package, package / 'application-receipt.json')
        policy = json.loads(receipt.read_text(encoding='utf-8')).get('reference_policy') if receipt.is_file() else None
        if policy is not None and policy != 'independent-v1':
            raise ValueError('模型参考登记策略未知，保留资料等待兼容处理。')
        registered = policy != 'independent-v1'
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
    return library.register_default(reference.read_bytes(), transcript.strip(), language=language, registered=registered)


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
        policy = manifest.get('reference_policy')
        if policy is not None and policy != 'independent-v1':
            raise ValueError('参考清单登记策略未知，保留资料等待兼容处理。')
        independent = policy == 'independent-v1'
        pointer = library._index.get('default_voice')
        prior = library.get_voice(pointer['voice_id']) if pointer else None
        first_registration = (not pointer or prior.get('default_reference_registered') is False) and not (
            library._index['migrations'].get('default_reference_choice') or library._index['migrations'].get('reference_imports'))
        default = register_model_default(library, package, registered=not independent)
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
            if not independent and row['path'] == manifest.get('default_audio'):
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
                            'error': error, 'verification_source': row.get('verification_source', 'automatic'),
                            'provenance': {'task_id': task_id, 'source_path': row['path'],
                                           'source_config': row.get('source_config'), 'revision': manifest.get('revision')}}
            entries.append(entry)
            imported_source.append(entry)
            existing.add(identity)
            counts['ready' if ready else 'pending'] += 1
        if independent and first_registration and not any(
                row.get('provenance', {}).get('source_path') == manifest.get('validation_audio')
                and row.get('reference_text') == voice['reference_text']
                and row.get('reference_language') == voice.get('reference_language') for row in entries):
            # 用户稍后补充的验收录音也独立登记，不能成为隐藏且无法选择的素材。
            validation_entry = {'id': hashlib.sha256((task_id + ':validation-reference').encode()).hexdigest()[:32],
                'name': '补充参考录音', 'tag': 'imported_reference', 'asset_id': voice['reference_asset_id'],
                'source_asset_id': None, 'reference_text': voice['reference_text'],
                'reference_language': voice.get('reference_language', 'zh'), 'intensity': .5,
                'status': 'ready', 'error': '', 'verification_source': 'manual',
                'provenance': {'task_id': task_id, 'source_path': 'validation-reference'}}
            entries.append(validation_entry)
            imported_source.append(validation_entry)
            counts['ready'] += 1
        if entries != voice.get('references', []):
            voice = library.update_voice(voice['id'], voice['revision'], {**voice, 'references': entries})
        if independent and first_registration:
            recommended = [row for row in voice.get('references', []) if row.get('status') == 'ready'
                           and row.get('provenance', {}).get('source_path') == manifest.get('recommendation_audio')]
            if len(recommended) == 1:
                voice = library.set_default_reference(voice['id'], voice['revision'], recommended[0]['id'])
        updated = json.loads(json.dumps(library._index))
        source = updated['migrations'].get('default_source')
        if isinstance(source, dict):
            known = {entry['id'] for entry in source.get('references', [])}
            source.setdefault('references', []).extend(entry for entry in imported_source if entry['id'] not in known)
            if independent and first_registration:
                source['revision'] = voice['revision']
        if independent and first_registration:
            updated['migrations']['default_reference_choice'] = {'source': 'author_recommendation',
                'registered': voice.get('default_reference_registered', True), 'task_id': task_id}
        updated['migrations'].setdefault('reference_imports', {})[task_id] = {
            **counts, 'voice_id': voice['id'], 'revision': voice['revision'],
            **({'default_registered': voice.get('default_reference_registered', True),
                'warning': '' if voice.get('default_reference_registered', True) else
                    '模型默认合成参考未登记，请在参考音频中选择一段已就绪录音并设为默认。'} if independent else {})}
        library._save_index(updated)
        return dict(updated['migrations']['reference_imports'][task_id])

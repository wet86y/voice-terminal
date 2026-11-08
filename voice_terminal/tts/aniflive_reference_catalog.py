"""AnifLive（本地语音）参考资料的安全解析和配对。"""
from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
from pathlib import PurePosixPath
from typing import Mapping

_AUDIO = {'.wav', '.mp3', '.flac', '.ogg'}
_TEXT = {'.txt', '.text', '.lab'}
_CONFIG = {'.json', '.yaml', '.yml'}
_LIMIT = 256 * 1024  # 单份配置或旁注的最大字节数。


def _language(value) -> str:
    """归一化来源明确声明的语言，不从台词内容猜测。"""
    aliases = {'中文': 'zh', '普通话': 'zh', 'chinese': 'zh', 'all_zh': 'zh', 'zh-cn': 'zh',
               '英文': 'en', '英语': 'en', 'english': 'en', '日文': 'ja', '日语': 'ja', 'japanese': 'ja', 'all_ja': 'ja',
               '韩文': 'ko', '韩语': 'ko', 'korean': 'ko', 'all_ko': 'ko', '粤语': 'yue', 'all_yue': 'yue'}
    code = value.strip().casefold() if isinstance(value, str) else ''
    return aliases.get(code, code)


def _source_audio(config: str, value: str, audio: set[str]) -> str:
    """仅在本次清单内解析路径；旧设备绝对路径只允许完整来源路径后缀唯一匹配。"""
    value = value.replace('\\', '/') if isinstance(value, str) else ''
    if not value:
        raise ValueError('参考音频路径为空。')
    if value.startswith('/') or ':' in value:
        candidates = {path for path in audio if value.casefold().endswith('/' + path.casefold())}
    else:
        candidates = set()
        for candidate in (value.removeprefix('./'), posixpath.normpath(posixpath.join(posixpath.dirname(config), value))):
            if candidate in audio:
                candidates.add(candidate)
    if len(candidates) != 1:
        raise ValueError('参考路径无法在本次清单内唯一定位。')
    return candidates.pop()


def _reference_nodes(data: dict):
    """有界读取数据对象；只识别完整字段组合，不执行脚本或说明文档。"""
    pending = [(data, '', '', 0)]
    visited = 0
    while pending and visited < 2000:
        node, key, context, depth = pending.pop(0)
        visited += 1
        if isinstance(node, dict):
            if sum(name in node for name in ('api_0322', 'api_0706', 'v2_api_0821')) > 1:
                continue  # 多个互斥接口快照并存且没有选用版本，不展开混登。
            yield node, key, context
            if depth < 6:
                pending.extend((value, str(name), context + '/' + str(name), depth + 1)
                               for name, value in node.items() if isinstance(value, (dict, list)))
        elif isinstance(node, list) and depth < 6:
            pending.extend((value, key, context, depth + 1) for value in node if isinstance(value, dict))


def _first(data: dict, keys: tuple[str, ...], default=''):
    values = [data[key] for key in keys if isinstance(data.get(key), str) and data[key].strip()]
    if len(set(values)) > 1:
        raise ValueError('同一配置含有冲突的参考字段。')
    return values[0] if values else default


def _document(config: str, raw: bytes) -> dict:
    """只解析数据配置，不执行来源仓库代码。"""
    if len(raw) > _LIMIT:
        return {}
    if config.casefold().endswith('.json'):
        data = json.loads(raw.decode('utf-8-sig'))
    else:
        import yaml
        data = yaml.safe_load(raw.decode('utf-8-sig'))
    return data if isinstance(data, dict) else {}


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('参考资料路径无效。')
    parts = value.split('/')
    if value.startswith('/') or any(part in {'', '.', '..'} for part in parts):
        raise ValueError('参考资料路径必须位于来源目录内。')
    return value


def _relative(config: str, value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or value.startswith('/'):
        raise ValueError('配置中的参考路径无效。')
    path = posixpath.normpath(posixpath.join(posixpath.dirname(config), value))
    return _path(path)


def _tag(value: str, audio: str) -> str:
    tag = re.sub(r'[^a-z0-9_-]+', '_', value.casefold()).strip('_-')[:48]
    if not tag or not tag[0].isalnum():
        tag = 'reference_' + hashlib.sha256(audio.encode('utf-8')).hexdigest()[:12]
    if tag in {'default', 'base_internal'}:
        tag = 'reference_' + tag
    return tag


def discover_references(paths: list[str], documents: Mapping[str, bytes]) -> list[dict]:
    """配对显式配置与同名旁注；无法确认的参考保留为待完善。"""
    names = {_path(name) for name in paths}
    audio = {name for name in names if PurePosixPath(name).suffix.casefold() in _AUDIO}
    result: list[dict] = []
    referenced: set[str] = set()
    used_keys: set[tuple] = set()
    defaults: set[str] = set()

    def append(path: str, text: str, language: str, label: str, origin: str | None,
               tag_source: str | None = None, intensity: float = .5) -> None:
        if path not in audio:
            return
        tag = _tag(tag_source or label, path)
        intensity_valid = not isinstance(intensity, bool) and isinstance(intensity, (int, float)) and math.isfinite(intensity) and 0 <= intensity <= 1
        level = float(intensity) if intensity_valid else .5
        lang = _language(language)
        lang = lang if lang in {'zh', 'yue', 'en', 'ja', 'ko'} else ''
        if (tag, lang, level) in used_keys:
            tag = tag[:50] + '_' + hashlib.sha256((path + str(origin)).encode('utf-8')).hexdigest()[:10]
        used_keys.add((tag, lang, level))
        value = text.strip() if isinstance(text, str) else ''
        valid = bool(value and len(value) <= 2000 and lang and intensity_valid)
        result.append({'path': path, 'name': label, 'tag': tag, 'reference_text': value[:2000],
                       'reference_language': lang, 'status': 'ready' if valid else 'pending',
                       'error': None if valid else '参考转写或语言缺失，或转写超出长度限制。',
                       'source_config': origin, 'intensity': level})
        referenced.add(path)

    for config, raw in sorted(documents.items()):
        if config not in names or PurePosixPath(config).suffix.casefold() not in _CONFIG or len(raw) > _LIMIT:
            continue
        try:
            data = _document(config, raw)
            expression = data.get('expression')
            if isinstance(expression, dict) and isinstance(expression.get('profiles'), list):
                for row in expression['profiles']:
                    if not isinstance(row, dict) or not isinstance(row.get('reference_audio'), str):
                        continue
                    path = _relative(config, row['reference_audio'])
                    label = str(row.get('name') or row.get('id') or PurePosixPath(path).stem).strip()[:80]
                    append(path, row.get('reference_text', ''), row.get('reference_language', ''),
                           label, config, str(row.get('emotion') or row.get('id') or label), row.get('intensity', .5))
            prompt = data.get('prompt')
            if not isinstance(prompt, dict):
                continue
            ref = prompt.get('ref_audio_path')
            if not isinstance(ref, str):
                continue
            path = _source_audio(config, ref, audio)
            if data.get('status') == 'final_selected':
                defaults.add(path)
            scene = data.get('scene') if isinstance(data.get('scene'), dict) else {}
            label = str(scene.get('usage') or data.get('name') or PurePosixPath(config).stem).strip()[:80]
            append(path, prompt.get('prompt_text', ''), prompt.get('prompt_lang', ''), label, config,
                   PurePosixPath(config).stem)
        except Exception:  # 单个来源配置损坏不阻断其余参考资料。
            continue

    # 兼容接口参数导出与角色配置：字段组合必须在同一个对象内，推荐必须有显式声明。
    for config, raw in sorted(documents.items()):
        if config not in names or PurePosixPath(config).suffix.casefold() not in _CONFIG:
            continue
        try:
            data = _document(config, raw)
        except Exception:
            continue
        selectors = [data[key] for key in ('default_profile', 'default_reference', 'default_reference_id',
                    'default_voice', 'selected_final') if isinstance(data.get(key), str) and data[key]]
        found: list[tuple[str, set[str]]] = []
        for node, key, context in _reference_nodes(data):
            try:
                default_fields = 'default_refer_path' in node
                if default_fields:
                    ref, text, language = (node.get('default_refer_path'), node.get('default_refer_text', ''),
                                           node.get('default_refer_language', ''))
                elif key in {'default_refer', 'default_reference'} and 'path' in node:
                    ref, text, language = node['path'], node.get('text', ''), node.get('language', '')
                elif PurePosixPath(config).name.casefold() == 'voices.json' and all(
                        name in node for name in ('gpt_path', 'sovits_path', 'ref_audio', 'ref_text', 'ref_lang')):
                    ref, text, language = node['ref_audio'], node['ref_text'], node['ref_lang']
                    if key == 'default':
                        defaults.add(_source_audio(config, ref, audio))
                else:
                    # 别名只在已知接口组合或明确的语音提供方配置中使用。
                    provider = 'gpt_sovits' in context.casefold().replace('-', '_')
                    internal = 'reference_text' in node and 'reference_language' in node
                    audio_keys = ('ref_audio_path', 'refer_wav_path') + (
                        ('reference_audio', 'reference_audio_path', 'ref_wav_path') if provider or internal else ())
                    ref = _first(node, audio_keys)
                    if not ref:
                        continue
                    text = _first(node, ('prompt_text', 'reference_text'))
                    language = _first(node, ('prompt_lang', 'prompt_language', 'reference_language'))
                path = _source_audio(config, ref, audio)
                identifiers = {str(node.get('id') or ''), str(node.get('name') or ''), key, path}
                found.append((path, identifiers - {''}))
                if default_fields or key in {'default_refer', 'default_reference'} or node.get('status') == 'final_selected' or any(
                        node.get(flag) is True for flag in ('is_default', 'default', 'recommended')):
                    defaults.add(path)
                if path not in referenced:
                    label = str(node.get('name') or node.get('id') or PurePosixPath(path).stem).strip()[:80]
                    append(path, text, language, label, config, str(node.get('emotion') or node.get('id') or key or label),
                           node.get('intensity', .5))
                elif text and language and not any(row['path'] == path and row['reference_text'] == str(text).strip()
                        and row['reference_language'] == _language(language) for row in result):
                    # 不同配置对同一录音声明不同转写或语言，保留冲突并阻止自动推荐。
                    for row in result:
                        if row['path'] == path:
                            row.update(status='pending', error='来源配置对该录音的转写或语言存在冲突，请核对。')
            except (ValueError, TypeError, AttributeError):
                continue
        for selector in selectors:
            matches = {path for path, identifiers in found if selector in identifiers}
            if not matches:
                try:
                    matches.add(_source_audio(config, selector, audio))
                except ValueError:
                    pass
            defaults.update(matches)

    # 仓库参考清单使用仓库根相对路径；优先保留场景配置的完整标记，再补清单中遗漏的资料。
    for config, raw in sorted(documents.items()):
        if config not in names or PurePosixPath(config).suffix.casefold() not in _CONFIG:
            continue
        try:
            data = _document(config, raw)
            rows = data.get('references')
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('file'), str):
                    continue
                path = _path(row['file'])
                profile = row.get('profile')
                if isinstance(profile, str) and PurePosixPath(profile).stem == data.get('selected_final'):
                    defaults.add(path)
                if path in referenced:
                    continue
                label = str(row.get('recommendation') or row.get('id') or PurePosixPath(path).stem).strip()[:80]
                append(path, row.get('prompt_text', ''), row.get('prompt_lang', ''), label, config,
                       PurePosixPath(profile).stem if isinstance(profile, str) else row.get('id'))
        except Exception:
            continue

    # GPT-SoVITS 标注清单：录音路径|说话人|语言|准确转写，只接受来源内可唯一定位的路径。
    list_rows: dict[str, list[tuple[str, str, str]]] = {}
    for name, raw in sorted(documents.items()):
        if name not in names or not name.casefold().endswith('.list') or len(raw) > _LIMIT:
            continue
        try:
            for line in raw.decode('utf-8-sig').splitlines():
                pieces = line.split('|', 3)
                if len(pieces) != 4:
                    continue
                relative, _, language, text = pieces
                try:
                    paths_found = {_path(relative), _relative(name, relative)} & audio
                except ValueError:
                    continue
                if len(paths_found) == 1:
                    list_rows.setdefault(paths_found.pop(), []).append((text, language.casefold(), name))
        except UnicodeError:
            continue

    by_stem: dict[str, list[str]] = {}
    for name in names:
        if PurePosixPath(name).suffix.casefold() in _TEXT:
            by_stem.setdefault(str(PurePosixPath(name).with_suffix('')).casefold(), []).append(name)
    for path in sorted(audio - referenced):
        annotations = list_rows.get(path, [])
        if len(annotations) == 1:
            text, language, origin = annotations[0]
            append(path, text, language, PurePosixPath(path).stem, origin)
            continue
        candidates = by_stem.get(str(PurePosixPath(path).with_suffix('')).casefold(), [])
        text = ''
        if len(candidates) == 1:
            raw = documents.get(candidates[0], b'')
            if len(raw) <= _LIMIT:
                try:
                    text = raw.decode('utf-8-sig').strip()
                except UnicodeError:
                    pass
        # 同名旁注没有语言元数据，不能猜测语言。
        append(path, text, '', PurePosixPath(path).stem, candidates[0] if len(candidates) == 1 else None)
    conditions: dict[str, set[tuple[str, str]]] = {}
    for row in result:
        if row['reference_text'] and row['reference_language']:
            conditions.setdefault(row['path'], set()).add((row['reference_text'], row['reference_language']))
    for row in result:
        row['is_default'] = row['path'] in defaults  # is_default（来源明确声明的默认参考）。
        if len(conditions.get(row['path'], ())) > 1:
            row.update(status='pending', error='来源配置对该录音的转写或语言存在冲突，请核对。')
    # 同一来源录音只登记一次；不同配置的来源信息保留，冲突条目仍待人工核对。
    unique: dict[str, dict] = {}
    for row in result:
        if row['path'] not in unique:
            unique[row['path']] = row
        else:
            existing = unique[row['path']]
            sources = {item for item in (existing.get('source_config'), row.get('source_config')) if item}
            existing['source_configs'] = sorted(set(existing.get('source_configs', [])) | sources)
            existing['is_default'] |= row['is_default']
            if row['status'] != 'ready':
                existing.update(status='pending', error=row['error'])
    return list(unique.values())

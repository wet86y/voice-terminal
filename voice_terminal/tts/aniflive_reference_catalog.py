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

    def append(path: str, text: str, language: str, label: str, origin: str | None,
               tag_source: str | None = None, intensity: float = .5) -> None:
        if path not in audio:
            return
        tag = _tag(tag_source or label, path)
        intensity_valid = not isinstance(intensity, bool) and isinstance(intensity, (int, float)) and math.isfinite(intensity) and 0 <= intensity <= 1
        level = float(intensity) if intensity_valid else .5
        lang = language if language in {'zh', 'yue', 'en', 'ja', 'ko'} else ''
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
            if config.casefold().endswith('.json'):
                data = json.loads(raw.decode('utf-8-sig'))
            else:
                import yaml
                data = yaml.safe_load(raw.decode('utf-8-sig'))
            if not isinstance(data, dict):
                continue
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
            path = _relative(config, ref)
            scene = data.get('scene') if isinstance(data.get('scene'), dict) else {}
            label = str(scene.get('usage') or data.get('name') or PurePosixPath(config).stem).strip()[:80]
            append(path, prompt.get('prompt_text', ''), prompt.get('prompt_lang', ''), label, config,
                   PurePosixPath(config).stem)
        except Exception:  # 单个来源配置损坏不阻断其余参考资料。
            continue

    by_stem: dict[str, list[str]] = {}
    for name in names:
        if PurePosixPath(name).suffix.casefold() in _TEXT:
            by_stem.setdefault(str(PurePosixPath(name).with_suffix('')).casefold(), []).append(name)
    for path in sorted(audio - referenced):
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
    return result

"""本地语音设置的唯一默认值、旧选择迁移和参数校验。"""
from __future__ import annotations
import re

DEFAULT_LOCAL_MODEL = 'minimax-female-shaonv-zh'  # DEFAULT_LOCAL_MODEL（本地默认模型）。
ANIFLIVE_OPTION_DEFAULTS = {  # ANIFLIVE_OPTION_DEFAULTS（引擎可选参数默认值）。
    'gpu_max_performance': False, 'aniflive_chunk_optimization': True, 'aniflive_first_audio_wait_ms': 0,
}


def migrate_choice(backend, model):
    """迁移旧本地选择；已保存的在线选择保持在线。"""
    if backend == 'voxcpm1':
        backend = 'aniflive'
    if model in {'voxcpm1', 'voxcpm2'}:
        model = DEFAULT_LOCAL_MODEL
    return backend, model


def valid_local_model(model):
    if not isinstance(model, str) or model in {'voxcpm1', 'voxcpm2', 'aniflive'}:
        return False
    return not model.endswith('.') and bool(re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', model)) and model.split('.')[0] not in {
        'con', 'prn', 'aux', 'nul', *[f'com{i}' for i in range(1, 10)], *[f'lpt{i}' for i in range(1, 10)]}


def validate_options(values):
    result = {}
    for name, default in ANIFLIVE_OPTION_DEFAULTS.items():
        value = values.get(name, default)
        if isinstance(default, bool):
            if type(value) is not bool:
                raise ValueError(f'{name}（本地合成开关）必须为布尔值。')
        elif type(value) is not int or not 0 <= value <= 200:
            raise ValueError('aniflive_first_audio_wait_ms（首音到达后等待毫秒）必须为 0–200 的整数。')
        result[name] = value
    revision = values.get('local_model_revision')
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{24}', revision)):
        raise ValueError('local_model_revision（模型精确版本）无效。')
    result['local_model_revision'] = revision
    return result

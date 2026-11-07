"""使用当前源码身份构造小型模型测试夹具，不借用真实引擎。"""
from pathlib import Path
from voice_terminal.tts.aniflive_native import digest, PATCHED_SHA256, PATCH_REVISION, UPSTREAM_COMMIT
from voice_terminal.tts.aniflive_models import BUILD_ADAPTER_FILES


def build_identity():
    program = Path(__file__).resolve().parents[1]
    return {'dependency_lock': digest(program / 'config/runtime/aniflive-runtime.lock.json'),
            'shared_lock': digest(program / 'config/runtime/aniflive-shared-assets.lock.json'),
            'source': {'upstream_commit': UPSTREAM_COMMIT, 'patch_revision': PATCH_REVISION,
                       'files': [{'path': path, 'after_sha256': value} for path, value in PATCHED_SHA256.items()]},
            'adapter': {name: digest(program / name) for name in BUILD_ADAPTER_FILES}}

"""旧引擎在仅导出补丁变化时复用，仍拒绝未知身份和运行实现改动。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from voice_terminal.tts.aniflive_models import BUILD_ADAPTER_FILES, validate_build_identity
from voice_terminal.tts.aniflive_native import UPSTREAM_COMMIT
from voice_terminal.tts.aniflive_environment import RUNTIME_VERSIONS


def fixture_program(root):
    for name in BUILD_ADAPTER_FILES:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'current runtime')
    config=root/'config/runtime';config.mkdir(parents=True)
    for name in ('aniflive-runtime.lock.json','aniflive-shared-assets.lock.json'):
        (config/name).write_text('{}')
    sha=hashlib.sha256(b'{}').hexdigest()
    identity={'dependency_lock':sha,'shared_lock':sha,'source':{'upstream_commit':UPSTREAM_COMMIT,'patch_revision':3},
              'adapter':{name:hashlib.sha256(b'old adapter').hexdigest() for name in BUILD_ADAPTER_FILES[:-1]}}
    rules={'schema':1,'profiles':[{'source':identity['source'],'adapter':identity['adapter'],
                                 'runtime_adapter':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in BUILD_ADAPTER_FILES}}]}
    (config/'aniflive-build-compatibility.json').write_text(json.dumps(rules))
    return identity


def test_reviewed_export_change_keeps_original_identity(tmp_path):
    identity=fixture_program(tmp_path);before=deepcopy(identity)
    validate_build_identity(identity,tmp_path)
    assert identity==before and identity['source']['patch_revision']==3


@pytest.mark.parametrize('field',['dependency_lock','source','adapter'])
def test_unknown_old_build_is_not_implicitly_compatible(tmp_path,field):
    identity=fixture_program(tmp_path)
    if field=='dependency_lock': identity[field]='a'*64
    elif field=='source': identity[field]['patch_revision']=2
    else: identity[field][next(iter(identity[field]))]='b'*64
    with pytest.raises(ValueError): validate_build_identity(identity,tmp_path)


def test_runtime_change_invalidates_reviewed_compatibility(tmp_path):
    identity=fixture_program(tmp_path)
    (tmp_path/'voice_terminal/tts/aniflive_service.py').write_bytes(b'changed inference behavior')
    with pytest.raises(ValueError): validate_build_identity(identity,tmp_path)


def test_current_release_accepts_only_pinned_historical_build():
    program=Path(__file__).resolve().parents[1]
    rules=json.loads((program/'config/runtime/aniflive-build-compatibility.json').read_text(encoding='utf-8'))
    profile=rules['profiles'][0]
    identity={'source':profile['source'],'adapter':profile['adapter'],
              'dependency_lock':hashlib.sha256((program/'config/runtime/aniflive-runtime.lock.json').read_bytes()).hexdigest(),
              'shared_lock':hashlib.sha256((program/'config/runtime/aniflive-shared-assets.lock.json').read_bytes()).hexdigest()}
    validate_build_identity(identity,program)


def test_runtime_contract_ignores_tool_and_lock_digest_changes_but_rejects_unknown_protocol(tmp_path):
    identity = fixture_program(tmp_path)
    path = tmp_path / 'config/runtime/aniflive-build-compatibility.json'
    rules = json.loads(path.read_text())
    rules['runtime_contract'] = {'revision': 1, 'upstream_commit': UPSTREAM_COMMIT,
                                 'source_patch_revisions': [3, 5],
                                 'runtime_packages': RUNTIME_VERSIONS}
    path.write_text(json.dumps(rules))
    identity['adapter'] = {'old-export-tool': 'a' * 64}
    identity['dependency_lock'] = 'old dependency provenance（旧依赖溯源）'
    (tmp_path / BUILD_ADAPTER_FILES[0]).write_bytes(b'new tool implementation')
    original = deepcopy(identity)
    validate_build_identity(identity, tmp_path)
    assert identity == original  # 旧收据保持真实身份，不伪造为新构建。
    for changed in ({**identity, 'runtime_contract': 2},
                    {**identity, 'source': {**identity['source'], 'patch_revision': 999}},
                    {**identity, 'source': {**identity['source'], 'upstream_commit': 'unknown'}}):
        with pytest.raises(ValueError, match='推理协议'):
            validate_build_identity(changed, tmp_path)

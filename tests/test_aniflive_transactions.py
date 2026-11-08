"""本机依赖恢复、生成模型登记与构建错误分类契约。"""
import hashlib
import json
from pathlib import Path
import pytest
from voice_terminal.tts.aniflive_transactions import recover_dependencies
from voice_terminal.tts.aniflive_build_diagnostics import classify_build_logs
from voice_terminal.tts.aniflive_models import AnifLiveModelRegistry
from tests.test_model_resource_updates import workspace


@pytest.mark.parametrize('state', ['prepared', 'committed'])
@pytest.mark.parametrize('folders', [('packages', 'export-packages'), ('source',)])
def test_dependency_swap_recovers_both_versions_without_mixing(tmp_path, state, folders):
    for folder in folders:
        for name, content in ((folder, b'new'), ('.' + folder + '-old-1', b'old'), ('.' + folder + '-stage-1', b'candidate')):
            (tmp_path / name).mkdir()
            (tmp_path / name / 'version').write_bytes(content)
    rows = [{'target': folder, 'stage': '.' + folder + '-stage-1', 'backup': '.' + folder + '-old-1', 'original': True}
            for folder in folders]
    (tmp_path / 'dependency-transaction.json').write_text(json.dumps({'schema': 1, 'state': state, 'items': rows}), encoding='utf-8')
    recover_dependencies(tmp_path)
    for folder in folders:
        assert (tmp_path / folder / 'version').read_bytes() == (b'old' if state == 'prepared' else b'new')
    assert not list(tmp_path.glob('.*')) and not (tmp_path / 'dependency-transaction.json').exists()


def test_dependency_recovery_rejects_escaping_directory(tmp_path):
    journal = tmp_path / 'dependency-transaction.json'
    journal.write_text(json.dumps({'schema': 1, 'state': 'prepared', 'items': [
        {'target': 'packages', 'stage': '../unrelated', 'backup': '.packages-old-1', 'original': True}]}), encoding='utf-8')
    with pytest.raises(ValueError, match='身份'):
        recover_dependencies(tmp_path)
    assert journal.exists()


def _verified():
    return {'validation': {'enqueue_verified': True, 'engine_count': 9,
                          'smoke_inference': {'pytorch_fallback': False}}, 'streaming_verified': True}


def test_capture_error_requires_explicit_fallback_and_real_execution_proof(tmp_path):
    log = tmp_path / 'engines/fingerprint/logs/sovits_stream.log'
    log.parent.mkdir(parents=True)
    error = '[TensorRT:ERROR] IExecutionContext::enqueueV3: Error Code 1: Myelin MyelinCheckException\nerror: operation not permitted when stream is capturing\n'
    log.write_text(error, encoding='utf-8')
    with pytest.raises(RuntimeError, match='未分类'):
        classify_build_logs(tmp_path, _verified())

    log.write_text(error + '[TensorRT:INFO] The engine is not graph capturable. Falling back to not using CUDA graphs.\n', encoding='utf-8')
    result = classify_build_logs(tmp_path, _verified())
    assert len(result['capture_probe_fallbacks']) == 1
    assert result['capture_probe_fallbacks'][0]['log_sha256'] == hashlib.sha256(log.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='验收'):
        classify_build_logs(tmp_path, {**_verified(), 'streaming_verified': False})
    log.write_text('[TensorRT:ERROR] out of memory\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='未分类'):
        classify_build_logs(tmp_path, _verified())


def test_registry_full_loading_check_rejects_same_length_damage(tmp_path):
    model, revision = 'example-model', 'a' * 24
    folder = tmp_path / 'models/aniflive-tts/packages' / model / revision
    folder.mkdir(parents=True)
    engine = folder / 'model.engine'
    engine.write_bytes(b'original')
    (folder / 'manifest.json').write_text(json.dumps({'model_id': model, 'format': 'aniflive-tts-model-package',
        'model_family': 'gsv-v2proplus', 'backend': 'TensorRT-11'}), encoding='utf-8')
    (folder / 'checksums.json').write_text('{}', encoding='utf-8')
    from tests.aniflive_fixtures import build_identity
    identity = {**build_identity(), 'input': 'fixture'}
    (folder / 'application-receipt.json').write_text(json.dumps({'identity': identity,
        'validation': {'enqueue_verified': True, 'engine_count': 9}, 'streaming_verified': True,
        'files': [{'path': p.name, 'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in (engine, folder / 'manifest.json')]}), encoding='utf-8')
    registry = AnifLiveModelRegistry(tmp_path)
    registry.register(model, revision, '测试模型', identity)
    from voice_terminal.tts.aniflive_models import validate_registered_package
    checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (engine, folder / 'manifest.json', folder / 'application-receipt.json')}
    (folder / 'checksums.json').write_text(json.dumps(checksums), encoding='utf-8')
    assert validate_registered_package(tmp_path, folder) == checksums
    (folder / 'notes.txt').write_text('启动不扫描额外旁注文件', encoding='utf-8')
    assert validate_registered_package(tmp_path, folder) == checksums
    with pytest.raises(ValueError, match='未登记文件'):
        registry.resolve(model, verify_hashes=True)
    (folder / 'notes.txt').unlink()
    registry.resolve(model, verify_hashes=True)
    engine.write_bytes(b'modified')
    with pytest.raises(ValueError, match='摘要不符'):
        registry.resolve(model, verify_hashes=True)
    engine.unlink()
    assert not next(row for row in registry.models() if row['id'] == model)['available']


def test_build_identity_preserves_provenance_and_rejects_incompatible_runtime_protocol():
    from copy import deepcopy
    from tests.aniflive_fixtures import build_identity
    from voice_terminal.tts.aniflive_models import validate_build_identity
    program = Path(__file__).resolve().parents[1]
    identity = build_identity()
    validate_build_identity(identity, program)
    for field in ('dependency_lock', 'shared_lock', 'adapter'):
        stale = deepcopy(identity)
        if field in {'dependency_lock', 'shared_lock'}:
            stale[field] = '0' * 64
        else:
            stale[field]['scripts/run-aniflive.py'] = '0' * 64
        validate_build_identity(stale, program)  # 工具与下载锁摘要属于溯源，不是推理协议。
    for field in ('patch', 'upstream', 'protocol'):
        stale = deepcopy(identity)
        if field == 'patch': stale['source']['patch_revision'] = 999
        elif field == 'upstream': stale['source']['upstream_commit'] = '0' * 40
        else: stale['runtime_contract'] = 999
        with pytest.raises(ValueError, match='推理协议'):
            validate_build_identity(stale, program)


def test_model_registration_rolls_back_with_model_manifest(workspace, tmp_path, monkeypatch):
    from voice_terminal import resource_distribution as resources
    program, root, manager = workspace
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    (candidate / 'generated.engine').write_bytes(b'engine')
    (candidate / 'application-receipt.json').write_bytes(b'{}')
    (candidate / 'checksums.json').write_bytes(b'{}')
    resource = resources.ResourceSpec('tts-aniflive-example', 'model', '事务样本',
        'aniflive-tts/packages/example/' + '1' * 24, '1' * 24, {},
        tuple(resources.ResourceFile(p.name, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest(), False)
              for p in candidate.iterdir()), required=False)
    before_manifest = (root / 'models/manifest.json').read_bytes()
    before_release = (root / 'app/release.json').read_bytes()
    original = resources._atomic_write
    injected = []
    def fail_registry(path, content):
        if path == root / 'data/config/aniflive-models.json':
            injected.append(True)
            raise OSError('模拟登记写入失败')
        original(path, content)
    monkeypatch.setattr(resources, '_atomic_write', fail_registry)
    with pytest.raises(resources.ResourceError):
        manager._commit_candidate(resource, candidate, 'aniflive-test', model_registration=b'{"schema":1,"models":{}}')
    assert injected == [True]
    assert (root / 'models/manifest.json').read_bytes() == before_manifest
    assert (root / 'app/release.json').read_bytes() == before_release
    assert not (root / 'models' / resource.destination).exists()
    assert not (root / 'data/config/aniflive-models.json').exists()
    assert not manager.transaction_marker.exists()


def test_generated_package_commit_preserves_strict_upstream_inventory(workspace, tmp_path):
    from voice_terminal import resource_distribution as resources
    _, root, manager = workspace
    candidate = tmp_path / 'generated-package'
    candidate.mkdir()
    (candidate / 'generated.engine').write_bytes(b'engine')
    (candidate / 'application-receipt.json').write_bytes(b'{}')
    checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in candidate.iterdir()}
    (candidate / 'checksums.json').write_text(json.dumps(checksums), encoding='utf-8')
    spec = resources.ResourceSpec('tts-aniflive-example', 'model', '严格包清单测试',
        'aniflive-tts/packages/example/' + '1' * 24, '1' * 24, {},
        tuple(resources.ResourceFile(p.name, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest(), False)
              for p in candidate.iterdir()), required=False)
    manager._commit_candidate(spec, candidate, 'aniflive-inventory', model_registration=b'{"schema":1,"models":{}}')
    target = root / 'models' / spec.destination
    assert not (target / 'resource-install.json').exists()
    assert {p.name for p in target.iterdir()} == set(checksums) | {'checksums.json'}
    assert all(hashlib.sha256((target / name).read_bytes()).hexdigest() == value for name, value in checksums.items())
    manifest = json.loads((root / 'models/manifest.json').read_text(encoding='utf-8'))
    assert sum(row['path'].startswith(spec.destination + '/') for row in manifest['files']) == 3

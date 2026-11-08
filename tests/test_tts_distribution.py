"""本地语音退役迁移、固定输入分发及实际加载前门禁回归。"""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import wave
import pytest
from voice_terminal.config import AppConfig
from voice_terminal.resource_distribution import ResourceCatalog, ResourceError
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.providers.aniflive_server import AnifLiveTtsServer
from voice_terminal.tts.aniflive_models import AnifLiveModelRegistry
from tests.test_tts_model_api import registered_model_fixtures

BASE, TANG = 'minimax-female-shaonv-zh', 'tangyuge-v2pp-d78cf598'


@pytest.mark.parametrize('backend,selected,available,online,ready', [
    ('minimax', TANG, [BASE], False, False), ('aniflive', TANG, [BASE], False, False),
    ('aniflive', BASE, [BASE, TANG], True, True), ('aniflive', TANG, [], True, False),
    ('minimax', TANG, [BASE], True, True),
])
def test_startup_preserves_explicit_tts_selection(tmp_path, backend, selected, available, online, ready):
    registry = AnifLiveModelRegistry(tmp_path)
    for model in (BASE, TANG):
        if model not in available:
            package, _, _ = registry.resolve(model)
            (package / 'fixture.engine').unlink()
    config = AppConfig()
    runtime = SessionRuntime(replace(config, tts=replace(config.tts, backend=backend, local_model=selected)), runtime_data_dir=tmp_path / 'data')
    runtime._api_key = lambda _: 'configured-test-key' if online else ''
    if ready:
        runtime._select_available_tts()
    else:
        with pytest.raises((RuntimeError, ValueError), match='所选|缺失|验证'):
            runtime._select_available_tts()
    assert (runtime.config.tts.backend, runtime.config.tts.local_model) == (backend, selected)


def test_missing_online_credentials_do_not_trigger_local_fallback(tmp_path):
    runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / 'data')
    runtime._api_key = lambda _: ''
    with pytest.raises(RuntimeError, match='填写密钥或显式选择'):
        runtime._select_available_tts()
    assert runtime.config.tts.backend == 'minimax'


@pytest.mark.parametrize('damage', [b'wrong-size', b''])
def test_corrupt_model_cannot_spawn_managed_service(tmp_path, monkeypatch, damage):
    registry = AnifLiveModelRegistry(tmp_path)
    package, revision, _ = registry.resolve(BASE)
    (package / 'fixture.engine').write_bytes(damage)
    monkeypatch.setattr('voice_terminal.providers.aniflive_server.asyncio.create_subprocess_exec',
                        lambda *a, **kw: pytest.fail('损坏模型不能派生服务'))
    server = AnifLiveTtsServer(tmp_path, Path(__file__).resolve().parents[1], local_model=BASE,
                              revision=revision, library_root=tmp_path / 'data/tts/aniflive/voices')
    with pytest.raises(ValueError, match='大小不符|摘要不符'):
        asyncio.run(server.ensure_ready())
    assert server.pid is None


@pytest.mark.parametrize('field', ['runtime_contract', 'source'])
def test_self_consistent_outdated_build_is_unavailable(tmp_path, field):
    registry = AnifLiveModelRegistry(tmp_path)
    package, revision, _ = registry.resolve(BASE)
    record = registry.read()
    path = package / 'application-receipt.json'
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if field == 'source':
        receipt['identity']['source']['patch_revision'] = 999
    else:
        receipt['identity'][field] = 999
    record['models'][BASE]['versions'][revision]['identity'] = receipt['identity']
    path.write_text(json.dumps(receipt), encoding='utf-8')
    registry.path.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='推理协议'):
        registry.resolve(BASE)
    assert not next(row for row in registry.models() if row['id'] == BASE)['available']
    assert next(row for row in registry.models() if row['id'] == TANG)['available']


def test_unlisted_installer_receipt_is_rejected(tmp_path):
    registry = AnifLiveModelRegistry(tmp_path)
    package, _, _ = registry.resolve(BASE)
    (package / 'resource-install.json').write_text('{}', encoding='utf-8')
    registry.resolve(BASE)  # 普通启动不因额外旁车记录拒绝已验证引擎。
    with pytest.raises(ValueError, match='未登记文件'):
        registry.resolve(BASE, verify_hashes=True)


def test_catalog_pins_inputs_and_seed_instead_of_device_engines():
    root = Path(__file__).resolve().parents[1]
    catalog = ResourceCatalog(root)
    for old in ('tts-voxcpm1', 'tts-voxcpm2', 'tts-audiocpp-runtime'):
        with pytest.raises(ResourceError):
            catalog.get(old)
    source = json.loads((root / 'config/resources/catalog.json').read_text(encoding='utf-8'))
    for row in source['resources']:
        if row.get('preparation') == 'aniflive_local_build':
            assert row['required'] is False and row['files'] == []
            assert len(row['source']['revision']) == 40
            assert row['source']['transfer']['kind'] == 'fixed_inputs_local_build'
    presets = json.loads((root / 'config/aniflive-presets.json').read_text(encoding='utf-8'))
    for model in presets['models']:
        assert len(model['revision']) == 40 and len(model['weights']) == 2
        assert all(len(row['sha256']) == 64 and row['size_bytes'] > 0 for row in model['weights'])
    seed = presets['models'][0]['reference']
    path = root / 'config/resources' / seed['asset']
    assert path.stat().st_size == seed['size_bytes'] and hashlib.sha256(path.read_bytes()).hexdigest() == seed['sha256']
    with wave.open(str(path)) as recording:
        assert recording.getframerate() == 32000 and recording.getnchannels() == 1
    assert seed['text'] and seed['language'] == 'zh'


def test_old_recordings_are_preserved_without_initializing_old_libraries(tmp_path):
    originals = {}
    for model in ('voxcpm1', 'voxcpm2'):
        folder = tmp_path / 'data/tts' / model / 'voices'; folder.mkdir(parents=True)
        for name in ('index.json', 'recording.wav'):
            file = folder / name; file.write_bytes(b'legacy-user-material'); originals[file] = file.read_bytes()
    runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / 'data')
    assert set(runtime.voice_library.libraries) == {runtime.config.tts.local_model}
    assert all(file.read_bytes() == content for file, content in originals.items())
    for model in ('voxcpm1', 'voxcpm2'):
        with pytest.raises(ValueError):
            runtime.voice_library.for_model(model)

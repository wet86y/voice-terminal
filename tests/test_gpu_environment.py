"""GPU environment（显卡环境）：真实后端检查、资源门禁与未知指标。"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from voice_terminal import compute
from voice_terminal.config import AppConfig, COMPUTE_MODEL_IDS
from voice_terminal.gpu_memory import aggregate_gpu_counters, process_vram_bytes


def test_process_gpu_memory_combines_adapters_and_preserves_invalid_samples():
    values = aggregate_gpu_counters([
        ('pid_12_luid_0_phys_0', 0, 100), ('pid_12_luid_1_phys_0', 1, 200),
        ('pid_13_luid_0_phys_0', 0, 0), ('pid_14_luid_0_phys_0', 0xC0000BC6, 999),
        ('pid_14_luid_1_phys_0', 0, 100), ('pid_15_luid_0_phys_0', 0, -1),
        ('_Total', 0, 999999),
    ])
    assert values == {12: 300, 13: 0, 14: None, 15: None}
    assert process_vram_bytes(12, snapshot=values) == 300
    assert process_vram_bytes(90, snapshot=values) == 0
    assert process_vram_bytes(12, snapshot=None) is None
    assert process_vram_bytes(14, snapshot=values) is None


def test_torch_device_declaration_does_not_pass_without_working_cuda_computation():
    backend = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True, synchronize=lambda: None),
                              ones=Mock(side_effect=RuntimeError('驱动动态库缺失')))
    assert compute._torch_cuda_probe(backend) is False
    backend.ones = lambda *_, **__: np.ones((8, 8), dtype=np.float32)
    assert compute._torch_cuda_probe(backend) is True


@pytest.mark.parametrize('provider', ['CPUExecutionProvider', 'CUDAExecutionProvider'])
def test_onnx_requires_profile_evidence_of_cuda_execution(provider):
    class Session:
        def __init__(self, _model, *, sess_options, providers):
            self.profile = Path(sess_options.profile_file_prefix + '.json')
        def disable_fallback(self): pass
        def get_providers(self): return ['CUDAExecutionProvider', 'CPUExecutionProvider']
        def run(self, _, inputs): return [inputs['a'] @ inputs['b']]
        def end_profiling(self):
            self.profile.write_text(json.dumps([{'args': {'provider': provider, 'op_name': 'MatMul'}}]), encoding='utf-8')
            return str(self.profile)
    backend = SimpleNamespace(get_available_providers=lambda: ['CUDAExecutionProvider', 'CPUExecutionProvider'],
        preload_dlls=lambda: None, SessionOptions=SimpleNamespace, InferenceSession=Session)
    assert compute._onnx_cuda_probe(backend) is (provider == 'CUDAExecutionProvider')
    backend.InferenceSession = Mock(side_effect=RuntimeError('显卡运行库缺失'))
    assert compute._onnx_cuda_probe(backend) is False


def test_installed_framework_does_not_enable_gpu_without_model_assets(tmp_path):
    config = AppConfig(model_dir=tmp_path)
    backend = {'models': {name: {'gpu_available': True, 'reason': None} for name in COMPUTE_MODEL_IDS}}
    assert all(not row['gpu_available'] for row in compute.model_hardware_status(backend, config)['models'].values())
    gpu_model = tmp_path / 'analysis/smart-turn/smart-turn-v3.2-gpu.onnx'
    gpu_model.parent.mkdir(parents=True)
    gpu_model.touch()
    result = compute.model_hardware_status(backend, config)
    assert result['models']['smart_turn']['gpu_available'] is True
    assert result['models']['campplus']['gpu_available'] is False
    assert all(row['gpu_available'] for row in backend['models'].values()), '不能篡改缓存的框架探测结果'


def test_refresh_replaces_host_snapshot_through_an_isolated_probe(monkeypatch):
    cached = {'models': {'smart_turn': {'gpu_available': True}}, 'gpu': {}, 'backends': {}, 'system_memory': {}}
    fresh = {**cached, 'models': {'smart_turn': {'gpu_available': False, 'reason': '运行库缺失'}}}
    monkeypatch.setattr(compute, '_capabilities', cached)
    monkeypatch.setenv('VOICE_RESOURCE_ROLE', 'voice')
    monkeypatch.setenv('VOICE_HARDWARE_CAPABILITIES', json.dumps(cached))
    with (patch.object(compute, '_backend_hardware_status', side_effect=AssertionError('主服务不得加载显卡框架')),
          patch.object(compute.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(fresh))) as probe):
        assert compute.hardware_status()['models']['smart_turn']['gpu_available'] is True
        probe.assert_not_called()
        assert compute.hardware_status(refresh=True)['models']['smart_turn']['gpu_available'] is False
        probe.assert_called_once()
        command = probe.call_args.args[0]
        assert any(argument.endswith('backend-entry.py') for argument in command), '隔离解释器必须使用正式显式模块入口'
        assert command[-1] == 'voice_terminal.hardware_probe'
        assert compute.hardware_status()['models']['smart_turn']['gpu_available'] is False


@pytest.mark.parametrize('profile,compatible_gpu,current_lock,explicit_registration,expected', [
    ('cu128', False, False, False, True),
    ('compatible-private', True, True, False, True),
    ('compatible-private', True, False, False, False),
    ('compatible-private', False, True, False, False),
    ('cpu-private', False, True, False, False),
    ('compatible-private', True, True, True, False),
])
def test_migrated_private_gpu_preserves_backend_capabilities_with_current_lock(
        tmp_path, monkeypatch, profile, compatible_gpu, current_lock, explicit_registration, expected):
    import hashlib
    program=tmp_path/'app';lock=program/'config/runtime/voice-runtime.lock.json'
    lock.parent.mkdir(parents=True);lock.write_bytes(b'current dependency lock')
    runtime=tmp_path/'runtimes/python/runtime.json';runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({'schema':2,'profile':profile,'compatible_gpu':compatible_gpu,
        'lock_sha256':hashlib.sha256(lock.read_bytes()).hexdigest() if current_lock else 'old'}),encoding='utf-8')
    if explicit_registration:
        registration=tmp_path/'data/config/gpu-environment.json';registration.parent.mkdir(parents=True)
        registration.write_text('{}',encoding='utf-8')
    monkeypatch.setenv('VOICE_INSTALL_ROOT',str(tmp_path));monkeypatch.setenv('VOICE_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.setenv('VOICE_PROGRAM_ROOT',str(program));monkeypatch.setenv('VOICE_LAUNCH_CHANNEL','product')
    monkeypatch.delenv('VOICE_GPU_PROBE',raising=False);monkeypatch.delenv('VOICE_GPU_FALLBACK',raising=False)
    monkeypatch.setattr(compute,'_capabilities',{'models':{'smart_turn':{'gpu_available':True,'reason':None}}})
    with patch.object(compute,'_backend_hardware_status',side_effect=AssertionError('复用已有后端检查结果')):
        assert compute.hardware_status()['models']['smart_turn']['gpu_available'] is expected


@pytest.mark.parametrize('provider', ['CPUExecutionProvider', 'CUDAExecutionProvider'])
def test_actual_model_requires_cuda_nodes_even_if_session_advertises_gpu(monkeypatch, provider, tmp_path):
    class Session:
        def __init__(self, _model, *, sess_options, providers):
            self.profile = Path(sess_options.profile_file_prefix + '.json')
        def disable_fallback(self): pass
        def get_providers(self): return ['CUDAExecutionProvider', 'CPUExecutionProvider']
        def get_inputs(self): return [SimpleNamespace(name='feats', type='tensor(float)', shape=['B', 'T', 80])]
        def run(self, _, inputs):
            assert inputs['feats'].shape == (1, 160, 80)
            return [np.ones((1, 256), dtype=np.float32)]
        def end_profiling(self):
            self.profile.write_text(json.dumps([{'args': {'provider': provider, 'op_name': 'Conv'}}]), encoding='utf-8')
            return str(self.profile)
    backend = SimpleNamespace(preload_dlls=Mock(), SessionOptions=SimpleNamespace, InferenceSession=Session)
    monkeypatch.setitem(__import__('sys').modules, 'onnxruntime', backend)
    assert compute.onnx_model_cuda_probe('wespeaker', tmp_path / 'model.onnx')['ready'] is (provider == 'CUDAExecutionProvider')
    backend.preload_dlls.assert_called_once()


def test_model_checks_are_isolated_cached_by_file_and_refreshed_with_backend_snapshot(tmp_path, monkeypatch):
    model = tmp_path / 'model.onnx'
    model.write_bytes(b'first')
    monkeypatch.setattr(compute, '_model_probe_cache', {})
    with patch.object(compute.subprocess, 'run', return_value=SimpleNamespace(stdout='{"ready":false,"reason":"卷积库缺失"}')) as probe:
        assert not compute._isolated_onnx_model_probe('campplus', model, 1)['ready']
        assert not compute._isolated_onnx_model_probe('campplus', model, 1)['ready']
        assert probe.call_count == 1
        assert probe.call_args.kwargs['env']['PYTHONDONTWRITEBYTECODE'] == '1'
        assert any(argument.endswith('backend-entry.py') for argument in probe.call_args.args[0])
        compute._isolated_onnx_model_probe('campplus', model, 2)
        assert probe.call_count == 2
        model.write_bytes(b'changed-size')
        compute._isolated_onnx_model_probe('campplus', model, 2)
        assert probe.call_count == 3


def test_present_model_file_and_framework_do_not_override_failed_actual_model_probe(tmp_path):
    config = AppConfig(model_dir=tmp_path)
    config.speaker_model_path.parent.mkdir(parents=True)
    config.speaker_model_path.touch()
    hardware = {'models': {'campplus': {'gpu_available': True, 'reason': None}}}
    with patch.object(compute, '_isolated_onnx_model_probe', return_value={'ready': False, 'reason': '模型算子无法执行'}):
        status = compute.model_hardware_status(hardware, config, verify=True)['models']['campplus']
    assert not status['gpu_available']
    assert status['reason'] == '模型算子无法执行'


@pytest.mark.parametrize('base_current,gpu_current,stale_registration,expected', [
    (True, True, False, True), (True, True, True, True),
    (True, False, False, False), (True, False, True, False), (False, True, False, False),
])
def test_managed_gpu_uses_shared_interpreter_and_independent_current_locks(
        tmp_path, monkeypatch, base_current, gpu_current, stale_registration, expected):
    """Managed GPU（卡片安装显卡依赖）无需手动登记，失效登记也不能覆盖当前锁验证。"""
    import hashlib
    program = tmp_path / 'app'
    locks = program / 'config/runtime'
    locks.mkdir(parents=True)
    (locks / 'voice-runtime.lock.json').write_bytes(b'cpu-lock')
    (locks / 'voice-gpu.lock.json').write_bytes(b'gpu-lock')
    runtime = tmp_path / 'runtimes/python'
    runtime.mkdir(parents=True)
    (runtime / 'runtime.json').write_text(json.dumps({
        'schema': 2, 'profile': 'gpu-private', 'gpu_profile': 'cu128',
        'lock_sha256': hashlib.sha256(b'cpu-lock').hexdigest() if base_current else 'old',
        'gpu_lock_sha256': hashlib.sha256(b'gpu-lock').hexdigest() if gpu_current else 'old',
    }), encoding='utf-8')
    if stale_registration:
        registration = tmp_path / 'data/config/gpu-environment.json'
        registration.parent.mkdir(parents=True)
        registration.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('VOICE_INSTALL_ROOT', str(tmp_path))
    monkeypatch.setenv('VOICE_PROGRAM_ROOT', str(program))
    monkeypatch.setenv('VOICE_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('VOICE_LAUNCH_CHANNEL', 'product')
    monkeypatch.delenv('VOICE_GPU_PROBE', raising=False)
    monkeypatch.delenv('VOICE_GPU_FALLBACK', raising=False)
    monkeypatch.setattr(compute, '_capabilities', {'models': {'paraformer': {'gpu_available': True, 'reason': None}}})
    assert compute.hardware_status()['models']['paraformer']['gpu_available'] is expected


def test_managed_shared_runtime_takes_priority_over_legacy_independent_gpu(tmp_path, monkeypatch):
    """Shared runtime（现用解释器）安装显卡能力后，不再被历史独立环境登记覆盖。"""
    from voice_terminal import gpu_environment
    runtime = tmp_path / 'runtimes/python-gpu'
    runtime.mkdir(parents=True)
    (runtime / 'python.exe').write_bytes(b'legacy')
    models = tmp_path / 'models'
    models.mkdir()
    data = tmp_path / 'data'
    registration = data / 'config/gpu-environment.json'
    registration.parent.mkdir(parents=True)
    registration.write_text(json.dumps({'path': 'runtimes/python-gpu',
        'environment_fingerprint': gpu_environment.fingerprint(runtime),
        'models_fingerprint': gpu_environment.fingerprint(models),
        'models': {'paraformer': {'gpu_available': True}},
    }), encoding='utf-8')
    monkeypatch.setattr(gpu_environment, 'managed_runtime', lambda *_: False)
    assert gpu_environment.worker_python(tmp_path, data, 'asr', 'shared-python', {'paraformer': 'cuda'}) == str(runtime / 'python.exe')
    monkeypatch.setattr(gpu_environment, 'managed_runtime', lambda *_: True)
    assert gpu_environment.worker_python(tmp_path, data, 'asr', 'shared-python', {'paraformer': 'cuda'}) == 'shared-python'

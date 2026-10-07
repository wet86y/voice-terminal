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
    assert result['models']['wespeaker']['gpu_available'] is False
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
        assert not compute._isolated_onnx_model_probe('wespeaker', model, 1)['ready']
        assert not compute._isolated_onnx_model_probe('wespeaker', model, 1)['ready']
        assert probe.call_count == 1
        assert probe.call_args.kwargs['env']['PYTHONDONTWRITEBYTECODE'] == '1'
        assert any(argument.endswith('backend-entry.py') for argument in probe.call_args.args[0])
        compute._isolated_onnx_model_probe('wespeaker', model, 2)
        assert probe.call_count == 2
        model.write_bytes(b'changed-size')
        compute._isolated_onnx_model_probe('wespeaker', model, 2)
        assert probe.call_count == 3


def test_present_model_file_and_framework_do_not_override_failed_actual_model_probe(tmp_path):
    config = AppConfig(model_dir=tmp_path)
    config.speaker_model_path.parent.mkdir(parents=True)
    config.speaker_model_path.touch()
    hardware = {'models': {'wespeaker': {'gpu_available': True, 'reason': None}}}
    with patch.object(compute, '_isolated_onnx_model_probe', return_value={'ready': False, 'reason': '模型算子无法执行'}):
        status = compute.model_hardware_status(hardware, config, verify=True)['models']['wespeaker']
    assert not status['gpu_available']
    assert status['reason'] == '模型算子无法执行'

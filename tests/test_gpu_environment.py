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
        assert compute.hardware_status()['models']['smart_turn']['gpu_available'] is False

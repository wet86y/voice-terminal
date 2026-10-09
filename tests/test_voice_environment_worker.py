"""完整语音环境：本地依赖复用与已验证显卡构建兼容。"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location('voice_environment_worker', Path(__file__).parents[1] / 'scripts/voice-environment-worker.py')
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)

LOCK = {'wheels': [{'name': 'torch', 'version': '2.11.0+cpu'}, {'name': 'torchaudio', 'version': '2.11.0+cpu'},
                   {'name': 'onnxruntime', 'version': '1.24.3'}, {'name': 'funasr', 'version': '1.4.14'}]}


def versions(name):
    """versions（模拟安装版本）：现用环境包含显卡计算库，缺少音频库。"""
    values = {'torch': '2.11.0+cu128', 'onnxruntime-gpu': '1.24.4', 'funasr': '1.4.14'}
    if name not in values:
        raise worker.metadata.PackageNotFoundError(name)
    return values[name]


def test_inventory_reuses_existing_gpu_and_requests_only_missing_dependencies():
    with patch.object(worker.metadata, 'version', side_effect=versions):
        result = worker.inventory(['torch', 'torchaudio', 'onnxruntime', 'funasr'], LOCK, compatible_gpu=True)
    assert result['reused'] == ['torch', 'onnxruntime', 'funasr']
    assert set(result['rejected']) == {'torchaudio'}


@pytest.mark.parametrize('name', ['torch', 'onnxruntime'])
def test_cpu_only_installation_does_not_silently_accept_gpu_packages(name):
    with patch.object(worker.metadata, 'version', side_effect=versions):
        with pytest.raises((ValueError, worker.metadata.PackageNotFoundError)):
            worker.installed_version(name, LOCK)


def test_unverified_gpu_version_still_requires_installation():
    with patch.object(worker.metadata, 'version', return_value='2.10.0+cu128'):
        result = worker.inventory(['torch'], LOCK, compatible_gpu=True)
    assert result['reused'] == []
    assert 'torch' in result['rejected']


def test_matching_versions_do_not_hide_actual_dependency_load_failure():
    with (patch.object(worker.metadata, 'version', side_effect=versions),
          patch.object(worker.importlib, 'import_module', side_effect=ImportError('动态库缺失'))):
        result = worker.check(['torch'], LOCK, compatible_gpu=True)
    assert not result['ready']
    assert '动态库缺失' in result['errors']['torch']


def test_gpu_onnx_does_not_reuse_cpu_version_number_as_an_approved_gpu_build():
    def installed(name):
        if name=='onnxruntime': raise worker.metadata.PackageNotFoundError(name)
        return '1.24.3'
    with patch.object(worker.metadata,'version',side_effect=installed):
        result=worker.inventory(['onnxruntime'],LOCK,compatible_gpu=True)
    assert result['reused']==[]


@pytest.mark.parametrize('signature_result,accepted', [(0,True),(1,False)])
def test_defender_injection_requires_trusted_location_and_valid_signature(tmp_path,monkeypatch,signature_result,accepted):
    monkeypatch.setenv('ProgramData',str(tmp_path))
    monkeypatch.setenv('SystemRoot',str(tmp_path/'Windows'))
    monkeypatch.setenv('PSModulePath','外部模块目录')
    module=tmp_path/'Microsoft/Windows Defender/Platform/4.18.26080.4-0/MpOAV.dll'
    with patch.object(worker.subprocess,'run',return_value=SimpleNamespace(returncode=signature_result)) as verify:
        assert worker.defender_injected_module(module)==accepted
        assert verify.call_args.kwargs['env']['VOICE_SYSTEM_MODULE_PATH']==str(module)
        assert verify.call_args.kwargs['env']['PSModulePath']==str(tmp_path/'Windows/System32/WindowsPowerShell/v1.0/Modules')
        assert not worker.defender_injected_module(tmp_path/'external/MpOAV.dll')
        assert not worker.defender_injected_module(module.with_name('unknown.dll'))
        verify.assert_called_once()

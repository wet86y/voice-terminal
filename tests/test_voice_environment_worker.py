"""完整语音环境：本地依赖复用与已验证显卡构建兼容。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_spec = importlib.util.spec_from_file_location('voice_environment_worker', Path(__file__).parents[1] / 'scripts/voice-environment-worker.py')
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)

LOCK = {'wheels': [{'name': 'torch', 'version': '2.11.0+cpu'}, {'name': 'torchaudio', 'version': '2.11.0+cpu'},
                   {'name': 'onnxruntime', 'version': '1.24.3'}, {'name': 'funasr', 'version': '1.4.14'}]}


def test_worker_diagnostics_remain_utf8_when_system_stream_uses_gbk():
    result = subprocess.run(
        [sys.executable, '-B', str(Path(worker.__file__)), 'check', '--names', json.dumps(['缺失的测试依赖'])],
        env={**os.environ, 'PYTHONIOENCODING': 'gbk'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    assert result.returncode == 0
    record = json.loads(result.stdout.decode('utf-8'))
    assert record['ok']
    assert not record['result']['ready']
    assert '缺失的测试依赖' in record['result']['errors']


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


def test_optional_gpu_lock_changes_only_tensor_packages(tmp_path):
    """GPU overlay（显卡覆盖清单）不改变其他基础依赖或原始文件。"""
    base = tmp_path / 'base.json'
    overlay = tmp_path / 'gpu.json'
    base.write_text(json.dumps(LOCK), encoding='utf-8')
    gpu = {'schema': 1, 'profile': 'cu128', 'wheels': [
        {'name': name, 'version': '2.11.0+cu128'} for name in ('torch', 'torchaudio')]}
    overlay.write_text(json.dumps(gpu), encoding='utf-8')
    effective = worker.runtime_lock(base, overlay)
    assert [row['version'] for row in effective['wheels']] == ['2.11.0+cu128', '2.11.0+cu128', '1.24.3', '1.4.14']
    assert worker.runtime_lock(base) == LOCK
    gpu['wheels'][0]['version'] = '2.10.0+cu128'
    overlay.write_text(json.dumps(gpu), encoding='utf-8')
    with pytest.raises(ValueError, match='版本不匹配'):
        worker.runtime_lock(base, overlay)
    gpu['wheels'].append({'name': 'onnxruntime-gpu', 'version': '1.24.4'})
    overlay.write_text(json.dumps(gpu), encoding='utf-8')
    with pytest.raises(ValueError, match='包集合无效'):
        worker.runtime_lock(base, overlay)


def _package(site, name, version, files):
    """package（验证安装包）：生成含真实文件记录的最小依赖。"""
    folder = site / f'{name}-{version}.dist-info'
    folder.mkdir(parents=True)
    (folder / 'METADATA').write_text(f'Name: {name}\nVersion: {version}\n', encoding='utf-8')
    rows = [*files, f'{folder.name}/METADATA', f'{folder.name}/RECORD']
    for relative, content in files.items():
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (folder / 'RECORD').write_text('\n'.join(f'{path},,' for path in rows), encoding='utf-8')
    return folder


def test_gpu_upgrade_prunes_old_tensor_files_and_metadata_only_inside_candidate(tmp_path):
    site = tmp_path / 'candidate/Lib/site-packages'
    _package(site, 'torch', '2.11.0+cpu', {'torch/lib/cpu-old.dll': b'old', 'torch/__init__.py': b'old'})
    _package(site, 'torchaudio', '2.11.0+cpu', {'torchaudio/__init__.py': b'old'})
    _package(site, 'numpy', '2.0', {'numpy/__init__.py': b'keep'})
    outside = tmp_path / 'source-model.pt'
    outside.write_bytes(b'keep-model')
    record = site / 'torch-2.11.0+cpu.dist-info/RECORD'
    record.write_text(record.read_text() + '\n../../../source-model.pt,,', encoding='utf-8')
    result = worker.prune(site, ['torch', 'torchaudio'])
    assert result['removed_files'] == 7
    assert not (site / 'torch').exists()
    assert not list(site.glob('torch*.dist-info'))
    assert (site / 'numpy/__init__.py').read_bytes() == b'keep'
    assert outside.read_bytes() == b'keep-model'
    # 新版本解包后，元数据只报告显卡版本，旧 CPU（中央处理器）动态库不会残留。
    _package(site, 'torch', '2.11.0+cu128', {'torch/__init__.py': b'new', 'torch/lib/cuda.dll': b'new'})
    installed = list(worker.metadata.distributions(path=[str(site)]))
    assert [item.version for item in installed if item.metadata['Name'] == 'torch'] == ['2.11.0+cu128']


def test_prune_rejects_unrelated_packages(tmp_path):
    with pytest.raises(ValueError, match='仅允许'):
        worker.prune(tmp_path, ['numpy'])


def test_prune_checks_all_candidate_paths_before_deleting(tmp_path, monkeypatch):
    site = tmp_path / 'site'
    folder = _package(site, 'torch', '2.11.0+cpu', {'torch/__init__.py': b'old', 'torch/lib/linked.dll': b'old'})
    monkeypatch.setattr(worker, 'reparse', lambda path: path.name == 'linked.dll')
    with pytest.raises(ValueError, match='外部链接'):
        worker.prune(site, ['torch'])
    assert (site / 'torch/__init__.py').read_bytes() == b'old'
    assert folder.is_dir()


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

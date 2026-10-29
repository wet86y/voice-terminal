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


def test_product_host_exception_requires_actual_image_and_exact_manifest_hashes(tmp_path):
    import hashlib
    host=tmp_path/'app/VoiceTerminalDesktop.exe'
    host.parent.mkdir()
    host.write_bytes(b'product-host')
    crt=host.with_name('vcruntime140.dll')
    crt.write_bytes(b'product-crt')
    plugin=host.with_name('unregistered.dll')
    plugin.write_bytes(b'external')
    def row(path):
        return {'path':path.name,'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest=host.with_name('release.json')
    manifest.write_text(json.dumps({'role':'product','files':[row(host),row(crt),row(plugin)]}))
    assert worker.verified_product_host_files(host)=={host,crt}
    assert worker.verified_product_host_files(host.with_name('other.exe'))==set()
    crt.write_bytes(b'changed-crt')
    assert worker.verified_product_host_files(host)=={host}
    host.write_bytes(b'changed-host')
    assert worker.verified_product_host_files(host)==set()


def test_soundtouch_dependency_check_requires_a_loadable_dynamic_library():
    lock={'wheels':[{'name':'voice-audio-dsp','version':'0.1.0'}]}
    for available in (True,False):
        with (patch.object(worker.metadata,'version',return_value='0.1.0'),
              patch.object(worker.importlib,'import_module',return_value=SimpleNamespace(available=lambda:available)),
              patch.object(worker,'dynamic_library_closure')):
            result=worker.check(['voice-audio-dsp'],lock)
        assert result['ready']==available
        if not available: assert 'voice-audio-dsp' in result['errors']


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


def _delta_runtime(tmp_path, packages, *, schema=2):
    import hashlib
    root=tmp_path/'runtime';site=root/'Lib/site-packages'
    rows=[]
    for name,version,files in packages:
        _package(site,name,version,files)
        rows.append({'name':name,'version':version,'file':name+'.whl','size':1,'sha256':name+'-hash'})
    tracked=[{'path':path.relative_to(root).as_posix(),'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in root.rglob('*') if path.is_file()]
    (root/'manifest.json').write_text(json.dumps({'schema':1,'kind':'python','files':tracked}),encoding='utf-8')
    (root/'runtime.json').write_text(json.dumps({'schema':schema,'packages':[r['name'] for r in rows]}),encoding='utf-8')
    old={r['name']:worker.package_identity(r) for r in rows}
    return root,{'wheels':rows},{'main':old,'overlay':old}


@pytest.mark.parametrize('schema',[1,2])
def test_legacy_environment_adds_only_new_package_and_preserves_old_content(tmp_path,schema):
    root,lock,old=_delta_runtime(tmp_path,[('large','1',{'large/library.dll':b'keep'})],schema=schema)
    lock['wheels'].append({'name':'small','version':'1','file':'small.whl','size':2,'sha256':'new'})
    plan=worker.delta_plan(root,lock,['large','small'],legacy=old)
    assert plan['main']['install']==['small']
    assert plan['main']['reused']==['large']
    assert plan['main']['delete']==[]
    assert (root/'Lib/site-packages/large/library.dll').read_bytes()==b'keep'


def test_same_version_repacked_wheel_updates_only_that_package(tmp_path):
    root,lock,old=_delta_runtime(tmp_path,[('large','1',{'large/a.dll':b'keep'}),('small','1',{'small/obsolete.py':b'old'})])
    lock['wheels'][1]['sha256']='repacked'
    plan=worker.delta_plan(root,lock,['large','small'],legacy=old)['main']
    assert plan['install']==['small']
    assert plan['reused']==['large']
    assert 'Lib/site-packages/small/obsolete.py' in plan['delete']
    assert not any('large/' in path for path in plan['delete'])


def test_package_uninstall_preserves_shared_namespace_and_foreign_package(tmp_path):
    root,lock,old=_delta_runtime(tmp_path,[('retire','1',{'shared/a.py':b'shared','retire/old.py':b'old'}),('keep','1',{'shared/a.py':b'shared'}),('foreign','1',{'foreign/a.py':b'keep'})])
    record=json.loads((root/'runtime.json').read_text());record['packages']=['retire','keep'];(root/'runtime.json').write_text(json.dumps(record))
    lock['wheels']=lock['wheels'][1:2]
    plan=worker.delta_plan(root,lock,['keep'],legacy=old)['main']
    assert plan['removed']==['retire']
    assert 'Lib/site-packages/retire/old.py' in plan['delete']
    assert 'Lib/site-packages/shared/a.py' not in plan['delete']
    assert not any('foreign' in path for path in plan['delete'])


def test_unknown_legacy_content_identity_stops_without_full_reinstallation(tmp_path):
    root,lock,_=_delta_runtime(tmp_path,[('large','1',{'large/a.dll':b'keep'})])
    with pytest.raises(ValueError,match='内容身份'):
        worker.delta_plan(root,lock,['large'])
    assert (root/'Lib/site-packages/large/a.dll').read_bytes()==b'keep'


def test_package_receipts_allow_update_without_historical_lock(tmp_path):
    root,lock,old=_delta_runtime(tmp_path,[('large','1',{'large/a.dll':b'keep'})])
    record=json.loads((root/'runtime.json').read_text());record['package_receipts']=old['main'];(root/'runtime.json').write_text(json.dumps(record))
    assert worker.delta_plan(root,lock,['large'])['main']['install']==[]


def test_legacy_identity_is_bound_to_exact_previous_lock_digest(tmp_path):
    import hashlib
    root,lock,old=_delta_runtime(tmp_path,[('large','1',{'large/a.dll':b'keep'})])
    lock_path=tmp_path/'voice-runtime.lock.json';lock_path.write_text(json.dumps(lock))
    record=json.loads((root/'runtime.json').read_text());record['lock_sha256']='previous';(root/'runtime.json').write_text(json.dumps(record))
    lock_path.with_name('voice-runtime-history.json').write_text(json.dumps({'schema':1,'locks':{'previous':old['main']}}))
    assert worker.legacy_identities(root,lock_path)['main']==old['main']
    record['lock_sha256']='unknown';(root/'runtime.json').write_text(json.dumps(record))
    assert worker.legacy_identities(root,lock_path)['main']=={}


def test_unrelated_dependency_does_not_reinstall_cpu_overlay(tmp_path):
    root,lock,old=_delta_runtime(tmp_path,[('small','1',{'small/a.py':b'keep'})])
    overlay=root/'Lib/cpu-packages';_package(overlay,'torch','1',{'torch/__init__.py':b'cpu'});_package(overlay,'torchaudio','1',{'torchaudio/__init__.py':b'cpu'})
    lock['cpu_wheels']=[{'name':n,'version':'1','file':n+'.whl','size':1,'sha256':n} for n in ['torch','torchaudio']]
    old['overlay']={r['name']:worker.package_identity(r) for r in lock['cpu_wheels']}
    plan=worker.delta_plan(root,lock,['small'],legacy=old,want_overlay=True)
    assert plan['overlay']['install']==[]
    assert set(plan['overlay']['reused'])=={'torch','torchaudio'}


def test_missing_old_distribution_blocks_replacement_and_uninstall(tmp_path):
    root,lock,old=_delta_runtime(tmp_path,[('retire','1',{'retire/old.py':b'old'})])
    import shutil
    shutil.rmtree(root/'Lib/site-packages/retire-1.dist-info')
    with pytest.raises(ValueError,match='缺少发行元数据'):
        worker.delta_plan(root,{'wheels':[]},[],legacy=old)
    assert (root/'Lib/site-packages/retire/old.py').is_file()


@pytest.mark.parametrize('schema',[1,2])
def test_legacy_gpu_preservation_records_actual_installed_identity_separately(tmp_path,schema):
    root,lock,_=_delta_runtime(tmp_path,[('torch','1+cu128',{'torch/__init__.py':b'gpu'})],schema=schema)
    lock['wheels'][0]['version']='1+cpu'
    old={'main':{'torch':worker.package_identity(lock['wheels'][0])}}
    plan=worker.delta_plan(root,lock,['torch'],compatible_gpu=True,legacy=old)['main']
    assert plan['install']==[]
    receipt=plan['receipts']['torch']
    assert receipt['installed_version']=='1+cu128'
    assert receipt['provenance']=='verified-legacy-installed-files'
    assert len(receipt['installed_files_sha256'])==64
    assert receipt!=worker.package_identity(lock['wheels'][0])
    record=json.loads((root/'runtime.json').read_text());record['package_receipts']=plan['receipts'];(root/'runtime.json').write_text(json.dumps(record))
    assert worker.delta_plan(root,lock,['torch'],compatible_gpu=True)['main']['install']==[]


def test_legacy_gpu_without_record_cannot_acquire_empty_content_identity(tmp_path):
    root,lock,_=_delta_runtime(tmp_path,[('torch','1+cu128',{'torch/__init__.py':b'gpu'})])
    lock['wheels'][0]['version']='1+cpu';old={'main':{'torch':worker.package_identity(lock['wheels'][0])}}
    (root/'Lib/site-packages/torch-1+cu128.dist-info/RECORD').unlink()
    with pytest.raises(ValueError,match='完整安装记录'):
        worker.delta_plan(root,lock,['torch'],compatible_gpu=True,legacy=old)


def test_uninstall_also_removes_only_derived_bytecode_of_retired_sources(tmp_path):
    root,lock,old=_delta_runtime(tmp_path,[('retire','1',{'retire/old.py':b'old'})])
    cache=root/'Lib/site-packages/retire/__pycache__/old.cpython-312.pyc';cache.parent.mkdir();cache.write_bytes(b'cache')
    deletes=worker.delta_plan(root,{'wheels':[]},[],legacy=old)['main']['delete']
    assert cache.relative_to(root).as_posix() in deletes


def test_upgrade_removes_tracked_metadata_added_by_legacy_installer(tmp_path):
    import hashlib
    root,lock,old=_delta_runtime(tmp_path,[('retire','1',{'retire/old.py':b'old'})])
    extra=root/'Lib/site-packages/retire-1.dist-info/INSTALLER';extra.write_bytes(b'legacy')
    manifest=json.loads((root/'manifest.json').read_text());manifest['files'].append({'path':extra.relative_to(root).as_posix(),'size':6,'sha256':hashlib.sha256(b'legacy').hexdigest()});(root/'manifest.json').write_text(json.dumps(manifest))
    plan=worker.delta_plan(root,{'wheels':[]},[],legacy=old)['main']
    assert extra.relative_to(root).as_posix() in plan['delete']


def test_uninstall_uses_the_same_wheel_data_mapping_as_install(tmp_path):
    root,_,old=_delta_runtime(tmp_path,[('retire','1',{'shared.py':b'old'})])
    record=root/'Lib/site-packages/retire-1.dist-info/RECORD'
    record.write_text(record.read_text().replace('shared.py,,','retire-1.data/purelib/shared.py,,'))
    plan=worker.delta_plan(root,{'wheels':[]},[],legacy=old)['main']
    assert 'Lib/site-packages/shared.py' in plan['delete']


@pytest.mark.parametrize('gpu',[False,True])
def test_original_offline_schema_without_lock_digest_or_package_names(tmp_path,gpu):
    actual='1+cu128' if gpu else '1+cpu'
    root,lock,_=_delta_runtime(tmp_path,[('torch',actual,{'torch/__init__.py':b'original'})],schema=1)
    artifact={'file':f'torch-{actual}-cp312-cp312-win_amd64.whl','size':1,'sha256':'a'*64}
    lock['wheels'][0].update(artifact);lock['wheels'][0]['version']='1+cpu'
    if gpu: lock['wheels'][0]['file']='torch-1+cpu-cp312-cp312-win_amd64.whl';lock['wheels'][0]['sha256']='b'*64
    (root/'runtime.json').write_text(json.dumps({'schema':1,'profile':'cu128' if gpu else 'cpu','wheels':[artifact]}))
    path=tmp_path/'voice-runtime.lock.json';path.write_text(json.dumps(lock))
    legacy=worker.legacy_identities(root,path)
    plan=worker.delta_plan(root,lock,['torch'],compatible_gpu=gpu,legacy=legacy)['main']
    assert plan['install']==[]
    assert plan['removed']==[]
    assert plan['reused']==['torch']


def test_local_repair_rebuilds_only_package_with_corrupt_content(tmp_path):
    root,lock,old=_delta_runtime(tmp_path,[('large','1',{'large/lib.dll':b'keep'}),('small','1',{'small/a.py':b'good'})])
    (root/'Lib/site-packages/small/a.py').write_bytes(b'bad!')
    plan=worker.delta_plan(root,lock,['large','small'],legacy=old,repair_local=True)['main']
    assert plan['install']==['small']
    assert plan['reused']==['large']
    assert not any('large/' in path for path in plan['delete'])


def test_local_repair_refuses_unowned_interpreter_damage(tmp_path):
    import hashlib
    root,lock,old=_delta_runtime(tmp_path,[('large','1',{'large/a.py':b'keep'})])
    manifest=json.loads((root/'manifest.json').read_text());manifest['files'].append({'path':'python.exe','size':4,'sha256':hashlib.sha256(b'good').hexdigest()});(root/'manifest.json').write_text(json.dumps(manifest))
    (root/'python.exe').write_bytes(b'bad!')
    with pytest.raises(ValueError,match='结构性文件损坏'):
        worker.delta_plan(root,lock,['large'],legacy=old,repair_local=True)


def test_local_repair_never_converts_unregistered_gpu_to_cpu(tmp_path):
    root,lock,old=_delta_runtime(tmp_path,[('torch','1+cu128',{'torch/__init__.py':b'gpu'})])
    lock['wheels'][0]['version']='1+cpu'
    with pytest.raises(ValueError,match='不能以处理器依赖覆盖'):
        worker.delta_plan(root,lock,['torch'],legacy=old,repair_local=True)


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

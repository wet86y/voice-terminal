"""发行验收：封包不泄漏数据，构建刷新失败不改变实际启用组合。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from voice_terminal.distribution import digest, manifest
from voice_terminal.paths import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    specification = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def candidate(root):
    app = root / 'apps/current'
    app.mkdir(parents=True)
    (app / 'business.py').write_text('旧程序', encoding='utf-8')
    (root / 'VoiceTerminal.exe').write_bytes(b'old-launcher')
    (root / 'Verify-Package.ps1').write_bytes(b'old-verifier')
    release = manifest(app, kind='app', build='current')
    release.update(components=[], root_files=[{'path':name,'size':(root/name).stat().st_size,'sha256':digest(root/name)}
                   for name in ('VoiceTerminal.exe','Verify-Package.ps1')])
    atomic_json(app / 'release.json', release)
    selection = {'schema':1,'app':'apps/current','external_python':None,'external_models':None}
    atomic_json(root / 'portable.json', {'data_dir':'data', **selection})
    atomic_json(root / 'data/state/active.json', selection)
    (root / 'data/private.txt').write_text('用户内容不得进入包', encoding='utf-8')
    return selection


@pytest.mark.parametrize('private_in_active', (False, True))
def test_package_only_publishes_declared_files_and_verified_default(tmp_path, monkeypatch, private_in_active):
    root = tmp_path / '应用'
    selected = candidate(root)
    atomic_json(root / 'portable.json', {'data_dir':'data', **selected,'app':'apps/missing','private':'不能发布'})
    if private_in_active:
        atomic_json(root / 'data/state/active.json', {**selected,'private':'不能发布'})
    (root / '.env.local').write_text('PRIVATE=不能发布', encoding='utf-8')
    module = script('package-desktop')
    archive, components = tmp_path / '完整包.zip', tmp_path / '组件'
    monkeypatch.setattr(sys, 'argv', ['package-desktop', '--root', str(root), '--output',str(archive),'--components',str(components)])
    module.main()
    with zipfile.ZipFile(archive) as content:
        assert set(content.namelist()) == {'apps/current/business.py','apps/current/release.json','VoiceTerminal.exe','Verify-Package.ps1','portable.json'}
        assert json.loads(content.read('portable.json')) == {'data_dir':'data', **selected}
        assert b'PRIVATE' not in b''.join(content.read(name) for name in content.namelist())
    assert (root / '.env.local').is_file() and (root / 'data/private.txt').is_file()


@pytest.mark.skipif(os.name != 'nt', reason='Windows（操作系统）目录联接验证')
def test_archive_rejects_ancestor_junction_before_portable_override(tmp_path):
    root = tmp_path / '包'
    data = root / 'data'
    data.mkdir(parents=True)
    atomic_json(data / 'private.json', {'schema':1,'private':'不能发布'})
    link = root / 'alias'
    subprocess.run(['powershell.exe','-NoProfile','-Command',
        'New-Item -ItemType Junction -Path $env:VOICE_TEST_LINK -Value $env:VOICE_TEST_TARGET -ErrorAction Stop | Out-Null'],
        env={**os.environ,'VOICE_TEST_LINK':str(link),'VOICE_TEST_TARGET':str(data)},check=True,capture_output=True)
    try:
        module = script('assemble-desktop')
        with pytest.raises(ValueError, match='链接|重解析'):
            module.archive(root,tmp_path/'unsafe.zip',include_roots=('alias/private.json',),overrides={'alias/private.json':b'{}'})
        assert not (tmp_path/'unsafe.zip').exists()
    finally:
        link.rmdir()  # 仅移除本测试创建的联接，保留数据目标。


def test_refresh_rolls_back_failed_selection_and_can_retry_same_build(tmp_path, monkeypatch):
    root, desktop = tmp_path / '包', tmp_path / '构建'
    candidate(root)
    desktop.mkdir()
    (desktop / 'VoiceTerminal.exe').write_bytes(b'new-launcher')
    module = script('assemble-desktop')
    def program(target, _desktop):
        target.mkdir(parents=True)
        (target / 'business.py').write_text('新程序', encoding='utf-8')
    monkeypatch.setattr(module,'collect_program',program)
    monkeypatch.setattr(module,'require_stopped',lambda *_:None)
    originals = {name:(root/name).read_bytes() for name in ('VoiceTerminal.exe','Verify-Package.ps1','portable.json','data/state/active.json','data/private.txt')}
    writer = module.atomic_json
    def fail_once(path,value):
        if path == root / 'portable.json':
            raise OSError('模拟磁盘提交失败')
        return writer(path,value)
    monkeypatch.setattr(module,'atomic_json',fail_once)
    with pytest.raises(OSError, match='模拟'):
        module.refresh_program(root,desktop,'next')
    assert not (root / 'apps/next').exists()
    assert all((root/name).read_bytes() == content for name,content in originals.items())
    monkeypatch.setattr(module,'atomic_json',writer)
    module.refresh_program(root,desktop,'next')
    assert json.loads((root/'data/state/active.json').read_text(encoding='utf-8'))['app'] == 'apps/next'
    assert (root/'data/private.txt').read_bytes() == originals['data/private.txt']


def test_refresh_validation_failure_leaves_original_selection(tmp_path, monkeypatch):
    root, desktop = tmp_path / '包', tmp_path / '构建'
    candidate(root)
    desktop.mkdir(); (desktop/'VoiceTerminal.exe').write_bytes(b'new-launcher')
    module = script('assemble-desktop')
    def failed_copy(target,_desktop):
        target.mkdir(parents=True); (target/'partial').write_text('部分副本')
        raise OSError('模拟源码收集失败')
    monkeypatch.setattr(module,'collect_program',failed_copy)
    monkeypatch.setattr(module,'require_stopped',lambda *_:None)
    with pytest.raises(OSError, match='模拟'):
        module.refresh_program(root,desktop,'next')
    assert not (root/'apps/next').exists()
    assert (root/'VoiceTerminal.exe').read_bytes() == b'old-launcher'
    assert json.loads((root/'data/state/active.json').read_text(encoding='utf-8'))['app'] == 'apps/current'

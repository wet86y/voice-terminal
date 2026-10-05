"""发行验收：封包不泄漏数据，构建刷新失败不改变实际启用组合。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from voice_terminal.distribution import digest, manifest, verify_install
from voice_terminal.paths import atomic_json

ROOT = Path(__file__).resolve().parents[1]
DSH_VERSION = '0.2.0-rc.2'  # DSH（代理宿主）版本。
DSH_COMMIT = '639ed015397290b3745d163aafe02ffee4aa3f84'  # commit（源码提交号）。


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


def dsh_runtime(path, *, build='639ed015-bundle', version=DSH_VERSION, commit=DSH_COMMIT, extra=None):
    """生成测试用 DSH（代理宿主）运行目录、版本记录和逐文件清单。"""
    path.mkdir(parents=True)
    atomic_json(path / 'runtime.json', {
        'schema':1, 'version':version, 'commit':commit, 'bridge_protocol':'2.0',
        'node':'node.exe', 'entry':'runtime-bootstrap.mjs', 'schedule':'schedule.mjs',
    })
    (path / 'node.exe').write_bytes(b'node runtime')
    (path / 'runtime-bootstrap.mjs').write_text('bootstrap', encoding='utf-8')
    (path / 'schedule.mjs').write_text('schedule', encoding='utf-8')
    if extra is not None:
        (path / 'extra.bin').write_bytes(extra)
    atomic_json(path / 'manifest.json', manifest(path, kind='dsh', build=build))
    return path


def collect_locked_program(module, *, version=DSH_VERSION, commit=DSH_COMMIT):
    """替换程序收集器，并写入 runtime lock（运行时锁版）。"""
    def collect(target, _desktop):
        target.mkdir(parents=True)
        (target / 'business.py').write_text('新程序', encoding='utf-8')
        atomic_json(target / 'config/dsh/runtime.lock.json', {
            'schema_version':1, 'version':version, 'commit':commit,
        })
    module.collect_program = collect


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
    runtime = dsh_runtime(tmp_path / 'DSH')
    module = script('assemble-desktop')
    collect_locked_program(module)
    monkeypatch.setattr(module,'require_stopped',lambda *_:None)
    originals = {name:(root/name).read_bytes() for name in ('VoiceTerminal.exe','Verify-Package.ps1','portable.json','data/state/active.json','data/private.txt')}
    writer = module.atomic_json
    def fail_once(path,value):
        if path == root / 'portable.json':
            raise OSError('模拟磁盘提交失败')
        return writer(path,value)
    monkeypatch.setattr(module,'atomic_json',fail_once)
    with pytest.raises(OSError, match='模拟'):
        module.refresh_program(root,desktop,'next',runtime)
    assert not (root / 'apps/next').exists()
    assert not (root / 'runtimes/dsh/639ed015-bundle').exists()
    assert all((root/name).read_bytes() == content for name,content in originals.items())
    monkeypatch.setattr(module,'atomic_json',writer)
    module.refresh_program(root,desktop,'next',runtime)
    assert json.loads((root/'data/state/active.json').read_text(encoding='utf-8'))['app'] == 'apps/next'
    assert json.loads((root/'data/state/active.json').read_text(encoding='utf-8'))['dsh'] == 'runtimes/dsh/639ed015-bundle'
    assert (root/'data/private.txt').read_bytes() == originals['data/private.txt']
    verify_install(root)


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


def test_refresh_replaces_dsh_and_verify_install_accepts_new_selection(tmp_path, monkeypatch):
    """切换 DSH（代理宿主）组件后，新程序清单与启用路径必须一致。"""
    root, desktop = tmp_path / '包', tmp_path / '构建'
    candidate(root)
    desktop.mkdir()
    (desktop / 'VoiceTerminal.exe').write_bytes(b'new-launcher')
    runtime = dsh_runtime(tmp_path / 'DSH')
    module = script('assemble-desktop')
    collect_locked_program(module)
    monkeypatch.setattr(module, 'require_stopped', lambda *_:None)

    module.refresh_program(root, desktop, 'next', runtime)

    expected = 'runtimes/dsh/639ed015-bundle'
    active = json.loads((root / 'data/state/active.json').read_text(encoding='utf-8'))
    portable = json.loads((root / 'portable.json').read_text(encoding='utf-8'))
    release = json.loads((root / 'apps/next/release.json').read_text(encoding='utf-8'))
    component = next(item for item in release['components'] if item['kind'] == 'dsh')
    assert active['dsh'] == portable['dsh'] == expected
    assert component == {
        'kind':'dsh', 'path':expected,
        'manifest_sha256':digest(root / expected / 'manifest.json'),
        'build':'639ed015-bundle',
    }
    assert (root / 'apps/current/business.py').read_text(encoding='utf-8') == '旧程序'
    assert (root / 'runtimes/dsh/639ed015-bundle/runtime.json').is_file()
    verify_install(root)


@pytest.mark.parametrize('invalid', ('manifest', 'version'))
def test_refresh_rejects_bad_dsh_manifest_or_version_without_selection_changes(tmp_path, monkeypatch, invalid):
    """坏 manifest（文件清单）或 runtime version（宿主版本）不得改变启用选择。"""
    root, desktop = tmp_path / '包', tmp_path / '构建'
    candidate(root)
    desktop.mkdir()
    (desktop / 'VoiceTerminal.exe').write_bytes(b'new-launcher')
    kwargs = {'version': '0.1.7-rc.2'} if invalid == 'version' else {}
    runtime = dsh_runtime(tmp_path / 'DSH', **kwargs)
    if invalid == 'manifest':
        record = json.loads((runtime / 'manifest.json').read_text(encoding='utf-8'))
        record['kind'] = 'python'
        atomic_json(runtime / 'manifest.json', record)
    module = script('assemble-desktop')
    collect_locked_program(module)
    monkeypatch.setattr(module, 'require_stopped', lambda *_:None)
    originals = {name:(root / name).read_bytes() for name in (
        'VoiceTerminal.exe', 'Verify-Package.ps1', 'portable.json',
        'data/state/active.json', 'data/private.txt',
    )}

    with pytest.raises(ValueError):
        module.refresh_program(root, desktop, 'next', runtime)

    assert all((root / name).read_bytes() == content for name, content in originals.items())
    assert not (root / 'apps/next').exists()
    assert not (root / 'runtimes/dsh').exists()
    assert json.loads((root / 'data/state/active.json').read_text(encoding='utf-8'))['app'] == 'apps/current'


def test_refresh_reuses_matching_dsh_build_and_rejects_same_build_mismatch(tmp_path, monkeypatch):
    """同 build（构建标识）的 DSH 组件仅在完整清单一致时复用。"""
    root, desktop = tmp_path / '包', tmp_path / '构建'
    candidate(root)
    desktop.mkdir()
    (desktop / 'VoiceTerminal.exe').write_bytes(b'new-launcher')
    runtime = dsh_runtime(tmp_path / 'DSH', extra=b'original bundle')
    different = dsh_runtime(tmp_path / 'DSH-different', extra=b'different bundle')
    module = script('assemble-desktop')
    collect_locked_program(module)
    monkeypatch.setattr(module, 'require_stopped', lambda *_:None)
    copier = module.copy_component
    copies = []
    def count_copies(*args, **kwargs):
        copies.append(args[1])
        return copier(*args, **kwargs)
    monkeypatch.setattr(module, 'copy_component', count_copies)

    module.refresh_program(root, desktop, 'first', runtime)
    installed = root / 'runtimes/dsh/639ed015-bundle'
    original_files = {path.relative_to(installed).as_posix():path.read_bytes() for path in installed.rglob('*') if path.is_file()}
    module.refresh_program(root, desktop, 'second', runtime)
    assert len(copies) == 1
    assert {path.relative_to(installed).as_posix():path.read_bytes() for path in installed.rglob('*') if path.is_file()} == original_files
    assert json.loads((root / 'data/state/active.json').read_text(encoding='utf-8'))['app'] == 'apps/second'
    verify_install(root)

    with pytest.raises(ValueError, match='不一致'):
        module.refresh_program(root, desktop, 'third', different)
    assert json.loads((root / 'data/state/active.json').read_text(encoding='utf-8'))['app'] == 'apps/second'
    assert not (root / 'apps/third').exists()
    assert {path.relative_to(installed).as_posix():path.read_bytes() for path in installed.rglob('*') if path.is_file()} == original_files
    verify_install(root)


def test_main_forwards_dsh_runtime_during_program_refresh(tmp_path, monkeypatch):
    """main（命令行入口）必须把 --dsh-runtime 参数传给刷新流程。"""
    root, desktop, runtime = tmp_path / '包', tmp_path / '构建', tmp_path / 'DSH'
    candidate(root)
    desktop.mkdir()
    (desktop / 'VoiceTerminal.exe').write_bytes(b'launcher')
    (desktop / 'VoiceTerminalDesktop.exe').write_bytes(b'desktop')
    module = script('assemble-desktop')
    received = []
    monkeypatch.setattr(module, 'refresh_program', lambda *args:received.append(args))
    monkeypatch.setattr(sys, 'argv', [
        'assemble-desktop', '--output', str(root), '--desktop', str(desktop),
        '--build', 'next', '--refresh-program', '--dsh-runtime', str(runtime),
    ])

    module.main()

    assert received == [(root.resolve(), desktop.resolve(), 'next', runtime)]

"""DSH npm（包安装）兼容、升级与完整性回归。"""
import json
from pathlib import Path
import importlib.util
import pytest
from voice_terminal.dsh import distribution as adapter
from voice_terminal.distribution import manifest, digest
from voice_terminal.paths import atomic_json
from voice_terminal.local_layout import default_portable
from voice_terminal.resource_distribution import ResourceManager

ROOT = Path(__file__).resolve().parents[1]

def test_shared_native_backend_and_installer_compatibility_contract():
    base = adapter.target_spec(ROOT)
    cases = adapter.read_json(ROOT / 'tests/fixtures/dsh-adapter-cases.json')['cases']
    for case in cases:
        assert adapter.is_current({**base, **case['installed']}, {**base, **case['target']}) == case['matches'], case['name']

def test_npm_configuration_has_no_source_commit_start_requirement():
    from voice_terminal.config import DshConfig
    assert DshConfig(expected_commit='').expected_commit == ''

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    program = tmp_path / 'app'
    lock = json.loads((ROOT / 'config/dsh/runtime.lock.json').read_text('utf-8'))
    atomic_json(program / 'config/dsh/runtime.lock.json', lock)
    spec = adapter.target_spec(program)
    atomic_json(program / spec['plugin_record'], {'dsh_version': spec['version'], 'adapter': spec['adapter'], 'bridge_protocol': spec['bridge_protocol']})
    resource = next(row for row in json.loads((ROOT / 'config/resources/catalog.json').read_text('utf-8'))['resources'] if row['kind'] == 'dsh')
    atomic_json(program / 'config/resources/catalog.json', {'schema_version': 1, 'resources': [resource]})
    atomic_json(tmp_path / 'portable.json', default_portable())
    atomic_json(program / 'release.json', {'schema': 1, 'kind': 'app', 'files': [], 'components': []})
    root = tmp_path / 'runtimes/dsh'
    metadata = {'schema': 3, 'kind': 'dsh', **spec, 'pnpm_entry': 'node_modules/pnpm/bin/pnpm.mjs'}
    for rel in {spec['entry'], spec['cli_entry'], spec['schedule'], metadata['pnpm_entry'], *spec['required_files']}:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('content', 'utf-8')
    atomic_json(root / 'package.json', {'private': True})
    atomic_json(root / f"{spec['modules']}/{spec['package_name']}/package.json", {'name': spec['package_name'], 'version': spec['version']})
    atomic_json(root / f"{spec['modules']}/pnpm/package.json", {'name': 'pnpm', 'version': spec['package_manager'].split('@')[1]})
    atomic_json(root / 'runtime.json', metadata)
    atomic_json(root / 'manifest.json', manifest(root, kind='dsh', build='installed'))
    atomic_json(program / 'release.json', {'schema': 1, 'kind': 'app', 'files': [], 'components': [
        {'kind': 'dsh', 'path': 'runtimes/dsh', 'build': 'installed', 'manifest_sha256': digest(root / 'manifest.json')}]})
    monkeypatch.setattr(adapter, 'discover_node', lambda *a, **k: (Path('node.exe'), None))
    return program, root, spec

def test_new_target_updates_existing_component_in_both_directions(runtime):
    program, root, spec = runtime
    manager = ResourceManager(program, root.parents[1])
    assert manager._dsh_state(manager.catalog.get('dsh'))['can_start']
    for version in ('0.1.0', '1.0.0'):
        lock = adapter.read_json(program / 'config/dsh/runtime.lock.json')
        lock['version'] = version
        atomic_json(program / 'config/dsh/runtime.lock.json', lock)
        manager = ResourceManager(program, root.parents[1])
        status = manager._dsh_state(manager.catalog.get('dsh'))
        assert status['installed_version'] == spec['version']
        assert status['version'] == version
        assert status['update_available'] and not status['can_start']

def test_plugin_mismatch_corruption_and_missing_entry_block_start(runtime):
    program, root, spec = runtime
    assert adapter.inspect_runtime(program, root, full=True)['version'] == spec['version']
    path = root / spec['entry']
    path.write_text('changed', 'utf-8')
    with pytest.raises(ValueError): adapter.inspect_runtime(program, root, full=True)
    path.unlink()
    with pytest.raises(ValueError): adapter.inspect_runtime(program, root)

def test_npm_download_plan_does_not_use_desktop_installer(runtime):
    program, root, spec = runtime
    plan = ResourceManager(program, root.parents[1]).download_plan('dsh', 'download-1')
    assert plan['transport'] == 'npm_package'
    assert plan['version'] == spec['version']
    assert plan['files'] == []


def test_legacy_status_is_fast_but_import_keeps_full_hash_validation(runtime, monkeypatch):
    from voice_terminal import resource_distribution as resources
    program, root, spec = runtime
    catalog = adapter.read_json(program / 'config/resources/catalog.json')
    resource = catalog['resources'][0]
    compatibility = resource['source']['official_compatibility']
    atomic_json(root / 'runtime.json', {'version': compatibility['version'], 'commit': compatibility['commit']})
    atomic_json(root / 'manifest.json', manifest(root, kind='dsh', build='legacy'))
    resource['legacy_export']['manifest_sha256'] = digest(root / 'manifest.json')
    atomic_json(program / 'config/resources/catalog.json', catalog)
    manager = ResourceManager(program, root.parents[1])
    original_hash = resources._hash_file
    hashed = []
    def track_hash(path):
        hashed.append(path)
        return original_hash(path)
    monkeypatch.setattr(resources, '_hash_file', track_hash)
    status = manager._dsh_state(manager.catalog.get('dsh'))
    assert status['state'] == 'installed' and status['update_available'] and not status['can_start']
    assert hashed and all(path.name == 'manifest.json' for path in hashed)
    assert not (manager.root / 'tmp/legacy-inspect').exists()
    (root / spec['entry']).write_text('corrupt', 'utf-8')  # 同长度损坏只能由完整摘要核验发现。
    with pytest.raises(resources.ResourceError, match='摘要'):
        manager._validate_dsh_tree(root, manager.root / 'tmp/import', create_candidate=False)
    (root / 'untracked.txt').write_text('extra', 'utf-8')
    assert manager._dsh_state(manager.catalog.get('dsh'))['state'] == 'incomplete'

@pytest.mark.parametrize('version,allowed', [('22.18.0', False), ('22.19.0', True), ('22.20.1', True), ('23.0.0', False), ('24.0.0', True), ('24.9.1', True), ('26.0.0', True)])
def test_node_requirements_come_from_configuration(version, allowed):
    assert adapter.node_version_matches(version, '^22.19.0 || >=24.0.0') == allowed

def test_npm_export_import_preserves_files_and_user_data(runtime, tmp_path):
    program, root, spec = runtime
    module_spec = importlib.util.spec_from_file_location('resource_export', ROOT / 'scripts/package-resources.py')
    exporter = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(exporter)
    manager = ResourceManager(program, root.parents[1])
    output = tmp_path / 'resources.zip'
    exporter._export(manager, output, ['dsh'])
    target = tmp_path / 'other'
    atomic_json(target / 'portable.json', default_portable())
    private = target / 'data/private.txt'
    private.parent.mkdir(parents=True)
    private.write_text('保留用户数据', 'utf-8')
    atomic_json(target / 'app/release.json', {'schema': 1, 'kind': 'app', 'files': [], 'components': []})
    incoming = ResourceManager(program, target)
    result = incoming.import_source(output, resource_id=None, task_id='import-1')
    assert result['installed_resource_ids'] == ['dsh']
    assert private.read_text('utf-8') == '保留用户数据'
    assert adapter.inspect_runtime(program, target / 'runtimes/dsh', full=True)['version'] == spec['version']


def test_installed_binding_and_extra_files_cannot_bypass_integrity(runtime):
    program, root, spec = runtime
    atomic_json(program / 'release.json', {'components': []})
    assert ResourceManager(program, root.parents[1])._dsh_state(ResourceManager(program, root.parents[1]).catalog.get('dsh'))['state'] == 'incomplete'
    (root / 'extra.txt').write_text('extra', 'utf-8')
    with pytest.raises(ValueError, match='文件集合'):
        adapter.inspect_runtime(program, root)


def test_startup_inventory_reuses_safe_tree_paths_and_still_rejects_damage(runtime, monkeypatch):
    program, root, spec = runtime
    for index in range(100):
        path = root / f'node_modules/sample/nested/{index}.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('complete', 'utf-8')
    atomic_json(root / 'manifest.json', manifest(root, kind='dsh', build='inventory-test'))
    original = adapter.safe_path
    calls = []
    def checked(base, relative):
        calls.append(relative)
        return original(base, relative)
    monkeypatch.setattr(adapter, 'safe_path', checked)
    adapter.inspect_runtime(program, root)
    assert len(calls) < 30  # 只解析必要入口和目标声明，不为每个依赖重复解析父目录。
    (root / 'node_modules/sample/nested/99.txt').write_text('broken-size', 'utf-8')
    with pytest.raises(ValueError, match='大小'):
        adapter.inspect_runtime(program, root)


def test_tool_requirement_change_requires_update_and_data_stays_in_place(runtime):
    program, root, spec = runtime
    lock = adapter.read_json(program / 'config/dsh/runtime.lock.json')
    lock['package_manager'] = 'pnpm@12.0.0'
    atomic_json(program / 'config/dsh/runtime.lock.json', lock)
    manager = ResourceManager(program, root.parents[1])
    status = manager._dsh_state(manager.catalog.get('dsh'))
    assert status['update_available'] and not status['can_start']
    assert adapter.read_json(root / 'runtime.json')['package_manager'] == spec['package_manager']


@pytest.mark.parametrize('identity,npm_ready,allowed', [
    ({'version': '22.18.0', 'platform': 'win32', 'arch': 'x64'}, True, False),
    ({'version': '24.0.0', 'platform': 'win32', 'arch': 'arm64'}, True, False),
    ({'version': '24.0.0', 'platform': 'win32', 'arch': 'x64'}, False, False),
    ({'version': '24.0.0', 'platform': 'win32', 'arch': 'x64'}, True, True)])
def test_actual_node_and_npm_probe(identity, npm_ready, allowed, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import subprocess
    node = tmp_path / 'node.exe'
    node.write_bytes(b'probe')
    npm = tmp_path / 'node_modules/npm/bin/npm-cli.js'
    if npm_ready:
        npm.parent.mkdir(parents=True)
        npm.write_bytes(b'probe')
    monkeypatch.setattr(adapter.shutil, 'which', lambda _: str(node))
    monkeypatch.setattr(adapter.os, 'get_exec_path', lambda: [str(tmp_path)])
    for key in ('ProgramFiles', 'LOCALAPPDATA'):
        monkeypatch.setenv(key, str(tmp_path / 'absent'))
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: SimpleNamespace(stdout=json.dumps(identity) if '-p' in args else '11.17.0'))
    spec = adapter.target_spec(ROOT)
    if allowed:
        assert adapter.discover_node(spec, require_npm=True)[0] == node.resolve()
    else:
        with pytest.raises(ValueError, match='Node.js'):
            adapter.discover_node(spec, require_npm=True)
    # 重新检测必须重新找文件，安装正确依赖后无需重启探测对象。
    npm.parent.mkdir(parents=True, exist_ok=True)
    npm.write_bytes(b'probe')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: SimpleNamespace(stdout=json.dumps({'version': '24.0.0', 'platform': 'win32', 'arch': 'x64'}) if '-p' in args else '11.17.0'))
    assert adapter.discover_node(spec, require_npm=True)[1] == npm

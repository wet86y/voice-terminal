"""发行验收：封包不泄漏数据，构建刷新失败不改变实际启用组合。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from voice_terminal.distribution import digest, manifest, verify, verify_install
from voice_terminal.paths import atomic_json

ROOT = Path(__file__).resolve().parents[1]
DSH_VERSION = '0.2.1-rc.2'  # DSH（代理宿主）版本。
DSH_COMMIT = '687f282e8625c6b8d11ff58211a90524be2e4317'  # commit（源码提交号）。


@pytest.fixture
def publisher(tmp_path, monkeypatch):
    from voice_terminal.local_layout import default_portable
    module = script('assemble-desktop')
    root, desktop = tmp_path / '本机', tmp_path / '原生构建'
    desktop.mkdir()
    for name in ('VoiceTerminal.exe', 'VoiceTerminalUIDemo.exe', 'VoiceTerminalDesktop.exe'):
        (desktop / name).write_bytes(b'new-native')
    for kind, relative in (('python', 'runtimes/python'), ('dsh', 'runtimes/dsh'), ('webview2', 'runtimes/webview2'), ('models', 'models')):
        component = root / relative
        component.mkdir(parents=True)
        (component / 'content.bin').write_bytes(kind.encode())
        if kind == 'dsh':
            atomic_json(component / 'runtime.json', {'schema': 1, 'version': DSH_VERSION, 'commit': DSH_COMMIT, 'node': 'node.exe'})
        atomic_json(component / 'manifest.json', manifest(component, kind=kind, build='current'))
    atomic_json(root / 'portable.json', default_portable())
    (root / 'data').mkdir()
    (root / 'data/private.txt').write_text('真实用户数据', encoding='utf-8')
    def collect(program, _desktop):
        program.mkdir(parents=True)
        (program / 'business.py').write_text('current-business', encoding='utf-8')
        atomic_json(program / 'config/dsh/runtime.lock.json', {'version': DSH_VERSION, 'commit': DSH_COMMIT})
    monkeypatch.setattr(module, 'collect_program', collect)
    monkeypatch.setattr(module, 'require_stopped', lambda *_: None)
    module.refresh_program(root, desktop, 'first')
    return module, root, desktop


def test_two_overwrite_updates_remove_stale_files_and_keep_one_combination(publisher):
    module, root, desktop = publisher
    (root / 'app/obsolete.py').write_text('old', encoding='utf-8')
    private = (root / 'data/private.txt').read_bytes()
    manifests = {p: (root / p / 'manifest.json').read_bytes() for p in ('runtimes/python', 'runtimes/dsh', 'runtimes/webview2', 'models')}
    for build in ('second', 'third'):
        module.refresh_program(root, desktop, build)
        verify_install(root)
        assert not (root / 'app/obsolete.py').exists()
        assert (root / 'data/private.txt').read_bytes() == private
        assert all((root / p / 'manifest.json').read_bytes() == content for p, content in manifests.items())
        assert sorted(p.name for p in (root / 'runtimes').iterdir()) == ['dsh', 'python', 'webview2']
        assert not (root / 'tmp/publish').exists()
        assert not (root / 'tmp/update-transaction.json').exists()


def test_build_failure_keeps_original_program_and_configuration(publisher, monkeypatch):
    module, root, desktop = publisher
    before = (root / 'app/release.json').read_bytes(), (root / 'portable.json').read_bytes()
    def broken(*_): raise RuntimeError('构建失败')
    monkeypatch.setattr(module, 'collect_program', broken)
    with pytest.raises(RuntimeError, match='构建失败'):
        module.refresh_program(root, desktop, 'failed')
    assert before == ((root / 'app/release.json').read_bytes(), (root / 'portable.json').read_bytes())
    assert not (root / 'tmp/publish').exists()


def test_invalid_component_fails_before_swap(publisher, tmp_path):
    import shutil
    module, root, desktop = publisher
    incoming = tmp_path / '候选环境'
    shutil.copytree(root / 'runtimes/python', incoming)
    (incoming / 'content.bin').write_bytes(b'corrupted')
    before = (root / 'app/release.json').read_bytes()
    with pytest.raises(ValueError, match='校验失败'):
        module.refresh_program(root, desktop, 'bad', python_runtime=incoming)
    assert (root / 'app/release.json').read_bytes() == before
    verify_install(root)


def test_component_update_replaces_whole_file_set(publisher, tmp_path):
    module, root, desktop = publisher
    incoming = tmp_path / '新环境'
    incoming.mkdir()
    (incoming / 'new.bin').write_bytes(b'new-python')
    atomic_json(incoming / 'manifest.json', manifest(incoming, kind='python', build='new'))
    module.refresh_program(root, desktop, 'updated', python_runtime=incoming)
    assert not (root / 'runtimes/python/content.bin').exists()
    assert (root / 'runtimes/python/new.bin').is_file()
    verify_install(root)


def test_python_runtime_verifier_ignores_only_manifested_pytest_bytecode(tmp_path):
    runtime = tmp_path / "python"
    source = runtime / "Lib/site-packages/anyio/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    record = manifest(runtime, kind="python", build="fixture")
    atomic_json(runtime / "manifest.json", record)
    cache = source.parent / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312-pytest-8.3.4.pyc").write_bytes(b"generated test cache")
    verify(runtime, record)

    (cache / "missing.cpython-312-pytest-8.3.4.pyc").write_bytes(b"unregistered source")
    with pytest.raises(ValueError, match="文件集合"):
        verify(runtime, record)


def test_project_candidate_moves_without_retaining_a_second_copy(publisher):
    module, root, desktop = publisher
    incoming = root / 'tmp/python-candidate'
    incoming.mkdir(parents=True)
    (incoming / 'new.bin').write_bytes(b'new-runtime')
    atomic_json(incoming / 'manifest.json', manifest(incoming, kind='python', build='new'))
    module.refresh_program(root, desktop, 'moved', python_runtime=incoming)
    assert not incoming.exists()
    assert (root / 'runtimes/python/new.bin').read_bytes() == b'new-runtime'
    assert not (root / 'tmp/publish').exists()
    verify_install(root)


@pytest.mark.parametrize('failure', ['renamed:app', 'installed:app', 'installed:portable.json', 'verified'])
def test_failed_swap_restores_full_combination(publisher, monkeypatch, failure):
    from voice_terminal import local_update
    module, root, desktop = publisher
    before = (root / 'app/release.json').read_bytes(), (root / 'portable.json').read_bytes(), (root / 'VoiceTerminal.exe').read_bytes()
    original = local_update.install
    def install(*args, **kwargs):
        def checkpoint(step):
            if step == failure: raise OSError('模拟换入失败')
        return original(*args, **kwargs, checkpoint=checkpoint)
    monkeypatch.setattr(local_update, 'install', install)
    with pytest.raises(OSError): module.refresh_program(root, desktop, 'failed')
    assert before == ((root / 'app/release.json').read_bytes(), (root / 'portable.json').read_bytes(), (root / 'VoiceTerminal.exe').read_bytes())
    verify_install(root)
    assert not (root / 'tmp/publish').exists()


@pytest.mark.parametrize('failure', ['renamed:app', 'installed:app', 'installed:portable.json'])
def test_interrupted_update_recovers_before_next_publish(publisher, monkeypatch, failure):
    from voice_terminal import local_update
    module, root, desktop = publisher
    before = (root / 'app/release.json').read_bytes()
    original = local_update.install
    def install(*args, **kwargs):
        def checkpoint(step):
            if step == failure: raise SystemExit('模拟进程中断')
        return original(*args, **kwargs, checkpoint=checkpoint)
    monkeypatch.setattr(local_update, 'install', install)
    with pytest.raises(SystemExit): module.refresh_program(root, desktop, 'interrupted')
    assert (root / 'tmp/update-transaction.json').exists()
    with local_update.update_lock(root):
        assert local_update.recover(root) == 'restored'
    assert (root / 'app/release.json').read_bytes() == before
    verify_install(root)
    monkeypatch.setattr(local_update, 'install', original)
    module.refresh_program(root, desktop, 'recovered')
    verify_install(root)


def test_export_only_current_declared_files_and_overwrites_latest(publisher):
    _, root, _ = publisher
    (root / 'cache').mkdir()
    (root / 'cache/private.log').write_text('不交付', encoding='utf-8')
    destination = root / 'releases/VoiceTerminal-latest.zip'
    export = script('package-desktop').export
    for _ in range(2):
        export(root, destination)
        with zipfile.ZipFile(destination) as archive:
            assert archive.testzip() is None
            assert all(not name.startswith(('data/', 'cache/', 'tmp/', 'build/', 'backups/', 'tools/', 'releases/')) for name in archive.namelist())
            assert json.loads(archive.read('portable.json'))['schema'] == 2
    assert {p.name for p in destination.parent.iterdir()} == {'VoiceTerminal-latest.zip', 'downloads.json'}


def test_backup_restores_important_data_without_dependencies_or_cache(tmp_path):
    from voice_terminal.local_files import backup_user_data
    root = tmp_path / '本机'
    for name in ('config/local.yaml', 'persona/user.md', 'speaker/user.npy', 'history/session.json', 'dsh/storages/tasks.json', '.env.local'):
        path = root / 'data' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    for name in ('cache/large.bin', 'logs/runtime.log', 'dsh/node_modules/large.bin'):
        path = root / 'data' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'exclude')
    for _ in range(2):
        record = backup_user_data(root)
        assert len(record['files']) == 6
        restored = tmp_path / '隔离恢复'
        import shutil
        shutil.copytree(root / 'backups/user-data', restored, dirs_exist_ok=True)
        assert all(digest(restored / row['path']) == row['sha256'] for row in record['files'])
    assert not (root / 'tmp/user-backup.old').exists()


def test_native_cache_records_lock_changes_without_deleting_channel_outputs(tmp_path, monkeypatch):
    module = script('prepare-build-cache')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    lock = tmp_path / 'apps/desktop/Cargo.lock'
    lock.parent.mkdir(parents=True)
    lock.write_text('version-1')
    cached = tmp_path / '.local/build/desktop/old.bin'
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b'cached')
    module.prepare('desktop', 'compiler-1')
    module.prepare('desktop', 'compiler-1')
    assert cached.exists()
    marker = tmp_path / '.local/build/desktop-cache.json'
    previous = json.loads(marker.read_text(encoding='utf-8'))['fingerprint']
    lock.write_text('version-2')
    module.prepare('desktop', 'compiler-1')
    # Cargo（原生包管理器）按依赖重新编译，各渠道固定输出不因登记新锁而整体删除。
    assert cached.exists()
    assert json.loads(marker.read_text(encoding='utf-8'))['fingerprint'] != previous


def test_dsh_cache_refresh_preserves_tracked_sources(tmp_path, monkeypatch):
    module = script('prepare-build-cache')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    source = tmp_path / '.local/build/dsh/source'
    for relative in ('config/dsh/runtime.lock.json', '.local/build/dsh/source/pnpm-lock.yaml',
                     '.local/build/dsh/source/packages/a/lib/tracked.js',
                     '.local/build/dsh/source/packages/b/lib/generated.js',
                     '.local/build/dsh/source/node_modules/old.js'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('content')
    monkeypatch.setattr(module.subprocess, 'check_output', lambda _: b'packages/a/lib/tracked.js\0')
    module.prepare('dsh', 'node-1')
    module.prepare('dsh', 'node-2')
    assert (source / 'packages/a/lib/tracked.js').is_file()
    assert not (source / 'packages/b/lib').exists()
    assert not (source / 'node_modules').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows（操作系统）文件占用验收')
def test_real_file_lock_rolls_back_without_mixed_version(publisher):
    import ctypes
    module, root, desktop = publisher
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    handle = kernel.CreateFileW(str(root / 'VoiceTerminal.exe'), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    before = (root / 'app/release.json').read_bytes()
    try:
        with pytest.raises(OSError): module.refresh_program(root, desktop, 'locked')
        assert (root / 'app/release.json').read_bytes() == before
        verify_install(root)
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)


def script(name):
    specification = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def frontend_build(path):
    """建立最小产品构建闭包，用于验证正式组装边界。"""
    (path / 'assets').mkdir(parents=True)
    (path / 'index.html').write_text('<script src="/assets/main.js"></script>', encoding='utf-8')
    (path / 'management.html').write_text('<script src="/assets/management.js"></script>', encoding='utf-8')
    (path / 'assets/management.js').write_text('management()', encoding='utf-8')
    (path / 'floating.html').write_text('<script src="/assets/floating.js"></script>', encoding='utf-8')
    (path / 'assets/floating.js').write_text('floating()', encoding='utf-8')
    for name in ('startup.html', 'startup.css', 'app-icon.svg'):
        (path / name).write_text('fixture', encoding='utf-8')
    (path / 'assets/main.js').write_text('import("./product.js")', encoding='utf-8')
    (path / 'assets/product.js').write_text('product()', encoding='utf-8')
    (path / 'assets/product.css').write_text('body{}', encoding='utf-8')
    atomic_json(path / 'desktop-manifest.json', {
        'index.html': {'isEntry': True, 'file': 'assets/main.js', 'dynamicImports': ['product']},
        'management.html': {'isEntry': True, 'file': 'assets/management.js'},
        'floating.html': {'isEntry': True, 'file': 'assets/floating.js'},
        'product': {'file': 'assets/product.js', 'css': ['assets/product.css']},
    })


def test_frontend_collection_only_copies_product_dependency_closure(tmp_path):
    source, target = tmp_path / '构建', tmp_path / '发布'
    frontend_build(source)
    script('assemble-desktop').collect_frontend(source, target)
    assert {path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file()} == {
        'index.html', 'desktop-manifest.json', 'assets/main.js', 'assets/product.js', 'assets/product.css',
        'management.html', 'assets/management.js', 'floating.html', 'assets/floating.js',
        'startup.html', 'startup.css', 'app-icon.svg',
    }


@pytest.mark.parametrize('extra', ['ui-demo.html', 'assets/fixture.js', 'frontend-dev.mjs'])
def test_frontend_collection_rejects_preview_or_stale_artifacts(tmp_path, extra):
    source, target = tmp_path / '构建', tmp_path / '发布'
    frontend_build(source)
    (source / extra).write_text('不能进入正式包', encoding='utf-8')
    with pytest.raises(ValueError, match='样例|陈旧'):
        script('assemble-desktop').collect_frontend(source, target)
    assert not target.exists()


def test_frontend_collection_rejects_additional_entry_and_preview_dependency(tmp_path):
    source = tmp_path / '构建'
    frontend_build(source)
    records = json.loads((source / 'desktop-manifest.json').read_text())
    records['ui-demo.html'] = {'isEntry': True, 'file': 'assets/product.js'}
    atomic_json(source / 'desktop-manifest.json', records)
    with pytest.raises(ValueError, match='入口集合'):
        script('assemble-desktop').collect_frontend(source, tmp_path / '发布')
    records.pop('ui-demo.html')
    records['index.html']['dynamicImports'] = ['src/ui-demo/main.ts']
    records['src/ui-demo/main.ts'] = {'file': 'assets/product.js'}
    atomic_json(source / 'desktop-manifest.json', records)
    with pytest.raises(ValueError, match='样例'):
        script('assemble-desktop').collect_frontend(source, tmp_path / '发布')


def test_cuda_probe_requires_a_real_successful_calculation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    module = script('build-runtime')
    result = SimpleNamespace(returncode=1, stdout='', stderr='CUDA initialization failed')
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs:result)
    with pytest.raises(RuntimeError, match='实际计算验证失败'):
        module.verify_cuda(tmp_path / 'python.exe', env={}, cwd=tmp_path)
    result.returncode, result.stdout, result.stderr = 0, 'VOICE_CUDA_RESULT={"verified":true,"matrix_sum":262144.0}', ''
    assert module.verify_cuda(tmp_path / 'python.exe', env={}, cwd=tmp_path)['verified'] is True


def test_onnx_cuda_probe_rejects_provider_listing_without_actual_gpu_kernel(monkeypatch, tmp_path):
    from types import SimpleNamespace
    module = script('build-runtime')
    result = SimpleNamespace(returncode=0, stdout='VOICE_ONNX_CUDA_RESULT={"verified":true,"matrix_sum":262144.0,"cuda_matmul_calls":0}', stderr='')
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs:result)
    with pytest.raises(RuntimeError, match='内核追踪'):
        module.verify_onnx_cuda(tmp_path / 'python.exe', env={}, cwd=tmp_path)
    result.stdout = 'VOICE_ONNX_CUDA_RESULT={"verified":true,"matrix_sum":262144.0,"cuda_matmul_calls":1}'
    assert module.verify_onnx_cuda(tmp_path / 'python.exe', env={}, cwd=tmp_path)['verified'] is True


def core_model_source(path):
    module = script('assemble-desktop')
    for relative in module.CORE_MODELS:
        folder = path / relative
        folder.mkdir(parents=True)
        (folder / 'model.bin').write_bytes(b'weights')
    smart = path / 'analysis/smart-turn'
    (smart / 'smart-turn-v3.2-cpu.onnx').write_bytes(b'cpu weights')
    (smart / 'smart-turn-v3.2-gpu.onnx').write_bytes(b'gpu weights')
    (path / module.CORE_MODELS[0] / 'keywords.txt').write_bytes(b'user keywords')
    return path

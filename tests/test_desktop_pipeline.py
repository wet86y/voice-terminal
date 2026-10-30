"""Desktop pipeline（桌面流水线）：覆盖内容复用、同包交付及真实脚本宿主的故障传播。"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state(monkeypatch):
    module = load_script('desktop-build-state')
    monkeypatch.setattr(module, 'tool_identity', lambda *_: {'compiler': 'fixture（测试编译器）'})
    monkeypatch.setattr(module, 'paths_for', lambda *_: (['source'], ['output']))
    return module


def test_content_identity_detects_same_size_edits_and_output_corruption(tmp_path, state):
    source = tmp_path / 'source'; source.write_bytes(b'abcd')
    output = tmp_path / 'output'; output.write_bytes(b'compiled')
    assert not state.run(tmp_path, 'check', 'product')
    state.run(tmp_path, 'begin', 'product'); state.run(tmp_path, 'record', 'product')
    assert state.run(tmp_path, 'check', 'product')
    timestamp = source.stat().st_mtime_ns
    source.write_bytes(b'wxyz'); os.utime(source, ns=(timestamp, timestamp))
    assert not state.run(tmp_path, 'check', 'product')
    source.write_bytes(b'abcd')
    output.write_bytes(b'corrupt!')
    assert not state.run(tmp_path, 'check', 'product')


def test_inputs_changed_during_build_are_not_registered(tmp_path, state):
    (tmp_path / 'source').write_text('before')
    state.run(tmp_path, 'begin', 'product')
    (tmp_path / 'source').write_text('after')
    (tmp_path / 'output').write_text('result')
    with pytest.raises(ValueError, match='构建期间输入发生变化'):
        state.run(tmp_path, 'record', 'product')
    assert not state.run(tmp_path, 'check', 'product')


def test_inventory_hashes_overlapping_inputs_once(tmp_path, monkeypatch):
    module = load_script('desktop-build-state')
    folder = tmp_path / 'source'; folder.mkdir()
    nested = folder / 'nested'; nested.mkdir()
    (folder / 'one').write_bytes(b'one'); (nested / 'two').write_bytes(b'two')
    expected = module.inventory(tmp_path, ['source'])
    calls = []
    original = module.digest
    def counted(path):
        calls.append(path.relative_to(tmp_path).as_posix())
        return original(path)
    monkeypatch.setattr(module, 'digest', counted)
    assert module.inventory(tmp_path, ['source', 'source/nested', 'source/one', 'source']) == expected
    assert sorted(calls) == ['source/nested/two', 'source/one']


def test_package_check_reads_archive_once_and_still_rejects_same_size_corruption(tmp_path, state, monkeypatch):
    directory = tmp_path / '.local/build/program-package'; directory.mkdir(parents=True)
    archive = directory / 'program.zip'; archive.write_bytes(b'package')
    info = directory / 'package-info.json'
    info.write_text(json.dumps({'size': 7, 'sha256': state.digest(archive), 'build': 'fixture'}))
    (tmp_path / 'source').write_text('source')
    monkeypatch.setattr(state, 'paths_for', lambda *_: (['source'], [
        '.local/build/program-package/program.zip', '.local/build/program-package/package-info.json']))
    state.run(tmp_path, 'begin', 'package'); state.run(tmp_path, 'record', 'package')
    calls = []
    original = state.digest
    def counted(path):
        if path == archive: calls.append(path)
        return original(path)
    monkeypatch.setattr(state, 'digest', counted)
    assert state.run(tmp_path, 'check', 'package', build='fixture')
    assert len(calls) == 1
    timestamp = archive.stat().st_mtime_ns
    archive.write_bytes(b'corrupt'); os.utime(archive, ns=(timestamp, timestamp))
    assert not state.run(tmp_path, 'check', 'package')
    with pytest.raises(ValueError, match='大小或摘要不符'):
        state.verify_package(tmp_path)
    archive.write_bytes(b'package')
    info.write_text(json.dumps({'size': 8, 'sha256': original(archive), 'build': 'fixture'}))
    # 错误元数据即使被登记进产物快照，也不能代替包自身的大小约束。
    state.run(tmp_path, 'begin', 'package'); state.run(tmp_path, 'record', 'package')
    assert not state.run(tmp_path, 'check', 'package')
    calls.clear()
    with pytest.raises(ValueError, match='大小或摘要不符'):
        state.verify_package(tmp_path)
    assert not calls


def test_installer_reuses_only_the_current_input_archive_digest(tmp_path, monkeypatch):
    module = load_script('desktop-build-state')
    archive = tmp_path / '.local/build/program-package/program.zip'; archive.parent.mkdir(parents=True)
    archive.write_bytes(b'package')
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *_, **__: 'fixture compiler')
    monkeypatch.setenv('VOICE_PROGRAM_PACKAGE', str(archive))
    calls = []
    original = module.digest
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(module, 'digest', counted)
    inputs, _, _ = module.inputs_for(tmp_path, 'installer', 'release', 'installer', False, '')
    assert calls.count(archive) == 1
    assert json.loads(inputs['<tools>'])['embedded_package'] == original(archive)
    external = tmp_path.parent / (tmp_path.name + '-external.zip')
    try:
        external.write_bytes(b'external package')
        monkeypatch.setenv('VOICE_PROGRAM_PACKAGE', str(external)); calls.clear()
        inputs, _, _ = module.inputs_for(tmp_path, 'installer', 'release', 'installer', False, '')
        assert calls.count(archive) == calls.count(external) == 1
        assert json.loads(inputs['<tools>'])['embedded_package'] == original(external)
    finally:
        external.unlink(missing_ok=True)


def test_archive_reuses_identical_bytes_across_timestamp_changes(tmp_path):
    module = load_script('assemble-program-package')
    stage = tmp_path / 'payload'; stage.mkdir()
    source = stage / 'example.txt'; source.write_bytes(b'content')
    archive = tmp_path / 'program.zip'
    module.write_archive(stage, archive)
    before = archive.read_bytes(); timestamp = archive.stat().st_mtime_ns
    os.utime(source, (1_700_000_000, 1_700_000_000))
    module.write_archive(stage, archive)
    assert archive.read_bytes() == before
    assert archive.stat().st_mtime_ns == timestamp
    with zipfile.ZipFile(archive) as opened:
        assert opened.read('example.txt') == b'content'
        assert opened.getinfo('example.txt').date_time == (1980, 1, 1, 0, 0, 0)


def ps(root, script, timeout=30):
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                           '-Command', script], cwd=root, capture_output=True, timeout=timeout)


@pytest.fixture
def pipeline_root(tmp_path):
    if sys.platform != 'win32':
        pytest.skip('需要 Windows PowerShell（实际脚本宿主）')
    root = tmp_path / '空 格工程'; root.mkdir()
    scripts = root / 'scripts'; scripts.mkdir()
    (root / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    for name in ['desktop-pipeline.ps1', 'desktop-build-state.py', 'prepare-desktop-product.ps1',
                 'publish-desktop.ps1', 'distribute-desktop.ps1', 'deliver-desktop.ps1']:
        shutil.copy2(ROOT / 'scripts' / name, scripts / name)
    python = str(Path(sys.executable)).replace("'", "''")
    (scripts / 'local-context.ps1').write_text(textwrap.dedent(f"""
        function Get-ProjectRoot {{Split-Path $PSScriptRoot -Parent}}
        function Get-LocalContext {{return @{{Python='{python}';Node=(Get-Command node.exe).Source}}}}
        function Get-Sha256File {{param([string]$LiteralPath);$stream=[IO.File]::OpenRead($LiteralPath);$sha=[Security.Cryptography.SHA256]::Create();try{{[BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant()}}finally{{$stream.Dispose();$sha.Dispose()}}}}
        """), encoding='utf-8-sig')
    (scripts / 'prepare-7zip.ps1').write_text('$global:LASTEXITCODE=0', encoding='utf-8-sig')
    plugin = root / 'integrations/dsh/voice-terminal'; plugin.mkdir(parents=True)
    (plugin / 'build.mjs').write_text("""
        import {mkdirSync,writeFileSync,appendFileSync} from 'node:fs';
        mkdirSync('.local/build/dsh/bundles/voice-terminal',{recursive:true});
        writeFileSync('.local/build/dsh/bundles/voice-terminal/plugin.js','plugin');
        appendFileSync('.local/counts.txt','plugin\\n');
        """, encoding='utf-8')
    (scripts / 'build-desktop.ps1').write_text(textwrap.dedent("""
        param($BuildTarget,$ProgramPackage,[switch]$Qualification,[switch]$RequireExisting)
        $root=Split-Path $PSScriptRoot -Parent
        if($BuildTarget -eq 'installer'){
          $dir=Join-Path $root '.local/build/desktop/installer'
          New-Item -ItemType Directory -Path "$dir/release" -Force | Out-Null
          [IO.File]::WriteAllText("$dir/release/VoiceTerminalSetup.exe",'installer')
          @{role='installer';program_package_sha256=(Get-Sha256File -LiteralPath $ProgramPackage)} | ConvertTo-Json | Set-Content "$dir/build-stamp.json"
          Add-Content (Join-Path $root '.local/counts.txt') 'installer'
        }else{
          $dir=Join-Path $root '.local/build/desktop/product'
          New-Item -ItemType Directory -Path "$dir/release" -Force | Out-Null
          [IO.File]::WriteAllText("$dir/release/VoiceTerminal.exe",'entry')
          [IO.File]::WriteAllText("$dir/release/VoiceTerminalDesktop.exe",'desktop')
          [IO.File]::WriteAllText("$dir/build-stamp.json",'stamp')
        }
        $global:LASTEXITCODE=0
        """), encoding='utf-8-sig')
    (scripts / 'assemble-program-package.py').write_text(textwrap.dedent("""
        import hashlib,json
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        out=root/'.local/build/program-package';out.mkdir(parents=True,exist_ok=True)
        (out/'program.zip').write_bytes(b'fixed package')
        (out/'package-info.json').write_text(json.dumps({'version':'0.9.6','build':'fixture','size':13,'sha256':hashlib.sha256(b'fixed package').hexdigest()}))
        with (root/'.local/counts.txt').open('a') as file:file.write('package\\n')
        """), encoding='utf-8')
    common = scripts / 'desktop-pipeline.ps1'
    with common.open('a', encoding='utf-8') as file:
        file.write("\nfunction Invoke-DesktopInstallProcess {param($Executable,$Arguments);$kind=if($Arguments[0] -eq '--retain-installer'){'retain'}elseif($Arguments[0] -eq '--install-package'){'install'}else{throw '未知维护操作'};Add-Content (Join-Path (Get-ProjectRoot) '.local/counts.txt') $kind}\n")
    # 停机检查仍执行真实脚本入口，但夹具没有用户进程或数据。
    voice = root / 'voice_terminal'; voice.mkdir()
    (voice / '__init__.py').write_text('')
    (voice / 'distribution.py').write_text('def require_stopped(*args): pass\n')
    return root


def test_combined_delivery_assembles_once_and_skip_build_never_repackages(pipeline_root):
    root = pipeline_root
    first = ps(root, "& ./scripts/deliver-desktop.ps1")
    assert first.returncode == 0, first.stdout + first.stderr
    counts = (root / '.local/counts.txt').read_text('utf-8-sig').splitlines()
    assert counts.count('plugin') == counts.count('package') == counts.count('installer') == counts.count('install') == 1
    assert counts.count('retain') == 1
    package = root / '.local/build/program-package/program.zip'
    content, timestamp = package.read_bytes(), package.stat().st_mtime_ns
    second = ps(root, "& ./scripts/publish-desktop.ps1 -SkipBuild")
    assert second.returncode == 0, second.stdout + second.stderr
    assert package.read_bytes() == content and package.stat().st_mtime_ns == timestamp
    counts = (root / '.local/counts.txt').read_text('utf-8-sig').splitlines()
    assert counts.count('package') == 1
    assert counts.count('install') == counts.count('retain') == 2
    (root / 'voice_terminal/distribution.py').write_text('def require_stopped(*args): pass\n# changed input\n')
    rejected = ps(root, "& ./scripts/publish-desktop.ps1 -SkipBuild")
    assert rejected.returncode != 0
    assert (root / '.local/counts.txt').read_text('utf-8-sig').splitlines().count('install') == 2


def test_install_failure_releases_lock_and_progress_is_captured(pipeline_root):
    root = pipeline_root
    # 使用真实读取函数，覆盖标准错误、中文输出、空格路径和非零退出码。
    shutil.copy2(ROOT / 'scripts/desktop-pipeline.ps1', root / 'scripts/desktop-pipeline.ps1')
    worker = root / '工作 进程.py'
    worker.write_text("import sys\nprint('核验进度',flush=True)\nprint('故障原因',file=sys.stderr,flush=True)\nsys.exit(7)\n", encoding='utf-8')
    executable = sys.executable.replace("'", "''")
    command = f". ./scripts/desktop-pipeline.ps1; Enter-DesktopPipeline; try {{Invoke-DesktopInstallProcess -Executable '{executable}' -Arguments @('-X','utf8','{worker}')}} finally {{Exit-DesktopPipeline}}"
    failed = ps(root, command)
    assert failed.returncode != 0
    log = (root / '.local/cache/logs/desktop-install-latest.log').read_text('utf-8')
    assert '核验进度' in log and '故障原因' in log
    recovered = ps(root, '. ./scripts/desktop-pipeline.ps1; Enter-DesktopPipeline; Exit-DesktopPipeline')
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr


def test_native_argument_quoting_preserves_quotes_and_trailing_backslash(pipeline_root):
    root = pipeline_root
    shutil.copy2(ROOT / 'scripts/desktop-pipeline.ps1', root / 'scripts/desktop-pipeline.ps1')
    worker = root / 'argument-probe.py'
    worker.write_text('import json,sys\nprint(json.dumps(sys.argv[1:],ensure_ascii=False))\n', encoding='utf-8')
    executable = sys.executable.replace("'", "''")
    values = ['C:\\空 格目录\\', 'quoted"value', '']
    arguments = ','.join("'" + value.replace("'", "''") + "'" for value in ['-X', 'utf8', str(worker)] + values)
    result = ps(root, f". ./scripts/desktop-pipeline.ps1; Invoke-DesktopInstallProcess -Executable '{executable}' -Arguments @({arguments})")
    assert result.returncode == 0, result.stdout + result.stderr
    log = (root / '.local/cache/logs/desktop-install-latest.log').read_text('utf-8').splitlines()
    assert json.loads(log[0].split(' ', 1)[1]) == values


@pytest.mark.parametrize('network,expected_calls', [(False, 1), (True, 2)])
def test_cargo_configuration_and_error_classification(pipeline_root, network, expected_calls):
    root = pipeline_root
    shutil.copy2(ROOT / 'scripts/download-sources.ps1', root / 'scripts/download-sources.ps1')
    config = root / 'config'; config.mkdir()
    (config / 'download-sources.json').write_text(json.dumps({'rust_index': 'sparse+https://example.invalid/index/',
        'rust_dist': 'https://example.invalid', 'rust_update': 'https://example.invalid'}), encoding='utf-8')
    worker = root / 'cargo-probe.py'
    message = 'error: failed to download dependency' if network else 'error[E0001]: compilation error'
    worker.write_text(textwrap.dedent(f"""
        import json,sys
        from pathlib import Path
        counter=Path('.local/cargo-calls.txt')
        calls=int(counter.read_text())+1 if counter.exists() else 1
        counter.write_text(str(calls))
        if '--config' in sys.argv:
            value=Path(sys.argv[sys.argv.index('--config')+1]).read_text()
            assert 'replace-with = "voice-mirror"' in value
            print({message!r},file=sys.stderr)
            sys.exit(1)
        print('Finished fixture build')
        """), encoding='utf-8')
    executable = sys.executable.replace("'", "''")
    result = ps(root, f". ./scripts/download-sources.ps1; Invoke-SourceCargo -Executable '{executable}' -Arguments @('-X','utf8','{worker}')")
    assert (root / '.local/cargo-calls.txt').read_text() == str(expected_calls)
    assert (result.returncode == 0) is network, result.stdout + result.stderr


@pytest.mark.parametrize('target,allowed', [('other-commit', False), ('expected', True)])
def test_release_target_is_checked_before_any_remote_write(pipeline_root, target, allowed):
    root = pipeline_root
    shutil.copy2(ROOT / 'scripts/github-release-target.ps1', root / 'scripts/github-release-target.ps1')
    response = json.dumps({'data': {'repository': {'ref': {'target': {'__typename': 'Commit', 'oid': target}},
        'release': {'isDraft': False, 'isPrerelease': False}}}})
    command = f"function gh {{'{response}';$global:LASTEXITCODE=0}}; . ./scripts/github-release-target.ps1; Assert-GitHubReleaseTarget -Tag v0.9.6 -Head expected"
    result = ps(root, command)
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr


def test_concurrent_pipeline_is_rejected_without_overwriting_artifacts(pipeline_root):
    root = pipeline_root
    command = ". ./scripts/desktop-pipeline.ps1; Enter-DesktopPipeline; try {[IO.File]::WriteAllText((Join-Path (Get-ProjectRoot) '.local/ready'),'ready'); while(-not (Test-Path .local/release)){Start-Sleep -Milliseconds 50}} finally {Exit-DesktopPipeline}"
    owner = subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], cwd=root,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (root / '.local/ready').exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert (root / '.local/ready').exists()
        rejected = ps(root, '. ./scripts/desktop-pipeline.ps1; Enter-DesktopPipeline; Exit-DesktopPipeline')
        assert rejected.returncode != 0
    finally:
        (root / '.local/release').write_text('release')
        owner.communicate(timeout=10)
    assert owner.returncode == 0


def test_public_verification_consumes_one_snapshot_and_removes_only_its_files(pipeline_root):
    root = pipeline_root
    shutil.copy2(ROOT / 'scripts/verify-github-release.ps1', root / 'scripts/verify-github-release.ps1')
    docs = root / 'docs/releases'; docs.mkdir(parents=True)
    (docs / 'v0.9.6.md').write_text('release notes', encoding='utf-8')
    output = root / '.local/releases/installer'; output.mkdir(parents=True)
    for name in ['VoiceTerminalSetup.exe', 'delivery.json', 'update.json', 'SHA256SUMS.txt']:
        (output / name).write_text(name)
    script = r"""
        function git {'expected';$global:LASTEXITCODE=0}
        function gh {
          if($args[0] -eq 'release' -and $args[1] -eq 'view'){'{"tagName":"v0.9.6","body":"release notes","isDraft":false,"isPrerelease":false}'}
          elseif($args[0] -eq 'release' -and $args[1] -eq 'download'){
            $dir=$args[[Array]::IndexOf($args,'--dir')+1]
            foreach($name in @('VoiceTerminalSetup.exe','delivery.json','update.json','SHA256SUMS.txt')){[IO.File]::Copy((Join-Path (Get-ProjectRoot) ".local/releases/installer/$name"),(Join-Path $dir $name))}
          }
          elseif($args[1] -like '*/releases/latest'){'v0.9.6'}
          else{'{"object":{"type":"commit","sha":"expected"}}'}
          $global:LASTEXITCODE=0
        }
        & ./scripts/verify-github-release.ps1 -Tag v0.9.6
        """
    result = ps(root, script)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / '.local/cache/reports/release-public-latest.json').read_text('utf-8'))
    assert report['commit'] == 'expected' and len(report['assets']) == 4
    assert not list((root / '.local/tmp').iterdir())

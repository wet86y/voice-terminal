"""开发资产治理：目录联接隔离、活动任务保护、保留预算与真实暂存对象门禁。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from scripts import workspace_assets as assets

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('workspace_asset_cli', ROOT / 'scripts/workspace-assets.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
def workspace(tmp_path):
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'development-assets.json').write_bytes((ROOT / 'config/development-assets.json').read_bytes())
    return tmp_path


def limits(root, **changes):
    path = root / 'config/development-assets.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['tasks'].update(changes)
    path.write_text(json.dumps(value), encoding='utf-8')


def finished(root, number, *, size=10, state='failed', age=1):
    run = assets.ManagedRun('example', root)
    (run.path / 'evidence.bin').write_bytes(b'x' * size)
    record = run.record.copy()
    record.update(pid=900000 + number, state=state, started_at=time.time() - age * 86400,
                  finished_at=time.time() - age * 86400)
    (run.path / 'asset-run.json').write_text(json.dumps(record), encoding='utf-8')
    return run.path


def link_directory(link, target):
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
        if result.returncode:
            pytest.skip('当前设备无法建立测试目录联接')
    else:
        link.symlink_to(target, target_is_directory=True)


def test_success_removes_only_owned_task_and_overwrites_summary(workspace):
    unknown = workspace / '.local/tmp/unregistered'
    unknown.mkdir(parents=True)
    (unknown / 'keep').write_text('keep')
    for _ in range(2):
        with assets.ManagedRun('example', workspace) as run:
            (run.path / 'large.wav').write_bytes(b'x' * 100)
        assert not run.path.exists()
    assert (unknown / 'keep').exists()
    reports = list((workspace / '.local/cache/reports/tasks/example').iterdir())
    assert [p.name for p in reports] == ['latest.json']
    assert json.loads(reports[0].read_text())['state'] == 'succeeded'


def test_backend_task_prefix_is_shorter_than_previous_test_runner(workspace):
    with assets.ManagedRun('backend-suite', workspace) as run:
        relative = (run.path / 'p').relative_to(workspace).as_posix()
        assert len(relative) <= len('.local/tmp/backend-suite-abcdefgh/pytest')
        assert run.record['kind'] == 'backend-suite'


def test_failure_is_preserved_and_live_owner_cannot_be_pruned(workspace):
    limits(workspace, keep_failed=0, max_age_days=0)
    with pytest.raises(RuntimeError):
        with assets.ManagedRun('example', workspace) as run:
            raise RuntimeError('failure')
    assert run.path.exists()
    assert not assets.sweep_tasks(workspace, apply=True)['removed']
    assert assets.owner_alive(os.getpid())


def test_forced_interrupt_does_not_claim_children_have_stopped(workspace):
    with pytest.raises(KeyboardInterrupt):
        with assets.ManagedRun('example', workspace) as run:
            raise KeyboardInterrupt()
    assert json.loads((run.path / 'asset-run.json').read_text())['state'] == 'running'


def test_maintenance_failure_does_not_change_success_or_original_failure(workspace, monkeypatch, capsys):
    original = assets.write_record
    def locked_report(root, relative, value):
        if '/reports/' in relative:
            raise PermissionError('report locked')
        return original(root, relative, value)
    monkeypatch.setattr(assets, 'write_record', locked_report)
    with assets.ManagedRun('example', workspace) as run:
        (run.path / 'evidence').write_text('keep')
    assert run.path.exists()
    with pytest.raises(RuntimeError, match='original assertion'):
        with assets.ManagedRun('example', workspace):
            raise RuntimeError('original assertion')
    assert '不改变验证结果' in capsys.readouterr().err


def test_cleanup_failure_and_missing_policy_do_not_block_tests(workspace, monkeypatch):
    import voice_terminal.local_files as local_files
    (workspace / 'config/development-assets.json').unlink()
    def held_open(*args):
        raise PermissionError('held open')
    monkeypatch.setattr(local_files, 'remove_tree', held_open)
    with assets.ManagedRun('example', workspace) as run:
        (run.path / 'result').write_text('passed')
    assert (run.path / 'result').exists()


def test_explicit_success_evidence_is_retained_for_manual_inspection(workspace, monkeypatch):
    with assets.ManagedRun('example', workspace, keep_success=True) as run:
        (run.path / 'sample.wav').write_bytes(b'audio')
    monkeypatch.setattr(assets, 'owner_alive', lambda pid: False)
    assets.sweep_tasks(workspace, apply=True)
    assert (run.path / 'sample.wav').read_bytes() == b'audio'


def test_new_task_does_not_scan_unrelated_task_kind(workspace, monkeypatch):
    other = assets.ManagedRun('unrelated', workspace)
    other.finish(1)
    record_path = other.path / 'asset-run.json'
    value = json.loads(record_path.read_text())
    value['pid'] = 900001
    record_path.write_text(json.dumps(value))
    original = assets.measure
    def measure_current_only(path):
        assert 'unrelated' not in str(path)
        return original(path)
    monkeypatch.setattr(assets, 'measure', measure_current_only)
    with assets.ManagedRun('example', workspace):
        pass


def test_retention_keeps_newest_two_and_dry_run_never_deletes(workspace, monkeypatch):
    paths = [finished(workspace, i, age=i + 1) for i in range(3)]
    monkeypatch.setattr(assets, 'owner_alive', lambda pid: False)
    planned = assets.sweep_tasks(workspace)
    assert planned['candidates'] == [paths[-1].relative_to(workspace).as_posix()]
    assert all(path.exists() for path in paths)
    assets.sweep_tasks(workspace, apply=True)
    assert paths[0].exists() and paths[1].exists() and not paths[2].exists()


def test_expired_and_oversized_failures_are_reclaimed(workspace, monkeypatch):
    monkeypatch.setattr(assets, 'owner_alive', lambda pid: True)
    expired = finished(workspace, 1, age=8)
    oversized = finished(workspace, 2, size=10000)
    limits(workspace, max_failed_bytes=2000)
    monkeypatch.setattr(assets, 'owner_alive', lambda pid: False)
    result = assets.sweep_tasks(workspace, apply=True)
    assert len(result['removed']) == 2
    assert not expired.exists() and not oversized.exists()


def test_oversized_newest_run_does_not_evict_small_older_evidence(workspace, monkeypatch):
    older = finished(workspace, 1, size=10, age=1)
    giant = finished(workspace, 2, size=10000, age=0)
    limits(workspace, max_failed_bytes=4096)
    monkeypatch.setattr(assets, 'owner_alive', lambda pid: False)
    assets.sweep_tasks(workspace, apply=True)
    assert older.exists() and not giant.exists()


def test_unknown_corrupt_and_interrupted_tasks_are_preserved(workspace, monkeypatch):
    interrupted = finished(workspace, 1, state='running', age=30)
    bad = interrupted.parent / ('b' * 32)
    bad.mkdir()
    (bad / 'asset-run.json').write_text('{broken')
    unknown = interrupted.parent / 'manual-copy'
    unknown.mkdir()
    monkeypatch.setattr(assets, 'owner_alive', lambda pid: False)
    result = assets.sweep_tasks(workspace, apply=True)
    assert not result['removed']
    assert interrupted.exists() and bad.exists() and unknown.exists()
    assert len(result['skipped']) == 3


def test_task_junction_never_traverses_or_deletes_external_data(workspace, tmp_path):
    external = tmp_path / 'external-protected'
    external.mkdir()
    sentinel = external / 'user-data'
    sentinel.write_text('protected')
    with assets.ManagedRun('example', workspace) as run:
        link_directory(run.path / 'shared-models', external)
        footprint = assets.measure(run.path)
        assert footprint['reparse_points'] == 1
    assert sentinel.read_text() == 'protected'


def test_ancestor_junction_and_escape_are_rejected(workspace, tmp_path):
    external = tmp_path / 'external'
    external.mkdir()
    (workspace / '.local').mkdir()
    link_directory(workspace / '.local/tmp', external)
    with pytest.raises(ValueError, match='重解析点'):
        assets.ManagedRun('example', workspace)
    with pytest.raises(ValueError, match='越出'):
        assets.guarded(workspace, '../outside')
    assert not list(external.iterdir())


def test_budget_audit_only_warns_and_never_removes(workspace):
    path = workspace / 'config/development-assets.json'
    value = json.loads(path.read_text())
    value['budgets'] = {'.local/cache/reports': 1}
    path.write_text(json.dumps(value))
    target = workspace / '.local/cache/reports/keep'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'evidence')
    result = cli.audit(workspace)
    assert result['ok'] and result['over_budget']
    assert target.read_bytes() == b'evidence'


@pytest.fixture
def repository(workspace):
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    (workspace / '.gitignore').write_text('.local/\n*.log\n')
    git(workspace, 'add', 'config', '.gitignore')
    git(workspace, '-c', 'user.name=Asset Test', '-c', 'user.email=asset@example.invalid', 'commit', '-qm', 'baseline')
    return workspace


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def test_gate_rejects_forced_ignored_files_and_keeps_index(repository):
    target = repository / '.local/secrets.txt'
    target.parent.mkdir()
    target.write_text('not printed')
    git(repository, 'add', '-f', str(target))
    before = git(repository, 'ls-files', '--stage', '-z').stdout
    result = cli.check_index(repository)
    assert not result['ok'] and result['errors'][0]['path'] == '.local/secrets.txt'
    assert 'not printed' not in json.dumps(result)
    assert git(repository, 'ls-files', '--stage', '-z').stdout == before


def test_gate_uses_staged_blob_not_current_small_working_file(repository):
    target = repository / 'large.txt'
    target.write_bytes(b'x' * (1024**2 + 1))
    git(repository, 'add', 'large.txt')
    target.write_text('small unstaged replacement')
    assert cli.check_index(repository)['ok']
    assert cli.check_index(repository)['warnings']
    assert not cli.check_index(repository, strict=True)['ok']


def test_gate_rejects_nested_target_and_unapproved_binary(repository):
    (repository / 'apps/other/target').mkdir(parents=True)
    (repository / 'apps/other/target/result.txt').write_text('generated')
    (repository / 'model.gguf').write_text('binary')
    (repository / 'recording.wav').write_bytes(b'audio evidence')
    git(repository, 'add', '.')
    result = cli.check_index(repository)
    assert {row['path'] for row in result['errors']} == {'apps/other/target/result.txt', 'model.gguf', 'recording.wav'}


def test_gate_requires_exact_asset_hash_and_enforces_total_content_budget(repository):
    for name in ['a', 'b', 'c']:
        (repository / f'{name}.txt').write_bytes(name.encode() * (2 * 1024**2))
    git(repository, 'add', 'a.txt', 'b.txt', 'c.txt')
    result = cli.check_index(repository)
    assert result['ok'] and result['changed_bytes'] == 6 * 1024**2
    assert any(row['path'] == '[staged]' for row in result['warnings'])
    assert not cli.check_index(repository, strict=True)['ok']
    git(repository, 'reset', '-q', 'HEAD')
    asset = repository / 'config/resources/assets/audiocpp/voxcpm-patch.zip'
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'wrong asset')
    git(repository, 'add', str(asset))
    assert any('固定资产' in row['reason'] for row in cli.check_index(repository)['warnings'])


def test_gate_accepts_small_source_and_deletions(repository):
    (repository / 'source.py').write_text('value = 1\n')
    git(repository, 'add', 'source.py')
    assert cli.check_index(repository)['ok']
    git(repository, 'rm', '.gitignore')
    assert cli.check_index(repository)['ok']


def test_intentional_small_audio_fixture_is_allowed(repository):
    fixture = repository / 'tests/fixtures/sample.wav'
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b'small fixture')
    git(repository, 'add', str(fixture))
    assert cli.check_index(repository)['ok']


def test_tracked_source_is_not_blocked_by_later_ignore_rule(repository):
    source = repository / 'source.py'
    source.write_text('version = 1')
    git(repository, 'add', 'source.py')
    git(repository, '-c', 'user.name=Asset Test', '-c', 'user.email=asset@example.invalid', 'commit', '-qm', 'source')
    (repository / '.gitignore').write_text('*.py\n')
    source.write_text('version = 2')
    git(repository, 'add', 'source.py')
    assert cli.check_index(repository)['ok']


def test_intentional_ignored_log_fixture_is_allowed(repository):
    fixture = repository / 'tests/fixtures/expected.log'
    fixture.parent.mkdir(parents=True)
    fixture.write_text('golden fixture')
    git(repository, 'add', '-f', str(fixture))
    assert cli.check_index(repository)['ok']


def test_asset_and_approved_digest_can_be_committed_together(repository):
    import hashlib
    asset_name = 'config/resources/assets/fsmn-vad/onnx/model.onnx'
    target = repository / asset_name
    target.parent.mkdir(parents=True)
    content = b'model fixture' * 100000
    target.write_bytes(content)
    config = repository / 'config/development-assets.json'
    value = json.loads(config.read_text())
    value['commit']['pinned_assets'][asset_name]['sha256'] = hashlib.sha256(content).hexdigest()
    config.write_text(json.dumps(value))
    git(repository, 'add', str(target), str(config))
    assert cli.check_index(repository, strict=True)['ok']


def test_unstaged_policy_relaxation_does_not_allow_large_staged_content(repository):
    target = repository / 'large.txt'
    target.write_bytes(b'x' * (1024**2 + 1))
    git(repository, 'add', 'large.txt')
    config = repository / 'config/development-assets.json'
    value = json.loads(config.read_text())
    value['commit']['max_file_bytes'] = 100 * 1024**2
    config.write_text(json.dumps(value))
    assert not cli.check_index(repository, strict=True)['ok']
    git(repository, 'add', 'config/development-assets.json')
    assert cli.check_index(repository, strict=True)['ok']


def test_policy_cannot_be_removed_from_index_while_working_copy_remains(repository):
    git(repository, 'rm', '--cached', 'config/development-assets.json')
    result = cli.check_index(repository)
    assert not result['ok']
    assert any('禁止' in row['reason'] for row in result['errors'])


def test_handoff_subworkspace_cleanup_preserves_junction_target(workspace):
    module_spec = importlib.util.spec_from_file_location('asset_handoff_test', ROOT / 'scripts/check-app-update-handoff.py')
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    module.ROOT = workspace
    external = workspace / 'protected-models'
    external.mkdir()
    sentinel = external / 'keep'
    sentinel.write_text('model')
    with assets.ManagedRun('app-update-handoff', workspace) as run:
        child = run.path / 'parent-waiting'
        child.mkdir()
        link_directory(child / 'shared-models', external)
        module.cleanup_workspace(child, run.path)
        assert not child.exists() and sentinel.read_text() == 'model'


def test_installed_hook_blocks_real_commit_without_changing_index(repository):
    import shutil
    for relative in ['.githooks/pre-commit', 'scripts/workspace-assets.py', 'scripts/workspace_assets.py']:
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    (repository / '.githooks/pre-commit').chmod(0o755)
    runtime = repository / '.local/runtimes'
    runtime.mkdir(parents=True)
    link_directory(runtime / 'python', Path(__import__('sys').executable).parent)
    target = repository / '.local/forced-artifact.txt'
    target.write_text('generated')
    git(repository, 'add', '-f', str(target))
    cli.install_hook(repository)
    before = git(repository, 'ls-files', '--stage', '-z').stdout
    result = subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Asset Test',
                             '-c', 'user.email=asset@example.invalid', 'commit', '-qm', 'must reject'], capture_output=True)
    assert result.returncode != 0
    assert b'forced-artifact.txt' in result.stdout + result.stderr
    assert git(repository, 'ls-files', '--stage', '-z').stdout == before


def test_hook_does_not_block_old_worktree_without_governance_scripts(repository):
    import shutil
    destination = repository / '.githooks/pre-commit'
    destination.parent.mkdir()
    shutil.copy2(ROOT / '.githooks/pre-commit', destination)
    destination.chmod(0o755)
    cli.install_hook(repository)
    (repository / 'source.py').write_text('value = 1')
    git(repository, 'add', 'source.py')
    git(repository, '-c', 'user.name=Asset Test', '-c', 'user.email=asset@example.invalid', 'commit', '-qm', 'normal commit')


def test_broken_governance_script_does_not_prevent_normal_commit(repository):
    import shutil
    destination = repository / '.githooks/pre-commit'
    destination.parent.mkdir()
    shutil.copy2(ROOT / '.githooks/pre-commit', destination)
    destination.chmod(0o755)
    (repository / 'scripts').mkdir()
    (repository / 'scripts/workspace-assets.py').write_text('invalid syntax !!!')
    (repository / 'scripts/workspace_assets.py').write_text('')
    runtime = repository / '.local/runtimes'
    runtime.mkdir(parents=True)
    link_directory(runtime / 'python', Path(__import__('sys').executable).parent)
    cli.install_hook(repository)
    (repository / 'source.py').write_text('value = 1')
    git(repository, 'add', 'source.py')
    git(repository, '-c', 'user.name=Asset Test', '-c', 'user.email=asset@example.invalid', 'commit', '-qm', 'normal commit')

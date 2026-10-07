"""代理安装：仅现用生产状态；受管补丁重复同步保留任务与用户字段。"""
from pathlib import Path
import json
import pytest
from voice_terminal.dsh.profile import migrate_plugin_patch, migrate_schedule_patch


def test_setup_no_longer_copies_environment_or_creates_source_archives():
    script = (Path(__file__).resolve().parents[1] / 'scripts/setup-dsh.ps1').read_text(encoding='utf-8-sig')
    assert 'Save-RuntimeBackup' not in script
    assert 'source.backup' not in script
    assert 'Get-LocalContext' in script
    assert 'voice_terminal.dsh.install' in script


@pytest.mark.parametrize('native_enabled', [False, True])
def test_patch_respects_native_task_selection(tmp_path, native_enabled):
    selected = '- id: schedule\n  disabled: true\n- id: ui-schedule\n  disabled: true\n'
    preserved = '- id: llm\n  config:\n    model: saved-model\n- id: voice-terminal-shadow-bridge\n  name: "@voice-terminal/dsh-shadow-bridge"\n  config:\n    workspaceId: saved-workspace\n'
    patch = tmp_path / 'cordis.patch.yml'
    patch.write_text(preserved + selected, encoding='utf-8')
    for _ in range(2):
        migrate_plugin_patch(patch)
        migrate_schedule_patch(patch, native_enabled=native_enabled)
    assert 'workspaceId: saved-workspace' in patch.read_text(encoding='utf-8')
    assert ('- id: schedule' in patch.read_text(encoding='utf-8')) == native_enabled

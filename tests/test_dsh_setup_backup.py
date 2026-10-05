"""DSH 安装事务：真实备份与恢复保留旧源码、用户任务及功能包。"""
import os
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(os.name != "nt" or not shutil.which("pwsh"), reason="需要 Windows（操作系统）及 PowerShell 7（命令环境）")
def test_setup_backup_restores_data_and_original_source(tmp_path: Path):
    runtime = tmp_path / "runtime"
    for relative, content in {"source/old.txt": "old source", "home/tasks.json": "用户任务", "bundles/plugin.js": "old plugin", "build-state.json": '{"version":"old"}', "session-mappings.json": '{"schema_version":2}'}.items():
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    original = {path.relative_to(runtime): path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
    setup = Path(__file__).resolve().parents[1] / "scripts/setup-dsh.ps1"
    command = r"""
$ErrorActionPreference = 'Stop'
$RuntimeRoot = $env:VOICE_TEST_RUNTIME
$SourceDir = Join-Path $RuntimeRoot 'source'
$SourceArchive = Join-Path $RuntimeRoot 'source.backup-old'
$CurrentHead = 'old-commit'
$taskErrors = $null
$taskTokens = $null
$taskAst = [Management.Automation.Language.Parser]::ParseFile($env:VOICE_TEST_SETUP, [ref]$taskTokens, [ref]$taskErrors)
if ($taskErrors.Count) { throw '安装脚本语法错误' }
$taskNames = @('Write-Utf8NoBom','Assert-RuntimePath','Copy-RuntimeTree','Save-RuntimeBackup','Restore-RuntimeBackup')
foreach ($taskFunction in $taskAst.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
  if ($taskFunction.Name -in $taskNames) { Invoke-Expression $taskFunction.Extent.Text }
}
$taskBackup = Join-Path $RuntimeRoot 'upgrade.backup-test'
Save-RuntimeBackup $taskBackup
Move-Item -LiteralPath $SourceDir -Destination $SourceArchive
New-Item -ItemType Directory -Path $SourceDir | Out-Null
Set-Content -LiteralPath (Join-Path $SourceDir 'new.txt') -Value 'failed new source'
Set-Content -LiteralPath (Join-Path $RuntimeRoot 'home/tasks.json') -Value 'mutated tasks'
Set-Content -LiteralPath (Join-Path $RuntimeRoot 'bundles/plugin.js') -Value 'failed new plugin'
Set-Content -LiteralPath (Join-Path $RuntimeRoot 'session-mappings.json') -Value '{}'
Restore-RuntimeBackup $taskBackup
"""
    result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
                            env={**os.environ, "VOICE_TEST_RUNTIME": str(runtime), "VOICE_TEST_SETUP": str(setup)},
                            text=True, encoding="utf-8", capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all((runtime / relative).read_bytes() == content for relative, content in original.items())
    assert not (runtime / "source/new.txt").exists()
    assert list(runtime.glob("upgrade.failed-*/source/new.txt"))


@pytest.mark.skipif(os.name != "nt" or not shutil.which("pwsh"), reason="需要 Windows（操作系统）及 PowerShell 7（命令环境）")
@pytest.mark.parametrize("native_enabled", [False, True])
def test_setup_patch_respects_native_task_selection(tmp_path: Path, native_enabled: bool):
    """执行真实安装函数，原生功能包启用时保留补丁、关闭时仅迁移旧禁用行。"""
    setup = Path(__file__).resolve().parents[1] / "scripts/setup-dsh.ps1"
    native = "@deepseek-ai/dsh-experimental-schedule-bundle"
    (tmp_path / "package.json").write_text(json.dumps({"dsh": {"profile": {"bundles": [native] if native_enabled else []}}}), encoding="utf-8")
    selected = "- id: schedule\n  disabled: true\n- id: ui-schedule\n  disabled: true\n"
    preserved = "- id: agent-preset-registry\n  config:\n    default: shadow-agent\n- id: llm\n  config:\n    model: saved-model\n"
    target = tmp_path / "cordis.patch.yml"
    target.write_text(preserved + selected, encoding="utf-8")
    command = r"""
$taskAst = [Management.Automation.Language.Parser]::ParseFile($env:VOICE_TEST_SETUP,[ref]$null,[ref]$null)
$taskNames = @('Write-Utf8NoBom','Test-NativeScheduleEnabled','Update-ManagedProfilePatch')
foreach ($taskFunction in $taskAst.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
  if ($taskFunction.Name -in $taskNames) { Invoke-Expression $taskFunction.Extent.Text }
}
$ProfilePackagePath = Join-Path $env:VOICE_TEST_PROFILE 'package.json'
$ProfilePatchPath = Join-Path $env:VOICE_TEST_PROFILE 'cordis.patch.yml'
Update-ManagedProfilePatch $ProfilePatchPath
"""
    result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
                            env={**os.environ, "VOICE_TEST_PROFILE": str(tmp_path), "VOICE_TEST_SETUP": str(setup)},
                            text=True, encoding="utf-8", capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_text(encoding="utf-8") == preserved + (selected if native_enabled else "")

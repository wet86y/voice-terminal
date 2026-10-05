"""DSH profile（网页配置）迁移：保留用户设置和独立的官方会话调度选择。"""
import json
from pathlib import Path

import pytest

from voice_terminal.dsh.profile import (
    BUNDLES, NATIVE_SCHEDULE_BUNDLE, build_profile,
    merge_profile_package, migrate_schedule_patch, native_schedule_enabled,
)


def test_schedule_patch_migration_preserves_settings_and_is_idempotent(tmp_path: Path):
    patch = tmp_path / "cordis.patch.yml"
    settings = "- id: voice-terminal-shadow-bridge\n  config:\n    workspaceId: user-workspace\n"
    defaults = "- id: agent-preset-registry\n  config:\n    default: shadow-agent\n"
    patch.write_text(defaults + "\n- id: schedule\n  disabled: true\n\n"
                     + settings + "\n- id: ui-schedule\n  disabled: true\n", encoding="utf-8")
    migrate_schedule_patch(patch)
    migrated = patch.read_bytes()
    text = patch.read_text(encoding="utf-8")
    assert defaults in text and settings in text
    assert "- id: schedule\n" not in text
    assert "- id: ui-schedule\n" not in text
    migrate_schedule_patch(patch)
    assert patch.read_bytes() == migrated


def test_profile_merge_keeps_user_plugins_and_native_selection():
    managed = {"name": "managed", "dependencies": {"voice-plugin": "0.1.1"},
               "dsh": {"profile": {"bundles": ["base", "voice-plugin"], "patchReload": "live"}}}
    existing = {"dependencies": {"voice-plugin": "0.1.0", "user-plugin": "1.2.3"},
                "dsh": {"profile": {"bundles": ["base", "voice-plugin", NATIVE_SCHEDULE_BUNDLE, "user-plugin"]}}}
    result = merge_profile_package(managed, existing)
    assert result["dependencies"] == {"voice-plugin": "0.1.1", "user-plugin": "1.2.3"}
    assert result["dsh"]["profile"]["bundles"] == ["base", "voice-plugin", NATIVE_SCHEDULE_BUNDLE, "user-plugin"]
    assert existing["dependencies"]["voice-plugin"] == "0.1.0"


def test_schedule_migration_preserves_enabled_native_patch(tmp_path: Path):
    patch = tmp_path / "cordis.patch.yml"
    original = b"- id: schedule\n  disabled: true\n- id: ui-schedule\n  disabled: false\n"
    patch.write_bytes(original)
    migrate_schedule_patch(patch, native_enabled=True)
    assert patch.read_bytes() == original


def test_schedule_migration_preserves_custom_configuration(tmp_path: Path):
    patch = tmp_path / "cordis.patch.yml"
    original = b"- id: schedule\n  disabled: false\n  config:\n    retention: 30\n"
    patch.write_bytes(original)
    migrate_schedule_patch(patch)
    assert patch.read_bytes() == original


def test_schedule_migration_commit_failure_keeps_original_settings(tmp_path: Path, monkeypatch):
    patch = tmp_path / "cordis.patch.yml"
    original = b"- id: llm\n  config:\n    model: saved-model\n- id: schedule\n  disabled: true\n"
    patch.write_bytes(original)

    def reject_commit(source, destination):
        raise OSError("模拟配置提交失败")

    monkeypatch.setattr("voice_terminal.dsh.profile.os.replace", reject_commit)
    with pytest.raises(OSError, match="提交失败"):
        migrate_schedule_patch(patch)
    assert patch.read_bytes() == original
    assert list(tmp_path.iterdir()) == [patch]


def test_managed_profile_updates_bundle_versions_and_migrates_only_old_rows(tmp_path: Path):
    program, bundles, destination = tmp_path / "program", tmp_path / "bundles", tmp_path / "profile"
    (program / "config/dsh").mkdir(parents=True)
    (program / "config/dsh/web-profile.patch.yml").write_text(
        "- id: agent-preset-registry\n  config:\n    default: shadow-agent\n", encoding="utf-8")
    destination.mkdir()
    patch = destination / "cordis.patch.yml"
    patch.write_text("- id: llm\n  config:\n    provider: deepseek-official\n    model: deepseek-flash\n"
                     "- id: schedule\n  disabled: true\n", encoding="utf-8")
    for name in BUNDLES:
        artifact = bundles / name
        artifact.mkdir(parents=True)
        (artifact / "package.json").write_text(json.dumps({"name": "@voice-terminal/" + name, "version": "0.1.1"}), encoding="utf-8")
        (artifact / "plugin.js").write_text("export default {}", encoding="utf-8")
    build_profile(program, bundles, destination)
    package = json.loads((destination / "package.json").read_text())
    assert set(package["dependencies"].values()) == {"0.1.1"}
    assert not native_schedule_enabled(destination)
    assert "model: deepseek-flash" in patch.read_text()
    assert "- id: schedule\n" not in patch.read_text()

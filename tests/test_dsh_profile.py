"""DSH profile（网页配置）迁移：保留用户设置和独立的官方会话调度选择。"""
import json
from pathlib import Path

import pytest

from voice_terminal.dsh.profile import (
    BUNDLES, LEGACY_PACKAGES, PLUGIN_PACKAGE, NATIVE_SCHEDULE_BUNDLE, build_profile,
    merge_profile_package, migrate_plugin_patch, migrate_schedule_patch, native_schedule_enabled,
)


def test_single_bundle_migration_removes_all_legacy_entries_and_keeps_user_choices():
    managed = {"dependencies": {PLUGIN_PACKAGE: "0.2.0"},
               "dsh": {"profile": {"bundles": ["base", PLUGIN_PACKAGE], "patchReload": "live"}}}
    existing = {"dependencies": {**dict.fromkeys(LEGACY_PACKAGES, "0.1.1"), "user-plugin": "3.0"},
                "dsh": {"profile": {"bundles": [*LEGACY_PACKAGES, NATIVE_SCHEDULE_BUNDLE, "user-plugin"], "custom": "保留"}},
                "credentials": {"reference": "用户凭据引用"}}
    migrated = merge_profile_package(managed, existing)
    assert migrated["dependencies"] == {PLUGIN_PACKAGE: "0.2.0", "user-plugin": "3.0"}
    assert migrated["dsh"]["profile"]["bundles"] == ["base", PLUGIN_PACKAGE, NATIVE_SCHEDULE_BUNDLE, "user-plugin"]
    assert migrated["dsh"]["profile"]["custom"] == "保留"
    assert migrated["credentials"] == existing["credentials"]
    assert merge_profile_package(managed, migrated) == migrated


def test_plugin_patch_migration_preserves_settings_comments_and_unrelated_rows(tmp_path: Path):
    path = tmp_path / "cordis.patch.yml"
    original = ('- id: voice-terminal-shadow-bridge\n  name: "@voice-terminal/dsh-shadow-bridge" # 用户设置\n'
                '  config:\n    workspaceId: saved-workspace\n    provider: saved-provider\n'
                "- id: voice-terminal-wake-tasks\n  name: '@voice-terminal/dsh-wake-tasks'\n"
                '- id: user-plugin\n  name: custom-plugin\n')
    path.write_text(original, encoding="utf-8")
    migrate_plugin_patch(path)
    expected = original.replace("@voice-terminal/dsh-shadow-bridge", PLUGIN_PACKAGE).replace("@voice-terminal/dsh-wake-tasks", PLUGIN_PACKAGE + "/wake-tasks")
    assert path.read_text(encoding="utf-8") == expected
    migrate_plugin_patch(path)
    assert path.read_text(encoding="utf-8") == expected


def test_plugin_patch_migration_failure_does_not_modify_original(tmp_path: Path, monkeypatch):
    path = tmp_path / "cordis.patch.yml"
    original = b'- id: voice-terminal-shadow-bridge\n  name: "@voice-terminal/dsh-shadow-bridge"\n'
    path.write_bytes(original)
    def fail(source, destination):
        raise OSError("插件迁移提交失败")
    monkeypatch.setattr("voice_terminal.dsh.profile.os.replace", fail)
    with pytest.raises(OSError, match="迁移提交失败"):
        migrate_plugin_patch(path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


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

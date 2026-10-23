"""强制兼容处理：无效配置默认回退，故障资料归档，重试幂等。"""
import json
from pathlib import Path

import pytest

from voice_terminal.compatibility import repair
from voice_terminal.config import load_config
from voice_terminal.server.product import _settings_validation_warning, apply_settings_to_config


def workspace(tmp_path):
    program, data = tmp_path / "app", tmp_path / "data"
    (program / "config").mkdir(parents=True)
    (program / "config/default.yaml").write_text("{}", encoding="utf-8")
    (data / "config").mkdir(parents=True)
    return program, data


def test_hotwords_and_required_punctuation_migrate_without_resetting_other_settings(tmp_path):
    import yaml
    program, data = workspace(tmp_path)
    local = data / "config/local.yaml"
    original = "asr:\n  punctuation_enabled: false\n  punctuation_device: cpu\n  chunk_size: [0, 8, 4]\n"
    local.write_text(original, encoding="utf-8")
    (data / "config/product-settings.json").write_text(json.dumps({"schema_version": 16, "hotwords": {
        "enabled": False, "threshold": .91, "entries": [{"word": "名称", "aliases": ["别名"], "blacklist": ["排除"]}]}}), encoding="utf-8")
    assert repair(program, data)["ready"]
    settings = json.loads((data / "config/product-settings.json").read_text(encoding="utf-8"))
    assert settings["schema_version"] == 17
    assert settings["hotwords"] == {"enabled": False, "stream_enabled": True, "threshold": .91,
                                      "entries": [{"word": "名称", "aliases": ["别名"], "blacklist": ["排除"]}]}
    assert yaml.safe_load(local.read_text(encoding="utf-8")) == {"asr": {"punctuation_device": "cpu", "chunk_size": [0, 8, 4]}}
    assert any(path.read_text(encoding="utf-8") == original for path in (data / "archives").rglob("local.yaml"))
    assert repair(program, data)["archived_items"] == 0


@pytest.mark.parametrize("raw", [
    "broken JSON", "[]", '{"schema_version":999,"private_preference":"保留原件"}',
    '{"schema_version":16,"wake":{"enabled":"错误"},"tts":{"backend":[]},"speaker":{"confirm_threshold":false}}',
    '{"schema_version":6,"tts":{"backend":"cosyvoice"},"compute":{"devices":{"wespeaker":"cpu"}}}',
])
def test_invalid_settings_are_repaired_persisted_and_original_is_archived(tmp_path, raw):
    program, data = workspace(tmp_path)
    settings = data / "config/product-settings.json"
    settings.write_text(raw, encoding="utf-8")
    result = repair(program, data)
    assert result["ready"] is True
    value = json.loads(settings.read_text(encoding="utf-8"))
    assert _settings_validation_warning(value) is None
    apply_settings_to_config(load_config(program / "config/default.yaml"), value)
    assert value["schema_version"] == 17
    assert any(path.read_text(encoding="utf-8") == raw
               for path in (data / "archives").rglob("product-settings.json"))
    # 默认回退已经落盘；再执行不能再次归档，也不能恢复故障值。
    assert repair(program, data)["archived_items"] == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == value


def test_bad_yaml_profile_and_voice_are_isolated_with_old_active_session(tmp_path):
    program, data = workspace(tmp_path)
    (data / "config/local.yaml").write_text("wake: [broken", encoding="utf-8")
    profile = data / "speaker/campplus-zh-cn-common"
    profile.mkdir(parents=True)
    (profile / "registry.json").write_text("invalid", encoding="utf-8")
    voice = data / "tts/voxcpm2/voices"
    voice.mkdir(parents=True)
    (voice / "index.json").write_text("invalid", encoding="utf-8")
    history = data / "history"
    history.mkdir()
    for suffix in ("", "-wal", "-shm"):
        (history / f"active-session.sqlite3{suffix}").write_bytes(b"old session")
    (history / "ledger-keep.sqlite3").write_bytes(b"history")
    result = repair(program, data)
    assert result["archived_items"] == 6
    archive = data / result["archive"]
    assert (archive / "speaker/campplus-zh-cn-common/registry.json").read_text() == "invalid"
    assert (archive / "tts/voxcpm2/voices/index.json").read_text() == "invalid"
    assert not profile.exists() and not voice.exists()
    assert not (history / "active-session.sqlite3").exists()
    assert (history / "ledger-keep.sqlite3").read_bytes() == b"history"
    assert repair(program, data)["archived_items"] == 0


def test_rebuilt_environment_clears_stale_gpu_settings(tmp_path):
    program, data = workspace(tmp_path)
    (data / "config/product-settings.json").write_text(
        json.dumps({"compute": {"devices": {"paraformer": "cuda"}}}), encoding="utf-8")
    (data / "config/gpu-environment.json").write_text('{"old":"gpu"}', encoding="utf-8")
    result = repair(program, data, reset_devices=True)
    value = json.loads((data / "config/product-settings.json").read_text(encoding="utf-8"))
    assert set(value["compute"]["devices"].values()) == {"cpu"}
    assert not (data / "config/gpu-environment.json").exists()
    assert (data / result["archive"] / "config/gpu-environment.json").is_file()


def test_data_links_are_not_followed_or_archived(tmp_path):
    program, data = workspace(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("outside", encoding="utf-8")
    try:
        (data / "speaker").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("本机未启用创建符号链接权限")
    with pytest.raises(ValueError, match="链接"):
        repair(program, data)
    assert sentinel.read_text() == "outside"


def test_voice_archive_interruption_cannot_keep_a_dangling_selection(tmp_path):
    program, data = workspace(tmp_path)
    selected = {"voice_id": "a" * 32, "revision": 1}
    (data / "config/product-settings.json").write_text(
        json.dumps({"tts": {"active_voice_by_model": {"voxcpm2": selected}}}), encoding="utf-8")
    # 模拟音色目录已归档、设置尚未来得及清理就被中断。
    repair(program, data)
    value = json.loads((data / "config/product-settings.json").read_text(encoding="utf-8"))
    assert value["tts"]["active_voice_by_model"]["voxcpm2"] is None


def test_session_archive_resumes_the_same_group_after_interruption(tmp_path, monkeypatch):
    from voice_terminal.compatibility import Archive
    program, data = workspace(tmp_path)
    history = data / "history"
    history.mkdir()
    for suffix in ("", "-wal", "-shm"):
        (history / f"active-session.sqlite3{suffix}").write_bytes(suffix.encode() or b"main")
    original = Archive.move

    def interrupted(self, relative, reason):
        if relative.endswith("-wal"):
            raise RuntimeError("模拟归档中断")
        return original(self, relative, reason)

    monkeypatch.setattr(Archive, "move", interrupted)
    with pytest.raises(RuntimeError, match="归档中断"):
        repair(program, data)
    group = json.loads((data / "state/compatibility-session-archive.json").read_text())["archive"]
    monkeypatch.setattr(Archive, "move", original)
    repair(program, data)
    assert not (data / "state/compatibility-session-archive.json").exists()
    for suffix in ("", "-wal", "-shm"):
        assert (data / group / f"history/active-session.sqlite3{suffix}").is_file()
        assert not (history / f"active-session.sqlite3{suffix}").exists()


def test_invalid_resource_policy_is_defaulted_before_startup(tmp_path):
    from voice_terminal.resources import ResourcePolicy
    program, data = workspace(tmp_path)
    path = data / "config/runtime-resources.json"
    path.write_text('{"policy_schema":99,"native_threads":{"voice":false}}', encoding="utf-8")
    repair(program, data)
    assert ResourcePolicy.load(path).native_threads == {}


def test_local_tts_rebuilds_default_voices_from_current_program_seed(tmp_path):
    import shutil
    from voice_terminal.tts.voices import VoiceLibrary
    program, data = workspace(tmp_path)
    source = Path(__file__).resolve().parents[1] / "config/resources/assets/tts"
    seed = program / "config/resources/assets/tts"
    seed.mkdir(parents=True)
    for name in ("default.wav", "default.json"):
        shutil.copy2(source / name, seed / name)
    (data / "config/product-settings.json").write_text(
        '{"tts":{"backend":"voxcpm1","local_model":"voxcpm2"}}', encoding="utf-8")
    result = repair(program, data)
    assert result["ready"] is True
    for model in ("voxcpm1", "voxcpm2"):
        library = VoiceLibrary(data / f"tts/{model}/voices", model=model)
        voice = library.resolve(model, library.active_selection)
        assert voice.reference_path.is_file()
    assert repair(program, data)["archived_items"] == 0


def test_incompatible_model_registry_and_bad_environment_are_archived(tmp_path):
    program, data = workspace(tmp_path)
    registry = data / "config/model-registry.json"
    registry.write_text('{"schema_version":999,"providers":["broken"]}', encoding="utf-8")
    (data / ".env.local").write_bytes(b"\xff\xfeinvalid encoding")
    result = repair(program, data)
    value = json.loads(registry.read_text(encoding="utf-8"))
    assert value["schema_version"] == 2 and value["providers"] == []
    assert not (data / ".env.local").exists()
    assert (data / result["archive"] / ".env.local").read_bytes() == b"\xff\xfeinvalid encoding"

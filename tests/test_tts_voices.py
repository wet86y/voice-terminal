"""Versioned voice library（版本化音色库）storage tests."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from voice_terminal.tts.voices import (
    VoiceDeleteConflict,
    VoiceLibrary,
    VoiceLibraryError,
    VoiceRevisionConflict,
)


def _wav_bytes(*, seconds: float = 5.0, sample_rate: int = 44100, channels: int = 2) -> bytes:
    count = round(seconds * sample_rate)
    time = np.arange(count, dtype=np.float32) / sample_rate
    sample = (0.15 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)
    audio = np.repeat(sample[:, None], channels, axis=1) if channels > 1 else sample
    stream = io.BytesIO()
    sf.write(stream, audio, sample_rate, format="WAV", subtype="PCM_16")
    return stream.getvalue()


def _create_voice(library: VoiceLibrary, *, text: str = "测试声音会清楚地朗读这段参考文本。"):
    asset = library.put_import(_wav_bytes(), "sample.wav")
    voice = library.create_voice({
        "name": "测试音色", "source": "import", "reference_asset_id": asset["asset_id"],
        "reference_text": text, "style_description": "语速自然",
    })
    return asset, voice


def test_model_libraries_default_active_version_and_inflight_asset(tmp_path):
    from voice_terminal.tts.voices import ModelVoiceLibraries
    owner = ModelVoiceLibraries(tmp_path / "tts")
    first, second = owner.for_model("voxcpm1"), owner.for_model("voxcpm2")
    for library in (first, second):
        library.register_default(_wav_bytes(), "这是默认音色的准确参考转写。")
    assert first.active_selection != second.active_selection
    with pytest.raises(FileNotFoundError):
        second.resolve("voxcpm2", first.active_selection)
    with pytest.raises(VoiceLibraryError):
        first.resolve("voxcpm2", first.active_selection)
    with pytest.raises(VoiceDeleteConflict):
        first.delete_voice(first.active_selection["voice_id"])
    asset, voice = _create_voice(first)
    first.activate({"voice_id": voice["id"], "revision": 1})
    frozen = first.resolve("voxcpm1", first.active_selection)
    release = first.pin(frozen)
    changed = first.update_voice(voice["id"], 1, {**voice, "name": "新名称"})
    assert first.active_selection == {"voice_id": voice["id"], "revision": 2}
    assert frozen.revision == 1 and frozen.reference_path == first.asset_path(asset["asset_id"])
    with pytest.raises(VoiceRevisionConflict):
        first.update_voice(voice["id"], 1, changed)
    first.delete_voice(voice["id"])
    first.collect_garbage(max_age_seconds=-1)
    assert frozen.reference_path.is_file()
    release()
    first.collect_garbage(max_age_seconds=-1)
    assert not frozen.reference_path.exists()
    assert first.get_voice(first.active_selection["voice_id"])["name"] == "默认音色"


def test_bound_design_candidate_rejects_changed_inputs(tmp_path):
    library = VoiceLibrary(tmp_path / "v2", model="voxcpm2")
    source = library.put_import(_wav_bytes(), "source.wav")
    target = library.put_import(_wav_bytes(seconds=5), "target.wav")
    payload = {"name": "混合音色", "source": "design", "reference_asset_id": target["asset_id"],
               "reference_text": "目标样音正文。", "design_text": "目标样音正文。", "design_description": "温和清晰的女声",
               "source_reference_asset_id": source["asset_id"], "source_reference_text": "源素材校对。", "inference_steps": 20}
    with pytest.raises(VoiceLibraryError, match="重新试音"):
        library.create_voice(payload)
    library.mark_design(target["asset_id"], payload)
    saved = library.create_voice(payload)
    resolved = library.resolve("voxcpm2", {"voice_id": saved["id"], "revision": 1})
    assert resolved.reference_path == library.asset_path(target["asset_id"])
    assert not resolved.style_description
    for field, changed in (("design_description", "另一设计"), ("design_text", "另一正文"),
                           ("source_reference_text", "重新校对"), ("inference_steps", 10),
                           ("source_reference_asset_id", None)):
        with pytest.raises(VoiceLibraryError, match="重新试音"):
            library.update_voice(saved["id"], 1, {**payload, field: changed})
    one = VoiceLibrary(tmp_path / "v1", model="voxcpm1")
    with pytest.raises(VoiceLibraryError, match="仅支持"):
        one.create_voice(payload)
    imported = one.put_import(_wav_bytes(), "v1.wav")
    with pytest.raises(VoiceLibraryError, match="准确"):
        one.create_voice({"name": "无转写", "source": "import", "reference_asset_id": imported["asset_id"]})


def test_import_normalizes_stereo_rate_and_format_and_rejects_bad_sources(tmp_path: Path):
    library = VoiceLibrary(tmp_path / "voices")
    asset = library.put_import(_wav_bytes(), "stereo.wav")
    info = sf.info(library.asset_path(asset["asset_id"]))
    assert info.samplerate == 16000
    assert info.channels == 1
    assert info.subtype == "PCM_16"
    assert 4900 <= asset["duration_ms"] <= 5100
    assert library.asset_bytes(asset["asset_id"]).startswith(b"RIFF")

    with pytest.raises(VoiceLibraryError, match="仅支持"):
        library.put_import(_wav_bytes(), "sample.aac")
    with pytest.raises(VoiceLibraryError, match="严格解码"):
        library.put_import(b"not an audio file", "sample.wav")
    with pytest.raises(VoiceLibraryError, match="5–15"):
        library.put_import(_wav_bytes(seconds=2.5), "short.wav")
    with pytest.raises(OverflowError, match="20 MiB"):
        library.put_import(b"x" * (20 * 1024 * 1024 + 1), "large.wav")


def test_revision_snapshots_are_immutable_and_delete_checks_active_models(tmp_path: Path):
    library = VoiceLibrary(tmp_path / "voices")
    asset, original = _create_voice(library)
    selection = {"voice_id": original["id"], "revision": 1}
    first_snapshot = library.resolve("voxcpm1", selection)

    updated = library.update_voice(original["id"], 1, {
        "name": "测试音色新版", "source": "import", "reference_asset_id": asset["asset_id"],
        "reference_text": "测试声音会清楚地朗读另一段参考文本。", "style_description": "语速更轻快",
    })
    assert updated["revision"] == 2
    assert first_snapshot.revision == 1
    assert library.resolve("voxcpm1", selection).reference_text == original["reference_text"]
    assert library.resolve("voxcpm1", selection).reference_path == first_snapshot.reference_path
    listing = library.list_voices({"voxcpm1": selection})[0]
    assert listing["active_by_model"]["voxcpm1"] == {"revision": 1, "is_latest": False}

    with pytest.raises(VoiceRevisionConflict, match="版本已更新"):
        library.update_voice(original["id"], 1, {
            "name": "过期编辑", "source": "import", "reference_asset_id": asset["asset_id"],
            "reference_text": original["reference_text"], "style_description": "",
        })
    with pytest.raises(VoiceDeleteConflict, match="voxcpm1"):
        library.delete_voice(original["id"], {"voxcpm1": selection})

    library.delete_voice(original["id"])
    assert library.asset_path(asset["asset_id"]).is_file()
    restored = VoiceLibrary(tmp_path / "voices")
    assert restored.list_voices() == []


def test_copy_can_share_an_immutable_asset_and_keeps_it_after_source_delete(tmp_path: Path):
    library = VoiceLibrary(tmp_path / "voices")
    asset, first = _create_voice(library)
    copied = library.create_voice({
        "name": "共享素材副本", "source": "import", "reference_asset_id": asset["asset_id"],
        "reference_text": first["reference_text"], "style_description": first["style_description"],
    })
    assert copied["id"] != first["id"]
    assert copied["reference_asset_id"] == first["reference_asset_id"]
    library.delete_voice(first["id"])
    assert library.asset_path(asset["asset_id"]).is_file()


def test_model_compatibility_and_design_snapshot_have_no_reference_audio(tmp_path: Path):
    library = VoiceLibrary(tmp_path / "voices")
    asset, voice = _create_voice(library, text="")
    for model in ("voxcpm1", "voxcpm2"):
        with pytest.raises(VoiceLibraryError, match="准确转写"):
            library.resolve(model, {"voice_id": voice["id"], "revision": 1})
    assert library.list_voices()[0]["compatibility"]["voxcpm1"]["ready"] is False
    assert library.list_voices()[0]["compatibility"]["voxcpm2"]["ready"] is False

    designed = library.resolve_design("温暖、自然，语速适中")
    assert designed.model == "voxcpm2"
    assert designed.reference_mode == "design"
    assert designed.reference_path is None
    with pytest.raises(VoiceLibraryError, match="圆括号"):
        library.resolve_design("(warm voice)")


def test_corrupt_index_is_preserved_for_repair(tmp_path: Path):
    root = tmp_path / "voices"
    root.mkdir()
    index = root / "index.json"
    index.write_text("{broken", encoding="utf-8")
    with pytest.raises(VoiceLibraryError, match="原文件已保留"):
        VoiceLibrary(root)
    assert index.read_text(encoding="utf-8") == "{broken"


def test_garbage_collection_only_removes_old_unreferenced_assets(tmp_path: Path):
    library = VoiceLibrary(tmp_path / "voices")
    asset, voice = _create_voice(library)
    unreferenced = library.put_import(_wav_bytes(), "loose.wav")
    library.collect_garbage(max_age_seconds=0)
    assert library.asset_path(asset["asset_id"]).is_file()
    with pytest.raises(FileNotFoundError):
        library.asset_path(unreferenced["asset_id"])
    assert library.get_voice(voice["id"], 1)["reference_asset_id"] == asset["asset_id"]


def test_combined_design_keeps_source_and_target_assets_across_reload_and_cleanup(tmp_path: Path):
    library = VoiceLibrary(tmp_path / 'voices')
    source = library.put_import(_wav_bytes(), 'source.wav')
    target = library.put_import(_wav_bytes(seconds=5), 'target.wav')
    voice = library.create_voice({
        'name': '可控克隆', 'source': 'design', 'reference_asset_id': target['asset_id'],
        'source_reference_asset_id': source['asset_id'], 'design_description': '温暖自然',
        'design_text': '目标样音对应的正文。', 'reference_text': '目标样音对应的正文。',
    })
    library.collect_garbage(max_age_seconds=0)
    restored = VoiceLibrary(library.root)
    saved = restored.get_voice(voice['id'])
    assert saved['source_reference_asset_id'] == source['asset_id']
    assert restored.asset_path(source['asset_id']).is_file()
    snapshot = restored.resolve('voxcpm2', {'voice_id': voice['id'], 'revision': 1}, emotion='happy')
    assert snapshot.reference_path == restored.asset_path(target['asset_id'])
    assert snapshot.style_description == '' and snapshot.emotion is None
    assert restored.list_voices()[0]['source_reference_duration_ms'] == source['duration_ms']
    with pytest.raises(FileNotFoundError, match='克隆源'):
        restored.create_voice({**saved, 'source_reference_asset_id': 'f' * 32})


def test_default_seed_repairs_missing_asset_without_changing_voice_selection(tmp_path):
    library = VoiceLibrary(tmp_path / "voices", model="voxcpm1")
    voice = library.register_default(_wav_bytes(), "默认素材准确转写。")
    selected = library.active_selection
    asset = library.asset_path(voice["reference_asset_id"])
    original = asset.read_bytes()
    asset.unlink()
    restored = VoiceLibrary(tmp_path / "voices", model="voxcpm1")
    restored.register_default(_wav_bytes(), "默认素材准确转写。")
    assert restored.active_selection == selected
    assert len(restored.list_voices()) == 1
    assert restored.resolve("voxcpm1", selected).reference_path.read_bytes() == original


def test_default_repair_preserves_custom_active_voice(tmp_path):
    library = VoiceLibrary(tmp_path / "voices", model="voxcpm2")
    default = library.register_default(_wav_bytes(), "默认素材准确转写。")
    _, custom = _create_voice(library)
    selected = {"voice_id": custom["id"], "revision": custom["revision"]}
    library.activate(selected)
    library.asset_path(default["reference_asset_id"]).unlink()
    library.register_default(_wav_bytes(), "默认素材准确转写。")
    assert library.active_selection == selected


def test_missing_modified_default_is_not_replaced_with_unrelated_seed(tmp_path):
    library = VoiceLibrary(tmp_path / "voices", model="voxcpm1")
    voice = library.register_default(_wav_bytes(), "原素材转写。")
    library.asset_path(voice["reference_asset_id"]).unlink()
    with pytest.raises(VoiceLibraryError, match="重新导入原参考音频"):
        library.register_default(_wav_bytes(), "不同素材转写。")


def test_default_repair_does_not_replace_a_user_reference_with_the_same_transcript(tmp_path):
    library = VoiceLibrary(tmp_path / "voices", model="voxcpm1")
    voice = library.register_default(_wav_bytes(), "相同转写。")
    imported = library.put_import(_wav_bytes(seconds=5), "custom.wav")
    library.update_voice(voice["id"], 1, {**voice, "reference_asset_id": imported["asset_id"]})
    library.asset_path(imported["asset_id"]).unlink()
    with pytest.raises(VoiceLibraryError, match="重新导入原参考音频"):
        library.register_default(_wav_bytes(), "相同转写。")

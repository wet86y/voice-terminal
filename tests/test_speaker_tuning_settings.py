"""声纹高级设置：逐项保存、重开、热更新与原子拒绝。"""
from dataclasses import replace
from pathlib import Path
import json

import pytest

from voice_terminal.config import AppConfig
from voice_terminal.server.product import ProductSettingsStore, ProductSettingsError
from voice_terminal.speaker.settings import TUNING_DEFAULTS


@pytest.mark.parametrize("key",list(TUNING_DEFAULTS))
def test_every_parameter_survives_reopen_and_effective_config(tmp_path,key):
    config=AppConfig(model_dir=tmp_path/'models')
    path=tmp_path/'settings.json'
    store=ProductSettingsStore(path=path,config=config)
    default=TUNING_DEFAULTS[key]
    edited=default+1 if type(default) is int else default+.001
    saved=store.save_speaker({"mode":"observe","confirm_threshold":.6,"owner_only_barge_in":False,key:edited})
    assert saved[key]==edited
    reopened=ProductSettingsStore(path=path,config=config)
    assert reopened.settings_only()['speaker'][key]==edited
    assert getattr(reopened.effective_config().speaker,key)==edited


def test_invalid_combination_does_not_partially_write(tmp_path):
    path=tmp_path/'settings.json'
    store=ProductSettingsStore(path=path,config=AppConfig(model_dir=tmp_path/'models'))
    store.save_speaker({"mode":"observe","confirm_threshold":.6,"owner_only_barge_in":False})
    before=path.read_bytes()
    with pytest.raises(ProductSettingsError):
        store.save_speaker({"mode":"filter","confirm_threshold":.1,"owner_only_barge_in":True,
                            "session_weight":.8,"intra_turn_relaxation":.11})
    assert path.read_bytes()==before
    assert store.settings_only()['speaker']['session_weight']==.6


def test_two_relaxation_values_remain_independent_across_save_reload_and_old_client(tmp_path):
    config = AppConfig(model_dir=tmp_path/'models')
    path = tmp_path/'settings.json'
    store = ProductSettingsStore(path=path, config=config)
    store.save_speaker({"mode": "observe", "confirm_threshold": .75, "owner_only_barge_in": False,
                        "continuity_relaxation": .04, "intra_turn_relaxation": .17})
    reopened = ProductSettingsStore(path=path, config=config)
    effective = reopened.effective_config().speaker
    assert effective.intra_turn_threshold == pytest.approx(.58)
    assert effective.continuity_threshold == pytest.approx(.71)
    saved = reopened.save_speaker({"mode": "observe", "confirm_threshold": .75,
                                  "owner_only_barge_in": False, "continuity_relaxation": .03})
    assert saved["intra_turn_relaxation"] == .17
    assert saved["continuity_threshold"] == .72


def test_old_saved_settings_keep_their_previous_intra_behavior_without_rewriting_file(tmp_path):
    config = AppConfig(model_dir=tmp_path/'models')
    path = tmp_path/'settings.json'
    store = ProductSettingsStore(path=path, config=config)
    store.save_speaker({"mode": "observe", "confirm_threshold": .75, "owner_only_barge_in": False,
                        "continuity_relaxation": .14})
    data = json.loads(path.read_text(encoding="utf-8"))
    data["speaker"].pop("intra_turn_relaxation")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    reopened = ProductSettingsStore(path=path, config=config)
    assert reopened.settings_only()["speaker"]["intra_turn_relaxation"] == .14
    assert reopened.effective_config().speaker.intra_turn_threshold == pytest.approx(.61)
    assert path.read_bytes() == before


@pytest.mark.parametrize("key", ["intra_turn_relaxation", "continuity_relaxation"])
def test_negative_derived_threshold_is_rejected_without_mutating_memory_or_disk(tmp_path, key):
    path = tmp_path / "settings.json"
    store = ProductSettingsStore(path=path, config=AppConfig(model_dir=tmp_path / "models"))
    store.save_speaker({"mode": "off", "confirm_threshold": .6, "owner_only_barge_in": False})
    before, settings = path.read_bytes(), store.settings_only()["speaker"]
    with pytest.raises(ProductSettingsError, match="小于0"):
        store.save_speaker({"mode": "observe", "confirm_threshold": .1, "owner_only_barge_in": False,
                            "intra_turn_relaxation": 0, "continuity_relaxation": 0, key: .11})
    assert path.read_bytes() == before
    assert store.settings_only()["speaker"] == settings


@pytest.mark.parametrize("base,intra,cross", [(0, 0, 0), (.2, .2, .2), (.3, .3, .2)])
def test_zero_threshold_and_new_upper_limits_save_and_reload(tmp_path, base, intra, cross):
    path = tmp_path / "settings.json"
    config = AppConfig(model_dir=tmp_path / "models")
    store = ProductSettingsStore(path=path, config=config)
    saved = store.save_speaker({"mode": "off", "confirm_threshold": base, "owner_only_barge_in": False,
                                "intra_turn_relaxation": intra, "continuity_relaxation": cross, "session_weight": .8})
    reopened = ProductSettingsStore(path=path, config=config)
    assert reopened.settings_only()["speaker"] == saved
    assert reopened.effective_config().speaker.intra_turn_threshold == pytest.approx(base-intra)
    assert reopened.effective_config().speaker.continuity_threshold == pytest.approx(base-cross)


def test_only_visible_fields_change_and_retired_threshold_is_ignored(tmp_path):
    store = ProductSettingsStore(path=tmp_path / "settings.json", config=AppConfig())
    before = store.save_speaker({"mode": "off", "confirm_threshold": .6, "owner_only_barge_in": False,
                                 "context_max_ms": 6400, "continuity_window_ms": 4500})
    saved = store.save_speaker({"mode": "off", "confirm_threshold": .25, "owner_only_barge_in": False,
                                "intra_turn_relaxation": .2, "continuity_relaxation": .2,
                                "session_weight": .8, "non_owner_threshold": .99})
    assert "non_owner_threshold" not in saved
    for key in TUNING_DEFAULTS.keys() - {"intra_turn_relaxation", "session_weight"}:
        assert saved[key] == before[key]


def test_old_weight_and_retired_field_do_not_reset_internal_parameters_or_rewrite_on_read(tmp_path):
    config = AppConfig()
    path = tmp_path / "settings.json"
    store = ProductSettingsStore(path=path, config=config)
    store.save_speaker({"mode": "off", "confirm_threshold": .6, "owner_only_barge_in": False, "context_max_ms": 6400})
    data = json.loads(path.read_text(encoding="utf-8"))
    data["speaker"].update(session_weight=.9, non_owner_threshold=.99)
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    reopened = ProductSettingsStore(path=path, config=config)
    assert reopened.effective_config().speaker.session_weight == .9
    assert reopened.effective_config().speaker.context_max_ms == 6400
    assert "non_owner_threshold" not in reopened.settings_only()["speaker"]
    assert path.read_bytes() == before
    reopened.save_speaker({"mode": "observe", "confirm_threshold": .6, "owner_only_barge_in": False})
    assert reopened.settings_only()["speaker"]["session_weight"] == .9
    with pytest.raises(ProductSettingsError, match="临时声纹权重"):
        reopened.save_speaker({"mode": "off", "confirm_threshold": .6, "owner_only_barge_in": False, "session_weight": .81})


def test_validation_uses_final_saved_threshold_precision(tmp_path):
    store = ProductSettingsStore(path=tmp_path / "settings.json", config=AppConfig())
    with pytest.raises(ProductSettingsError, match="小于0"):
        store.save_speaker({"mode": "off", "confirm_threshold": .10004, "owner_only_barge_in": False,
                            "intra_turn_relaxation": .10001, "continuity_relaxation": 0})


def test_old_negative_derived_values_allow_switch_changes_but_require_correction_on_control_save(tmp_path):
    path = tmp_path / "settings.json"
    config = AppConfig()
    store = ProductSettingsStore(path=path, config=config)
    store.save_speaker({"mode": "off", "confirm_threshold": .6, "owner_only_barge_in": False, "context_max_ms": 6400})
    data = json.loads(path.read_text(encoding="utf-8"))
    data["speaker"].update(confirm_threshold=.05, continuity_relaxation=.1, intra_turn_relaxation=.1)
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    reopened = ProductSettingsStore(path=path, config=config)
    assert reopened.effective_config().speaker.intra_turn_threshold == 0
    assert reopened.effective_config().speaker.context_max_ms == 6400
    assert path.read_bytes() == before
    saved = reopened.save_speaker({"mode": "observe", "confirm_threshold": .05, "owner_only_barge_in": False})
    assert saved["continuity_threshold"] == 0 and saved["continuity_relaxation"] == .1
    with pytest.raises(ProductSettingsError, match="小于0"):
        reopened.save_speaker({"mode": "observe", "confirm_threshold": .05, "owner_only_barge_in": False,
                               "intra_turn_relaxation": .1, "continuity_relaxation": .1, "session_weight": .6})

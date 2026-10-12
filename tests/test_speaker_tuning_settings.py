"""声纹高级设置：逐项保存、重开、热更新与原子拒绝。"""
from dataclasses import replace
from pathlib import Path

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
        store.save_speaker({"mode":"filter","confirm_threshold":.6,"owner_only_barge_in":True,
                            "session_weight":.8,"non_owner_threshold":.5})
    assert path.read_bytes()==before
    assert store.settings_only()['speaker']['session_weight']==.6

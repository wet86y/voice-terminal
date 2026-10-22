"""设置并发回归：完整文件写入与字段更新必须作为同一个事务串行执行。"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from voice_terminal.config import AppConfig
from voice_terminal.server.product import ProductSettingsStore


@pytest.mark.parametrize("first_fails", [False, True])
def test_concurrent_settings_writes_preserve_both_sections(tmp_path, monkeypatch, first_fails):
    from voice_terminal.server import product
    store = ProductSettingsStore(path=tmp_path / "settings.json", config=AppConfig())
    previous_device = store.settings_only()["compute"]["devices"]["paraformer"]
    original_write = product._atomic_write_json
    first_entered, release_first, second_started, second_write = (threading.Event() for _ in range(4))

    def write(path, value):
        if not first_entered.is_set():
            first_entered.set()
            assert release_first.wait(5)
            if first_fails:
                raise OSError("模拟计算设置保存失败")
        else:
            second_write.set()
        original_write(path, value)

    def save_speaker():
        second_started.set()
        return store.save_speaker({"mode": "observe", "confirm_threshold": 0.6, "owner_only_barge_in": False})

    monkeypatch.setattr(product, "_atomic_write_json", write)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="settings") as pool:
        first = pool.submit(store.save_compute_model, "paraformer", "cuda", active_device="cuda")
        try:
            assert first_entered.wait(5)
            second = pool.submit(save_speaker)
            assert second_started.wait(5)
            # 第一份快照尚未落盘时，第二项保存不能进入完整文件写入。
            assert not second_write.wait(0.1)
        finally:
            release_first.set()
        if first_fails:
            with pytest.raises(OSError, match="模拟计算设置保存失败"):
                first.result(timeout=5)
        else:
            first.result(timeout=5)
        second.result(timeout=5)

    disk = json.loads(store.path.read_text(encoding="utf-8"))
    memory = store.settings_only()
    assert disk == memory
    assert disk["speaker"]["mode"] == "observe"
    assert disk["compute"]["devices"]["paraformer"] == (previous_device if first_fails else "cuda")

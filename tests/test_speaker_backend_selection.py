"""可选声纹后端贯通：模型选择、前端指纹及旧资料隔离。"""
from pathlib import Path
import asyncio
import threading
from types import SimpleNamespace

import pytest

from voice_terminal.config import AppConfig, SpeakerConfig
from voice_terminal.server.speaker_runtime import SpeakerRuntimeMixin
from voice_terminal.speaker.engine import WeSpeakerOnnxEngine
from voice_terminal.speaker.native import CamPlusPlusFunAsrEngine
from voice_terminal.speaker.features import CAMPLUS_FRONTEND_VERSION, FRONTEND_VERSION
from voice_terminal.speaker.registry import SpeakerProfileStore
from voice_terminal.speaker.worker import SpeakerWorkerEngine


@pytest.mark.parametrize("override,expected", [(None, "cpu"), ("cuda", "cuda")])
def test_worker_entry_reads_device_override_before_model_load(tmp_path, monkeypatch, override, expected):
    """走真实工作进程入口，防止设备环境变量读取在模型加载前崩溃。"""
    from voice_terminal.speaker import worker
    config = AppConfig(model_dir=tmp_path / "models")
    monkeypatch.setattr(worker, "load_config", lambda *_: config)
    monkeypatch.setattr(worker.AppPaths, "resolve", lambda _: SimpleNamespace(config=tmp_path))
    monkeypatch.setattr(worker, "ProductSettingsStore", lambda **_: SimpleNamespace(effective_config=lambda: config))
    listener = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(worker, "bind_loopback", lambda *args, **kwargs: (listener, 0))
    if override is None:
        monkeypatch.delenv("VOICE_MODEL_DEVICE", raising=False)
    else:
        monkeypatch.setenv("VOICE_MODEL_DEVICE", override)
    selected = {}

    class LoadReached(Exception):
        """LoadReached（已到达加载阶段）：停止测试，避免加载真实模型。"""

    def build_engine(model_path, **kwargs):
        selected.update(path=model_path, **kwargs)
        def load():
            raise LoadReached()
        return SimpleNamespace(load=load)

    monkeypatch.setattr(worker, "SpeakerWorkerEngine", build_engine)
    with pytest.raises(LoadReached):
        asyncio.run(worker.run())
    assert selected["device"] == expected
    assert selected["path"] == config.speaker_model_path


@pytest.mark.parametrize("model_id,engine_type,frontend", [
    ("wespeaker-cnceleb-resnet34", WeSpeakerOnnxEngine, FRONTEND_VERSION),
    ("campplus-zh-cn-common", CamPlusPlusFunAsrEngine, CAMPLUS_FRONTEND_VERSION),
])
def test_worker_selects_configured_backend(model_id, engine_type, frontend):
    worker = SpeakerWorkerEngine(Path("unused.onnx"), model_id=model_id)
    assert type(worker.model) is engine_type
    assert worker.model.frontend_version == frontend


@pytest.mark.parametrize("model_id,engine_type,frontend,dimension", [
    ("wespeaker-cnceleb-resnet34", WeSpeakerOnnxEngine, FRONTEND_VERSION, 256),
    ("campplus-zh-cn-common", CamPlusPlusFunAsrEngine, CAMPLUS_FRONTEND_VERSION, 192),
])
def test_local_fallback_fingerprint_comes_from_selected_engine(
        tmp_path, monkeypatch, model_id, engine_type, frontend, dimension):
    # 仅替代原生模型加载；工厂、配置路径及资料指纹走真实代码。
    monkeypatch.setattr(engine_type, "load", lambda engine: setattr(engine, "session", object()))
    monkeypatch.setattr(engine_type, "probe_embedding", lambda _: (1.0,) + (0.0,) * (dimension - 1))
    runtime = object.__new__(SpeakerRuntimeMixin)
    runtime.config = AppConfig(model_dir=tmp_path, speaker=SpeakerConfig(model_id=model_id))
    runtime._speaker_engine = None
    runtime._speaker_engine_lock = threading.RLock()

    selected = runtime._get_speaker_engine()

    assert type(selected) is engine_type
    assert selected.model_path == runtime.config.speaker_model_path
    assert runtime._speaker_runtime_fingerprint() == {
        "frontend_version": frontend, "model_id": model_id, "embedding_dim": dimension,
    }


def test_model_switch_does_not_reuse_or_overwrite_existing_profile(tmp_path):
    profile = SpeakerProfileStore(tmp_path, model_id="wespeaker-cnceleb-resnet34")
    profile.replace_templates(
        [("原注册", (1.0, 0.0), {"speech_ms": 5000})],
        fingerprint={"model_id": "wespeaker-cnceleb-resnet34", "frontend_version": FRONTEND_VERSION},
        probe=(1.0, 0.0),
    )
    original = profile.path.read_bytes()

    candidate = SpeakerProfileStore(tmp_path, model_id="campplus-zh-cn-common")
    assert not candidate.available
    assert candidate.warning is not None
    assert profile.path.read_bytes() == original
    assert SpeakerProfileStore(tmp_path, model_id="wespeaker-cnceleb-resnet34").available

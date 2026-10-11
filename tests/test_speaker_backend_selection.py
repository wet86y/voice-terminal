"""可选声纹后端贯通：模型选择、前端指纹及旧资料隔离。"""
from pathlib import Path
import threading

import pytest

from voice_terminal.config import AppConfig, SpeakerConfig
from voice_terminal.server.speaker_runtime import SpeakerRuntimeMixin
from voice_terminal.speaker.engine import CamPlusPlusOnnxEngine, WeSpeakerOnnxEngine
from voice_terminal.speaker.features import CAMPLUS_FRONTEND_VERSION, FRONTEND_VERSION
from voice_terminal.speaker.registry import SpeakerProfileStore
from voice_terminal.speaker.worker import SpeakerWorkerEngine


@pytest.mark.parametrize("model_id,engine_type,frontend", [
    ("wespeaker-cnceleb-resnet34", WeSpeakerOnnxEngine, FRONTEND_VERSION),
    ("campplus-zh-cn-common", CamPlusPlusOnnxEngine, CAMPLUS_FRONTEND_VERSION),
])
def test_worker_selects_configured_backend(model_id, engine_type, frontend):
    worker = SpeakerWorkerEngine(Path("unused.onnx"), model_id=model_id)
    assert type(worker.model) is engine_type
    assert worker.model.frontend_version == frontend


@pytest.mark.parametrize("model_id,engine_type,frontend,dimension", [
    ("wespeaker-cnceleb-resnet34", WeSpeakerOnnxEngine, FRONTEND_VERSION, 256),
    ("campplus-zh-cn-common", CamPlusPlusOnnxEngine, CAMPLUS_FRONTEND_VERSION, 192),
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

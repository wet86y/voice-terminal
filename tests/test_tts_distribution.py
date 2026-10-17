"""TTS（语音合成）分发与启动任选条件回归。"""
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import zipfile

import pytest

from voice_terminal.config import AppConfig
from voice_terminal.providers.audio_cpp_tts import AudioCppTtsServer
from voice_terminal.resource_distribution import ResourceCatalog, ResourceManager
from voice_terminal.server.websocket import SessionRuntime


def _legacy_model_catalog(program: Path, content: bytes) -> Path:
    """program（测试程序目录）：为微型旧权重提供真实清单，避免使用完整模型文件。"""
    rows = []
    for name, folder, filename in [("voxcpm1", "VoxCPM1-GGUF", "voxcpm-0.5b-q8_0-audiovae-f16.gguf"),
                                   ("voxcpm2", "VoxCPM2-GGUF", "voxcpm2-q8_0.gguf")]:
        rows.append({"resource_id": f"tts-{name}", "kind": "model", "title": "测试语音模型", "destination": f"audiocpp/tts/{folder}",
                     "version": "1", "files": [{"path": filename, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}]})
    path = program / "config/resources/catalog.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "resources": rows}), encoding="utf-8")
    return program


@pytest.mark.parametrize("backend,selected,models,online,expected", [
    ("minimax", "voxcpm2", ["voxcpm1"], False, ("voxcpm1", "voxcpm1")),
    ("voxcpm1", "voxcpm2", ["voxcpm1"], False, ("voxcpm1", "voxcpm1")),
    ("voxcpm1", "voxcpm1", ["voxcpm1", "voxcpm2"], True, ("voxcpm1", "voxcpm1")),
    ("voxcpm1", "voxcpm2", [], True, ("minimax", "voxcpm2")),
    ("minimax", "voxcpm2", ["voxcpm1"], True, ("minimax", "voxcpm2")),
])
def test_startup_uses_configured_or_available_tts(tmp_path, monkeypatch, backend, selected, models, online, expected):
    config = AppConfig()
    config = replace(config, tts=replace(config.tts, backend=backend, local_model=selected))
    runtime = SessionRuntime(config, runtime_data_dir=tmp_path / "data")
    monkeypatch.setattr(AudioCppTtsServer, "available_models", lambda root: models)
    runtime._api_key = lambda name: "configured-test-key" if online else ""
    runtime._select_available_tts()
    assert (runtime.config.tts.backend, runtime.config.tts.local_model) == expected


def test_startup_without_any_tts_reports_actionable_error(tmp_path, monkeypatch):
    runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "data")
    monkeypatch.setattr(AudioCppTtsServer, "available_models", lambda root: [])
    runtime._api_key = lambda name: ""
    with pytest.raises(RuntimeError, match="安装任一本地模型"):
        runtime._select_available_tts()


def test_model_availability_includes_native_runtime_and_specs(tmp_path):
    program = _legacy_model_catalog(tmp_path / "program", b"weight")
    weight = tmp_path / "models/audiocpp/tts/VoxCPM1-GGUF/voxcpm-0.5b-q8_0-audiovae-f16.gguf"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"weight")
    assert AudioCppTtsServer.available_models(tmp_path, program_root=program) == []
    binary = tmp_path / "models/audiocpp/runtime/cuda/audiocpp_server.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    specs = binary.parent.parent / "model_specs/voxcpm1.json"
    specs.parent.mkdir()
    specs.write_text("{}")
    assert AudioCppTtsServer.available_models(tmp_path, program_root=program) == ["voxcpm1"]


@pytest.mark.parametrize("other_available", [True, False])
@pytest.mark.parametrize("bad_content", [b"bad!", b"x"])
def test_legacy_corrupt_selected_model_falls_back_to_valid_model_or_online(tmp_path, monkeypatch, other_available, bad_content):
    program = _legacy_model_catalog(tmp_path / "program", b"good")
    native = tmp_path / "models/audiocpp/runtime"
    (native / "cuda").mkdir(parents=True)
    (native / "cuda/audiocpp_server.exe").write_bytes(b"binary")
    (native / "model_specs").mkdir()
    for name, folder, filename in [("voxcpm1", "VoxCPM1-GGUF", "voxcpm-0.5b-q8_0-audiovae-f16.gguf"),
                                   ("voxcpm2", "VoxCPM2-GGUF", "voxcpm2-q8_0.gguf")]:
        (native / f"model_specs/{name}.json").write_text("{}")
        target = tmp_path / "models/audiocpp/tts" / folder / filename
        target.parent.mkdir(parents=True)
        if name == "voxcpm2" or other_available:
            target.write_bytes(bad_content if name == "voxcpm2" else b"good")
    available = AudioCppTtsServer.available_models
    monkeypatch.setattr(AudioCppTtsServer, "available_models", lambda root: available(root, program_root=program))
    config = AppConfig()
    runtime = SessionRuntime(replace(config, tts=replace(config.tts, backend="voxcpm1", local_model="voxcpm2")), runtime_data_dir=tmp_path / "data")
    runtime._api_key = lambda name: "configured-test-key"
    runtime._select_available_tts()
    assert runtime.config.tts.backend == ("voxcpm1" if other_available else "minimax")
    if other_available:
        assert runtime.config.tts.local_model == "voxcpm1"


def test_distribution_locks_optional_models_and_bundled_reference():
    root = Path(__file__).resolve().parents[1]
    catalog = ResourceCatalog(root)
    for name in ("voxcpm1", "voxcpm2"):
        spec = catalog.get(f"tts-{name}")
        assert spec.required is False
        assert len(spec.source["revision"]) == 40
        assert spec.required_files[0].source_path.startswith(f"VoxCPM{'1' if name == 'voxcpm1' else '2'}-GGUF/")
    assert (root / "config/resources/assets/tts/default.wav").is_file()
    assert (root / "config/resources/assets/tts/default.json").is_file()


def test_official_runtime_plan_includes_archives_and_bundled_patch(tmp_path):
    from voice_terminal.local_layout import default_portable
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "portable.json").write_text(json.dumps(default_portable()), encoding="utf-8")
    manager = ResourceManager(root, tmp_path)
    plan = manager.download_plan("tts-audiocpp-runtime", "distribution-plan")
    assert plan["transport"] == "native_official_audiocpp_patch"
    assert len(plan["files"]) == 2
    assert sum(row["size_bytes"] for row in plan["files"]) == 849397937
    patch = plan["generated_files"][0]
    data = (root / "config/resources" / patch["asset"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == patch["sha256"]
    assert patch["size_bytes"] == len(data)
    with zipfile.ZipFile(root / "config/resources" / patch["asset"]) as archive:
        assert "source.patch" in archive.namelist()
        assert "cuda/audiocpp_server.exe" in archive.namelist()
        assert "model_specs/voxcpm2.json" in archive.namelist()
        assert "model_specs/voxcpm1.json" in archive.namelist()
        voxcpm2 = json.loads(archive.read("model_specs/voxcpm2.json"))
        assert any(option["name"] == "voxcpm2.full_decode" for option in voxcpm2["options"]["request"])
        records = next(item for item in json.loads((root / "config/resources/catalog.json").read_text("utf-8"))["resources"]
                       if item["resource_id"] == "tts-audiocpp-runtime")["files"]
        for item in records:
            if item.get("archive_id") == "patch":
                assert hashlib.sha256(archive.read(item["source_path"])).hexdigest() == item["sha256"]
        native = next(row for row in json.loads((root / "config/resources/catalog.json").read_text("utf-8"))["resources"]
                      if row["resource_id"] == "tts-audiocpp-runtime")
        for item in native["files"]:
            if item.get("archive_id") == "patch":
                content = archive.read(item["source_path"])
                assert len(content) == item["size_bytes"]
                assert hashlib.sha256(content).hexdigest() == item["sha256"]


@pytest.mark.parametrize("script,hash_field", [
    ("prepare-audiocpp-patch.py", "SERVER_SHA256"),
    ("migrate-local-tts.py", "CUDA_SERVER_SHA256"),
])
def test_native_build_and_migration_pins_match_distributed_runtime(script, hash_field):
    """构建准备、旧资源迁入和客户机分发必须绑定同一份已验证原生程序。"""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    native = ResourceCatalog(root).get("tts-audiocpp-runtime")
    server = next(item for item in native.required_files if item.path == "cuda/audiocpp_server.exe")
    module_spec = importlib.util.spec_from_file_location("native_resource_pin", root / "scripts" / script)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    assert module.NATIVE_REVISION == native.source["native_revision"]
    assert getattr(module, hash_field) == server.sha256


def test_same_size_corrupt_installed_model_is_not_a_startup_candidate(tmp_path):
    from voice_terminal.model_installation import model_receipt
    program = _legacy_model_catalog(tmp_path / "program", b"good")
    weight = tmp_path / "models/audiocpp/tts/VoxCPM1-GGUF/voxcpm-0.5b-q8_0-audiovae-f16.gguf"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"good")
    receipt = model_receipt("tts-voxcpm1", "1", [{"path": weight.name, "size": 4, "sha256": hashlib.sha256(b"good").hexdigest()}])
    (weight.parent / "resource-install.json").write_text(json.dumps(receipt), encoding="utf-8")
    runtime = tmp_path / "models/audiocpp/runtime"
    (runtime / "cuda").mkdir(parents=True)
    (runtime / "cuda/audiocpp_server.exe").write_bytes(b"binary")
    (runtime / "model_specs").mkdir()
    (runtime / "model_specs/voxcpm1.json").write_text("{}")
    assert AudioCppTtsServer.available_models(tmp_path, program_root=program) == ["voxcpm1"]
    weight.write_bytes(b"bad!")
    assert AudioCppTtsServer.available_models(tmp_path, program_root=program) == []


@pytest.mark.parametrize("receipt_version,content,corrupt_file", [
    ("old", b"old-native", None), ("current", b"old-native", None), (None, b"old-native", None), ("current", b"new-native", None),
    ("current", b"new-native", "cuda/audiocpp_server.exe"), ("current", b"new-native", "model_specs/voxcpm1.json"), ("current", b"new-native", "cuda/base.dll"),
])
def test_local_tts_rejects_self_consistent_but_outdated_native_runtime(tmp_path, receipt_version, content, corrupt_file, caplog):
    """旧收据与旧程序互相一致时仍禁止启动，覆盖现场新版应用搭配旧组件的回归。"""
    from voice_terminal.model_installation import model_receipt
    program = _legacy_model_catalog(tmp_path / "program", b"weight")
    catalog_path = program / "config/resources/catalog.json"
    catalog = json.loads(catalog_path.read_text("utf-8"))
    files = {"cuda/audiocpp_server.exe": b"new-native", "model_specs/voxcpm1.json": b"{}", "cuda/base.dll": b"base"}
    catalog["resources"].append({"resource_id": "tts-audiocpp-runtime", "kind": "model", "title": "语音运行程序", "destination": "audiocpp/runtime",
                                 "version": "current", "files": [{"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()]})
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    native = tmp_path / "models/audiocpp/runtime"
    files["cuda/audiocpp_server.exe"] = content
    for name, data in files.items():
        path = native / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if receipt_version:
        receipt = model_receipt("tts-audiocpp-runtime", receipt_version, [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()])
        (native / "resource-install.json").write_text(json.dumps(receipt), encoding="utf-8")
    if corrupt_file:
        path = native / corrupt_file
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    weight = tmp_path / "models/audiocpp/tts/VoxCPM1-GGUF/voxcpm-0.5b-q8_0-audiovae-f16.gguf"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"weight")
    expected = ["voxcpm1"] if content == b"new-native" and not corrupt_file else []
    assert AudioCppTtsServer.available_models(tmp_path, program_root=program) == expected
    if not expected:
        assert "runtime_update_required" in caplog.text


def test_native_process_gate_uses_installed_program_context_before_spawning(tmp_path, monkeypatch):
    from voice_terminal.providers.audio_cpp_tts import AudioCppTtsError
    program = _legacy_model_catalog(tmp_path / "program", b"weight")
    monkeypatch.setenv("VOICE_PROGRAM_ROOT", str(program))
    assert AudioCppTtsServer._catalog_program() == program
    server = AudioCppTtsServer.__new__(AudioCppTtsServer)
    server.runtime_root = tmp_path
    monkeypatch.setattr(AudioCppTtsServer, "_runtime_update_error", classmethod(lambda cls, root, **kwargs: "原生组件需要更新"))
    monkeypatch.setattr("voice_terminal.providers.audio_cpp_tts.subprocess.Popen", lambda *args, **kwargs: pytest.fail("过期组件不得启动"))
    with pytest.raises(AudioCppTtsError, match="原生组件需要更新"):
        server._start_process()


def test_bundled_seed_initializes_both_libraries(tmp_path):
    from voice_terminal.paths import AppPaths
    root = Path(__file__).resolve().parents[1]
    config = replace(AppConfig(), paths=AppPaths(root, tmp_path, tmp_path / "data"))
    runtime = SessionRuntime(config, runtime_data_dir=tmp_path / "data")
    runtime._migrate_legacy_default_voice()
    for name in ("voxcpm1", "voxcpm2"):
        assert runtime._active_voice_snapshot(name).reference_path.is_file()
    before = runtime._active_voices_by_model()
    runtime._migrate_legacy_default_voice()
    assert runtime._active_voices_by_model() == before

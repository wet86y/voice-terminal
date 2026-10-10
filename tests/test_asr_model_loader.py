"""ASR（语音识别）/模型加载适配的纯轻量测试。"""
import builtins
import hashlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from voice_terminal.asr import model_loader


def test_package_keeps_lazy_exports_without_eager_scan():
    source = '''
events = []
def import_submodules(name):
    events.append(name)
import_submodules(__name__)
def __getattr__(name):
    return "lazy:" + name
'''
    scope = {"__name__": "funasr"}
    exec(model_loader._entry_code(source, "entry.py"), scope)
    assert scope["events"] == []
    assert scope["__getattr__"]("AutoModel") == "lazy:AutoModel"
    scope["import_submodules"]("fallback")
    assert scope["events"] == ["fallback"]


def test_speaker_dependencies_are_deferred_but_keep_original_behavior():
    source = '''
try:
    from funasr.models.campplus.utils import sv_chunk, postprocess, distribute_spk
    from funasr.models.campplus.cluster_backend import ClusterBackend
except:
    pass
class AutoModel:
    def __init__(self, **kwargs):
        spk_model = kwargs.get("spk_model")
        self.value = kwargs["value"] * 2
        if spk_model is not None:
            self.value = ClusterBackend(self.value)
    def speaker_result(self):
        return postprocess(self.value)
'''
    calls = []

    def imports(name, *args, **kwargs):
        calls.append(name)
        return SimpleNamespace(sv_chunk=lambda value: value, distribute_spk=lambda value: value,
                               postprocess=lambda value: value + 1, ClusterBackend=lambda value: value + 10)

    scope = {"__builtins__": {**vars(builtins), "__import__": imports}}
    exec(model_loader._model_code(source, "auto.py"), scope)
    assert calls == []
    assert scope["AutoModel"](value=3).value == 6
    assert calls == []
    speaker = scope["AutoModel"](value=3, spk_model="enabled")
    assert speaker.value == 16
    assert speaker.speaker_result() == 17
    assert len(calls) == 2


def test_mmap_candidate_avoids_deepcopy_and_keeps_file_and_stream_paths():
    source = '''
import copy
def load_pretrained_model(path, model, map_location="cpu", oss_bucket=None):
    if oss_bucket is None:
        ori_state = torch.load(path, map_location=map_location)
    else:
        buffer = BytesIO(oss_bucket.get_object(path).read())
        ori_state = torch.load(buffer, map_location=map_location)
    src_state = copy.deepcopy(ori_state)
    return src_state
'''
    calls = []
    checkpoint = {"weight": object()}

    class FakeTorch:
        @staticmethod
        def load(path, **kwargs):
            calls.append((path, kwargs))
            return checkpoint

    namespace = {"torch": FakeTorch, "logging": logging,
                 "BytesIO": lambda value: ("stream", value)}
    exec(model_loader._pretrained_loader_code(source, "load.py"), namespace)
    assert namespace["load_pretrained_model"]("weights.pt", object()) is checkpoint
    assert calls == [("weights.pt", {"map_location": "cpu", "mmap": True})]
    assert namespace["load_pretrained_model"].__voice_model_load_mode__ == "mmap"

    stream = SimpleNamespace(get_object=lambda _: SimpleNamespace(read=lambda: b"weights"))
    calls.clear()
    assert namespace["load_pretrained_model"]("remote-key", object(), oss_bucket=stream) is checkpoint
    assert calls == [(('stream', b"weights"), {"map_location": "cpu"})]


def test_mmap_candidate_falls_back_only_for_mmap_unsupported_errors():
    source = '''
import copy
def load_pretrained_model(path, model, map_location="cpu", oss_bucket=None):
    if oss_bucket is None:
        ori_state = torch.load(path, map_location=map_location)
    else:
        buffer = BytesIO(oss_bucket.get_object(path).read())
        ori_state = torch.load(buffer, map_location=map_location)
    src_state = copy.deepcopy(ori_state)
    return src_state
'''
    calls = []

    class FakeTorch:
        @staticmethod
        def load(path, **kwargs):
            calls.append(kwargs)
            if kwargs.get("mmap"):
                raise RuntimeError("mmap can only be used with zip serialization")
            return {"ok": True}

    namespace = {"torch": FakeTorch, "logging": logging,
                 "BytesIO": lambda value: ("stream", value)}
    exec(model_loader._pretrained_loader_code(source, "load.py"), namespace)
    assert namespace["load_pretrained_model"]("legacy.pt", object()) == {"ok": True}
    assert calls == [{"map_location": "cpu", "mmap": True}, {"map_location": "cpu"}]


def test_mmap_candidate_does_not_hide_mapping_or_checkpoint_failures():
    source = '''
import copy
def load_pretrained_model(path, model, map_location="cpu", oss_bucket=None):
    if oss_bucket is None:
        ori_state = torch.load(path, map_location=map_location)
    else:
        buffer = BytesIO(oss_bucket.get_object(path).read())
        ori_state = torch.load(buffer, map_location=map_location)
    src_state = copy.deepcopy(ori_state)
    return src_state
'''
    calls = []

    class FakeTorch:
        @staticmethod
        def load(path, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("mmap could not reserve address space")

    namespace = {"torch": FakeTorch, "logging": logging,
                 "BytesIO": lambda value: ("stream", value)}
    exec(model_loader._pretrained_loader_code(source, "load.py"), namespace)
    try:
        namespace["load_pretrained_model"]("weights.pt", object())
    except RuntimeError as error:
        assert "reserve address space" in str(error)
    else:
        raise AssertionError("真实 mmap 映射错误不得静默退回常规加载")
    assert calls == [{"map_location": "cpu", "mmap": True}]


def _write_config(path: Path, text: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(text, encoding="utf-8")
    return path


def _registering_package_importer(package, register_module, registries, imported):
    def importer(name):
        imported.append(name)
        if name == "funasr":
            return package
        if name == "funasr.register":
            return register_module
        for class_name, registry_name, module_name in model_loader._COMPONENTS.values():
            if module_name == name:
                registries[registry_name][class_name] = type(class_name, (), {"__module__": module_name})
        return SimpleNamespace(__name__=name)
    return importer


def test_known_configs_merge_sensevoice_and_fsmn_components(tmp_path):
    sensevoice = _write_config(tmp_path / "sensevoice", """
model: SenseVoiceSmall
encoder: SenseVoiceEncoderSmall
tokenizer: SentencepiecesTokenizer
frontend: WavFrontend
specaug: SpecAugLFR
""")
    vad = _write_config(tmp_path / "vad", """
model: FsmnVADStreaming
encoder: FSMN
frontend: WavFrontendOnline
""")
    selected = model_loader._requested_components((sensevoice, vad))
    assert selected is not None
    assert "sensevoice_model" in selected
    assert "fsmn_vad_model" in selected
    assert selected.count("online_frontend") == 1


def test_existing_paraformer_config_remains_selectively_supported(tmp_path):
    paraformer = _write_config(tmp_path / "paraformer", """
model: ParaformerStreaming
encoder: SANMEncoderChunkOpt
decoder: ParaformerSANMDecoder
predictor: CifPredictorV2
frontend: WavFrontendOnline
tokenizer: CharTokenizer
specaug: SpecAugLFR
""")
    assert model_loader._supported_model(paraformer)
    assert "paraformer_decoder" in model_loader._config_components(paraformer)


def test_existing_package_gets_vad_registration_after_sensevoice(tmp_path, monkeypatch):
    sensevoice = _write_config(tmp_path / "sensevoice", """
model: SenseVoiceSmall
encoder: SenseVoiceEncoderSmall
tokenizer: SentencepiecesTokenizer
frontend: WavFrontend
specaug: SpecAugLFR
""")
    vad = _write_config(tmp_path / "vad", """
model: FsmnVADStreaming
encoder: FSMN
frontend: WavFrontendOnline
""")
    registries = {name: {} for _, name, _ in model_loader._COMPONENTS.values()}
    register_module = SimpleNamespace(tables=SimpleNamespace(**{
        name: values for name, values in registries.items()
    }))
    package = SimpleNamespace(AutoModel=object(), __name__="funasr", __path__=[])
    monkeypatch.setitem(sys.modules, "funasr", package)
    monkeypatch.setenv("VOICE_ASR_IMPORT_MODE", "selective")
    monkeypatch.setenv("VOICE_MODEL_LOAD_MODE", "original")
    monkeypatch.setattr(model_loader, "_verified_root", lambda: tmp_path)
    monkeypatch.setattr(model_loader, "_compile_codes", lambda *args, **kwargs: ({}, None, None))
    imported = []
    monkeypatch.setattr(model_loader.importlib, "import_module",
                        _registering_package_importer(package, register_module, registries, imported))

    assert model_loader.auto_model_class(sensevoice) is package.AutoModel
    assert "funasr.models.sense_voice.model" in imported
    assert "funasr.models.fsmn_vad_streaming.model" not in imported

    imported.clear()
    assert model_loader.auto_model_class(vad) is package.AutoModel
    assert "funasr.models.fsmn_vad_streaming.model" in imported
    assert "funasr.models.fsmn_vad_streaming.encoder" in imported


def test_unknown_config_runs_full_scan_even_when_package_is_already_loaded(tmp_path, monkeypatch):
    _write_config(tmp_path, "model: AnotherModel\n")
    package = SimpleNamespace(AutoModel=object(), __name__="funasr", __path__=[])
    package.import_submodules = Mock()
    monkeypatch.setitem(sys.modules, "funasr", package)
    monkeypatch.setattr(model_loader, "_verified_root", lambda: tmp_path)
    monkeypatch.setenv("VOICE_ASR_IMPORT_MODE", "selective")
    monkeypatch.setenv("VOICE_MODEL_LOAD_MODE", "original")
    assert model_loader.auto_model_class(tmp_path) is package.AutoModel
    package.import_submodules.assert_called_once_with(package, recursive=True)


def test_explicit_full_import_does_not_read_model_config(tmp_path, monkeypatch):
    package = SimpleNamespace(AutoModel=object(), __name__="funasr", __path__=[])
    package.import_submodules = Mock()
    monkeypatch.setitem(sys.modules, "funasr", package)
    monkeypatch.setenv("VOICE_ASR_IMPORT_MODE", "full")
    monkeypatch.setattr(model_loader, "_requested_components",
                        Mock(side_effect=AssertionError("不应解析模型配置")))
    monkeypatch.setattr(model_loader, "_verified_root", lambda: None)
    assert model_loader.auto_model_class(tmp_path) is package.AutoModel


def test_changed_selective_source_falls_back_without_leaking_finder(tmp_path, monkeypatch):
    (tmp_path / "auto").mkdir()
    (tmp_path / "__init__.py").write_text("changed = True\n", encoding="utf-8")
    (tmp_path / "auto/auto_model.py").write_text("changed = True\n", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "funasr", raising=False)
    monkeypatch.setenv("VOICE_ASR_IMPORT_MODE", "selective")
    monkeypatch.setenv("VOICE_MODEL_LOAD_MODE", "original")
    monkeypatch.setattr(model_loader.metadata, "version", lambda _: "1.4.14")
    monkeypatch.setattr(model_loader, "find_spec",
                        lambda _: SimpleNamespace(origin=str(tmp_path / "__init__.py")))
    monkeypatch.setattr(model_loader, "_requested_components", lambda _: ("sensevoice_model",))
    package = SimpleNamespace(AutoModel=object())
    importer = Mock(return_value=package)
    monkeypatch.setattr(model_loader.importlib, "import_module", importer)
    before = list(sys.meta_path)
    assert model_loader.auto_model_class(tmp_path) is package.AutoModel
    importer.assert_called_once_with("funasr")
    assert sys.meta_path == before


def test_unknown_fun_asr_version_uses_full_package_registration(tmp_path, monkeypatch):
    package = SimpleNamespace(AutoModel=object(), __name__="funasr", __path__=[])
    package.import_submodules = Mock()
    monkeypatch.setitem(sys.modules, "funasr", package)
    monkeypatch.setenv("VOICE_MODEL_LOAD_MODE", "mmap")
    monkeypatch.setattr(model_loader.metadata, "version", lambda _: "9.9.9")
    finder = Mock(side_effect=AssertionError("版本不匹配时不得读取适配源"))
    monkeypatch.setattr(model_loader, "find_spec", finder)
    assert model_loader.auto_model_class(tmp_path) is package.AutoModel
    finder.assert_not_called()
    package.import_submodules.assert_called_once_with(package, recursive=True)


def test_component_import_failure_completes_registration_without_dropping_package(tmp_path, monkeypatch):
    sensevoice = _write_config(tmp_path / "sensevoice", """
model: SenseVoiceSmall
encoder: SenseVoiceEncoderSmall
tokenizer: SentencepiecesTokenizer
frontend: WavFrontend
specaug: SpecAugLFR
""")
    package = SimpleNamespace(AutoModel=object(), __name__="funasr", __path__=[])
    package.import_submodules = Mock()
    monkeypatch.setitem(sys.modules, "funasr", package)
    monkeypatch.setenv("VOICE_ASR_IMPORT_MODE", "selective")
    monkeypatch.setenv("VOICE_MODEL_LOAD_MODE", "original")
    monkeypatch.setattr(model_loader, "_verified_root", lambda: tmp_path)
    monkeypatch.setattr(model_loader, "_compile_codes", lambda *args, **kwargs: ({}, None, None))

    def importer(name):
        if name == "funasr":
            return package
        if name == "funasr.models.sense_voice.model":
            raise ImportError("模拟模型组件导入失败")
        return SimpleNamespace(__name__=name)

    monkeypatch.setattr(model_loader.importlib, "import_module", importer)
    assert model_loader.auto_model_class(sensevoice) is package.AutoModel
    package.import_submodules.assert_called_once_with(package, recursive=True)
    assert sys.modules["funasr"] is package


def test_load_pretrained_transform_is_compilable_without_model_import():
    source = '''
import copy
def load_pretrained_model(path, model, map_location="cpu", oss_bucket=None):
    if oss_bucket is None:
        ori_state = torch.load(path, map_location=map_location)
    else:
        ori_state = torch.load(BytesIO(), map_location=map_location)
    src_state = copy.deepcopy(ori_state)
'''
    code = model_loader._pretrained_loader_code(source, "load.py")
    assert code.co_name == "<module>"


def test_installed_fun_asr_loader_matches_guard_and_compiles_candidate():
    root = model_loader._verified_root()
    if root is None:
        pytest.skip("本机没有固定版本 FunASR 安装")
    source = model_loader._read_verified_source(root, "train_utils/load_pretrained_model.py")
    assert source is not None
    code = model_loader._pretrained_loader_code(source.decode("utf-8"), str(root / "load_pretrained_model.py"))
    assert code.co_name == "<module>"


def test_upstream_and_mmap_loaders_match_on_tiny_checkpoint(tmp_path, monkeypatch):
    """只用小型 CPU（中央处理器）张量对照上游和候选加载语义。"""
    root = model_loader._verified_root()
    if root is None:
        pytest.skip("本机没有固定版本 FunASR 安装")
    source = model_loader._read_verified_source(root, "train_utils/load_pretrained_model.py")
    assert source is not None

    import torch

    original_namespace = {"__name__": "_voice_test_original_loader"}
    exec(compile(source.decode("utf-8"), str(root / "train_utils/load_pretrained_model.py"), "exec"),
         original_namespace)
    candidate_namespace = {"__name__": "_voice_test_mmap_loader"}
    exec(model_loader._pretrained_loader_code(
        source.decode("utf-8"), str(root / "train_utils/load_pretrained_model.py")), candidate_namespace)

    class TinyModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.layer = torch.nn.Linear(2, 2)
            self.excluded = torch.nn.Linear(2, 2, bias=False)
            self.mismatch = torch.nn.Parameter(torch.full((3,), 7.0))
            self.missing = torch.nn.Parameter(torch.full((2,), 13.0))
            self.strict_values = []
            self.loaded_keys = None
            with torch.no_grad():
                self.layer.weight.fill_(-2.0)
                self.layer.bias.fill_(-2.0)
                self.excluded.weight.fill_(41.0)

        def load_state_dict(self, state_dict, strict=True):
            self.strict_values.append(strict)
            return super().load_state_dict(state_dict, strict=strict)

        def on_pretrained_model_loaded(self, loaded_keys):
            self.loaded_keys = loaded_keys

    payload = {"state_dict": {"model_state_dict": {"model": {
        "module.layer.weight": torch.tensor([[1.0, 2.0], [3.0, 4.0]]),
        "module.layer.bias": torch.tensor([5.0, 6.0]),
        "module.excluded.weight": torch.full((2, 2), 99.0),
        "module.mismatch": torch.tensor([1.0, 2.0, 3.0, 4.0]),
    }}}}
    checkpoint = tmp_path / "tiny-state.pt"
    torch.save(payload, checkpoint)
    file_hash_before = hashlib.sha256(checkpoint.read_bytes()).hexdigest()

    def clone_tree(value):
        if isinstance(value, dict):
            return {key: clone_tree(item) for key, item in value.items()}
        if torch.is_tensor(value):
            return value.clone()
        return value

    def assert_tree_equal(actual, expected):
        assert actual.keys() == expected.keys()
        for key in actual:
            left, right = actual[key], expected[key]
            if isinstance(left, dict):
                assert_tree_equal(left, right)
            elif torch.is_tensor(left):
                assert torch.equal(left, right)
            else:
                assert left == right

    real_torch_load = torch.load
    loaded_checkpoints = []
    snapshots = []
    load_calls = []

    def tracked_torch_load(path, *args, **kwargs):
        loaded = real_torch_load(path, *args, **kwargs)
        loaded_checkpoints.append(loaded)
        snapshots.append(clone_tree(loaded))
        load_calls.append((path, dict(kwargs)))
        return loaded

    monkeypatch.setattr(torch, "load", tracked_torch_load)
    models = []
    for namespace in (original_namespace, candidate_namespace):
        model = TinyModel()
        namespace["load_pretrained_model"](
            path=str(checkpoint), model=model, ignore_init_mismatch=True,
            map_location="cpu", scope_map=[], excludes=["excluded."],
        )
        models.append(model)

    original, candidate = models
    assert load_calls[0][1].get("mmap") is None
    assert load_calls[1][1].get("mmap") is True
    assert original.strict_values == candidate.strict_values == [True]
    assert original.loaded_keys == candidate.loaded_keys == frozenset({"layer.weight", "layer.bias"})
    for model in models:
        state = model.state_dict()
        assert torch.equal(state["layer.weight"], payload["state_dict"]["model_state_dict"]["model"]["module.layer.weight"])
        assert torch.equal(state["layer.bias"], payload["state_dict"]["model_state_dict"]["model"]["module.layer.bias"])
        assert torch.equal(state["excluded.weight"], torch.full((2, 2), 41.0))
        assert torch.equal(state["mismatch"], torch.full((3,), 7.0))
        assert torch.equal(state["missing"], torch.full((2,), 13.0))
    for loaded, snapshot in zip(loaded_checkpoints, snapshots):
        assert_tree_equal(loaded, snapshot)
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == file_hash_before

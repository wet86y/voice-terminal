"""ASR loader（识别加载器）：兼容回退与可选依赖的实际执行语义。"""
import builtins
import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_unknown_model_config_uses_full_registration(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("model: AnotherModel\n", encoding="utf-8")
    full = SimpleNamespace(AutoModel=object())
    importer = Mock(return_value=full)
    monkeypatch.setattr(model_loader.importlib, "import_module", importer)
    assert model_loader.auto_model_class(tmp_path) is full.AutoModel
    importer.assert_called_once_with("funasr")


def test_changed_dependency_source_uses_full_import_before_any_patches(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "funasr", raising=False)
    monkeypatch.setattr(model_loader, "_supported_model", lambda _: True)
    monkeypatch.setattr(model_loader.metadata, "version", lambda _: "1.4.14")
    monkeypatch.setattr(model_loader, "find_spec", lambda _: SimpleNamespace(origin=str(tmp_path / "__init__.py")))
    (tmp_path / "auto").mkdir()
    (tmp_path / "__init__.py").write_text("changed = True\n", encoding="utf-8")
    (tmp_path / "auto/auto_model.py").write_text("changed = True\n", encoding="utf-8")
    full = SimpleNamespace(AutoModel=object())
    monkeypatch.setattr(model_loader.importlib, "import_module", Mock(return_value=full))
    before = list(sys.meta_path)
    assert model_loader.auto_model_class(tmp_path) is full.AutoModel
    assert sys.meta_path == before


def test_explicit_full_import_never_reads_model_config(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_ASR_IMPORT_MODE", "full")
    supported = Mock(side_effect=AssertionError("不应读取模型配置"))
    monkeypatch.setattr(model_loader, "_supported_model", supported)
    full = SimpleNamespace(AutoModel=object())
    monkeypatch.setattr(model_loader.importlib, "import_module", Mock(return_value=full))
    assert model_loader.auto_model_class(tmp_path) is full.AutoModel
    supported.assert_not_called()


def test_unknown_version_falls_back_without_reading_dependency_files(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "funasr", raising=False)
    monkeypatch.setattr(model_loader, "_supported_model", lambda _: True)
    monkeypatch.setattr(model_loader.metadata, "version", lambda _: "2.0.0")
    monkeypatch.setattr(model_loader, "find_spec", lambda _: SimpleNamespace(origin=str(tmp_path / "missing.py")))
    full = SimpleNamespace(AutoModel=object())
    monkeypatch.setattr(model_loader.importlib, "import_module", Mock(return_value=full))
    assert model_loader.auto_model_class(tmp_path) is full.AutoModel


def test_partial_import_failure_restores_full_import_and_removes_temporary_finder(tmp_path, monkeypatch):
    entry = b'def import_submodules(name): pass\nimport_submodules(__name__)\n'
    auto = b'''try:
    from funasr.models.campplus.utils import sv_chunk, postprocess, distribute_spk
    from funasr.models.campplus.cluster_backend import ClusterBackend
except:
    pass
class AutoModel:
    def __init__(self):
        spk_model = None
        if spk_model is not None:
            pass
'''
    (tmp_path / "auto").mkdir()
    (tmp_path / "__init__.py").write_bytes(entry)
    (tmp_path / "auto/auto_model.py").write_bytes(auto)
    monkeypatch.setattr(model_loader, "_SOURCE_HASHES", {"__init__.py": hashlib.sha256(entry).hexdigest(),
                                                        "auto/auto_model.py": hashlib.sha256(auto).hexdigest()})
    fake_sys = SimpleNamespace(modules={"unrelated": object()}, meta_path=[])
    monkeypatch.setattr(model_loader, "sys", fake_sys)
    monkeypatch.setattr(model_loader, "_supported_model", lambda _: True)
    monkeypatch.setattr(model_loader.metadata, "version", lambda _: "1.4.14")
    monkeypatch.setattr(model_loader, "find_spec", lambda _: SimpleNamespace(origin=str(tmp_path / "__init__.py")))
    full = SimpleNamespace(AutoModel=object())
    attempts = []
    def importer(name):
        attempts.append(name)
        if len(attempts) == 1:
            fake_sys.modules["funasr"] = object()
            return object()
        if name != "funasr":
            fake_sys.modules[name] = object()
            raise ImportError("模拟按需组件导入失败")
        assert fake_sys.meta_path == [] and "funasr" not in fake_sys.modules
        return full
    monkeypatch.setattr(model_loader.importlib, "import_module", importer)
    assert model_loader.auto_model_class(tmp_path) is full.AutoModel
    assert list(fake_sys.modules) == ["unrelated"] and fake_sys.meta_path == []

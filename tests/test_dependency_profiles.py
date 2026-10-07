"""显卡运行库替代关系不能掩盖缺包、版本不符或模块覆盖。"""
from types import SimpleNamespace

from voice_terminal.dependencies import dependency_errors


def distribution(name, version="1.24.4", requires=()):
    return SimpleNamespace(metadata={"Name": name}, version=version, requires=list(requires))


def test_onnx_gpu_satisfies_wrapper_dependency_without_installing_cpu_package(monkeypatch, tmp_path):
    packages = [distribution("funasr-onnx", "0.4.3", ["onnxruntime>=1.7.0"]), distribution("onnxruntime-gpu")]
    seen = []
    def installed(**kwargs):
        seen.append(kwargs)
        return packages
    monkeypatch.setattr("voice_terminal.dependencies.metadata.distributions", installed)
    assert dependency_errors(tmp_path) == []
    assert seen == [{"path": [str(tmp_path)]}]


def test_overlapping_onnx_packages_are_rejected(monkeypatch):
    packages = [distribution("onnxruntime", "1.24.3"), distribution("onnxruntime-gpu")]
    monkeypatch.setattr("voice_terminal.dependencies.metadata.distributions", lambda **kwargs:packages)
    assert "不能同时安装" in dependency_errors()[0]


def test_gpu_alias_still_checks_missing_dependency_and_version(monkeypatch):
    packages = [distribution("consumer", requires=["onnxruntime>=2", "missing-package>=1"]), distribution("onnxruntime-gpu")]
    monkeypatch.setattr("voice_terminal.dependencies.metadata.distributions", lambda **kwargs:packages)
    errors = dependency_errors()
    assert len(errors) == 2
    assert "onnxruntime>=2" in errors[0] and "missing-package>=1" in errors[1]

"""隔离依赖必须来自显式目录，不能借用主环境或全局覆盖。"""
import pytest
from voice_terminal.tts import aniflive_environment as environment


def test_package_roles_have_explicit_overlay_order(tmp_path):
    root = tmp_path / "runtimes/aniflive-tts"
    (root / "packages").mkdir(parents=True)
    (root / "export-packages").mkdir()
    assert environment.package_paths(tmp_path, "inference") == [root / "packages"]
    assert environment.package_paths(tmp_path, "export") == [root / "export-packages", root / "packages"]
    with pytest.raises(ValueError, match="角色"):
        environment.package_paths(tmp_path, "other")


def test_main_environment_packages_cannot_satisfy_empty_isolated_directory(tmp_path):
    with pytest.raises(RuntimeError, match="未安装"):
        environment.validate_versions([tmp_path], "inference")


def test_export_override_does_not_change_runtime_versions(tmp_path):
    def install(directory, versions):
        directory.mkdir()
        for name, version in versions.items():
            folder = directory / f"{name.replace('-', '_')}-{version}.dist-info"
            folder.mkdir()
            (folder / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    runtime, export = tmp_path / "runtime", tmp_path / "export"
    install(runtime, environment.RUNTIME_VERSIONS)
    install(export, environment.EXPORT_VERSIONS)
    assert environment.validate_versions([runtime], "inference")["transformers"] == "5.5.0"
    assert environment.validate_versions([export, runtime], "export")["transformers"] == "4.51.3"
    with pytest.raises(RuntimeError, match="不匹配"):
        environment.validate_versions([export, runtime], "inference")

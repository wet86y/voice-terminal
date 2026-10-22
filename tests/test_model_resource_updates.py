"""Model update acceptance（模型更新验收）：旧版阻止启动、更新替换与失败恢复。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from voice_terminal.application.resource_readiness import inspect_model_resources
from voice_terminal.local_layout import default_portable
from voice_terminal.model_installation import RECEIPT_NAME, inspect_installed_model
from voice_terminal.resource_distribution import ResourceError, ResourceManager


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _catalog(program: Path, version: str, payload: dict[str, bytes], *, optional: set[str] | None = None) -> None:
    _json(program / "config/resources/catalog.json", {"schema_version": 1, "resources": [{
        "resource_id": "test-model", "kind": "model", "title": "更新验收模型",
        "destination": "test/model", "version": version,
        "source": {"platform": "fixture", "repo_id": "fixture/test-model", "revision": version,
                   "transfer": {"kind": "modelscope_sdk"}},
        "files": [{"path": name, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                   "optional": name in (optional or set())} for name, content in payload.items()]}]})


def _source(root: Path, payload: dict[str, bytes]) -> Path:
    root.mkdir(parents=True)
    for name, content in payload.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


@pytest.fixture
def workspace(tmp_path):
    program, root = tmp_path / "程序本体", tmp_path / "用户 工作区"
    _json(root / "portable.json", default_portable())
    _json(root / "app/release.json", {"schema": 1, "kind": "app", "build": "fixture", "components": []})
    (root / "data").mkdir()
    (root / "data/preferences.json").write_bytes(b"user-settings")
    _catalog(program, "v1", {"model.pt": b"old-weight", "config.yaml": b"config", "retired.onnx": b"retired"},
             optional={"retired.onnx"})
    manager = ResourceManager(program, root)
    manager.import_source(_source(tmp_path / "old-source", {"model.pt": b"old-weight", "config.yaml": b"config",
                                                          "retired.onnx": b"retired"}),
                          resource_id="test-model", task_id="initial")
    return program, root, manager


def test_first_install_records_a_cross_runtime_receipt(workspace):
    """First installation（首次安装）登记实际版本和内容摘要。"""
    _, root, manager = workspace
    receipt = json.loads((root / "models/test/model" / RECEIPT_NAME).read_text("utf-8"))
    state = manager.status("test-model")["resources"][0]
    assert receipt["resource_id"] == "test-model" and receipt["version"] == "v1"
    assert receipt["files"][2]["optional"] is True
    assert state["state"] == "installed" and state["installed_version"] == "v1"
    assert state["update_available"] is False
    assert inspect_installed_model(root / "models", "test/model", "test-model", verify_hashes=True).error is None


def test_program_upgrade_requires_model_update_before_startup(workspace):
    """Program upgrade（本体升级）要求组件更新，保留原文件直到候选通过校验。"""
    program, root, _ = workspace
    before = (root / "models/test/model/model.pt").read_bytes()
    _catalog(program, "v2", {"model.pt": b"new-weight-with-different-size", "config.yaml": b"config",
                             "onnx/model.onnx": b"light-model"})
    manager = ResourceManager(program, root)
    state = manager.status("test-model")["resources"][0]
    assert state["state"] == "installed" and state["update_available"] is True
    assert state["installed_version"] == "v1" and state["version"] == "v2"
    assert state["error"] is None
    assert state["can_start"] is False
    assert "更新" in inspect_model_resources(program, root / "models", {"voice": ["test-model"]})["voice"]
    assert manager.prepare("test-model", "upgrade")["already_installed"] is False
    assert (root / "models/test/model/model.pt").read_bytes() == before
    assert (root / "data/preferences.json").read_bytes() == b"user-settings"


def test_model_update_replaces_whole_model_and_removes_retired_files(workspace, tmp_path):
    """Model update（模型更新）完成后清理旧文件并复用用户数据。"""
    program, root, _ = workspace
    payload = {"model.pt": b"new-weight", "config.yaml": b"config", "onnx/model.onnx": b"light-model"}
    _catalog(program, "v2", payload)
    manager = ResourceManager(program, root)
    result = manager.import_source(_source(tmp_path / "new-source", payload), resource_id="test-model", task_id="upgrade")
    assert result["installed_resource_ids"] == ["test-model"]
    state = manager.status("test-model")["resources"][0]
    assert state["state"] == "installed" and state["update_available"] is False
    assert state["installed_version"] == "v2"
    assert state["can_start"] is True
    assert inspect_model_resources(program, root / "models", {"voice": ["test-model"]}) == {}
    assert not (root / "models/test/model/retired.onnx").exists()
    assert (root / "models/test/model/onnx/model.onnx").read_bytes() == b"light-model"
    assert not manager.transaction_marker.exists()
    assert not list((root / "tmp/resource-transactions").rglob("target-backup"))
    assert (root / "data/preferences.json").read_bytes() == b"user-settings"


def test_same_version_changed_weights_still_show_update(workspace):
    """Same-version content change（同版本内容变化）也显示更新。"""
    program, root, _ = workspace
    _catalog(program, "v1", {"model.pt": b"new-weight", "config.yaml": b"config"})
    state = ResourceManager(program, root).status("test-model")["resources"][0]
    assert state["state"] == "installed" and state["update_available"] is True
    assert state["can_start"] is False
    assert "更新" in inspect_model_resources(program, root / "models", {"voice": ["test-model"]})["voice"]


def test_updates_to_inactive_models_are_reported_without_blocking_other_roles(workspace):
    """启动只检查所选资源；完整性维护及组件管理继续报告未选资源更新。"""
    program, root, _ = workspace
    _catalog(program, "v2", {"model.pt": b"new-weight", "config.yaml": b"config"})
    catalog_path = program / "config/resources/catalog.json"
    value = json.loads(catalog_path.read_text("utf-8"))
    other = {**value["resources"][0], "resource_id": "active-model", "title": "当前启用模型",
             "destination": "active", "version": "v1", "files": [{"path": "model.pt", "size_bytes": 2,
                 "sha256": hashlib.sha256(b"ok").hexdigest()}]}
    value["resources"].append(other)
    _json(catalog_path, value)
    (root / "models/active").mkdir()
    (root / "models/active/model.pt").write_bytes(b"ok")
    issues = inspect_model_resources(program, root / "models", {"asr": ["active-model"], "voice": []})
    assert issues == {}
    assert ResourceManager(program, root)._model_state(ResourceManager(program, root).catalog.get("test-model"))["update_available"] is True


def test_optional_inactive_model_updates_do_not_block_other_roles(workspace):
    program, root, _ = workspace
    _catalog(program, "v2", {"model.pt": b"new-weight", "config.yaml": b"config"})
    catalog_path = program / "config/resources/catalog.json"
    value = json.loads(catalog_path.read_text("utf-8"))
    value["resources"][0]["required"] = False
    _json(catalog_path, value)
    issues = inspect_model_resources(program, root / "models", {"voice": [], "speaker": ["test-model"]})
    assert set(issues) == {"speaker"}
    assert "组件有更新" in issues["speaker"]


def test_old_runtime_receipt_does_not_bypass_current_dependency_gate(workspace):
    """Runtime update gate（运行依赖更新门禁）：旧格式及缺少第六依赖组均阻止启动。"""
    program, root, _ = workspace
    lock = program / "config/runtime/voice-runtime.lock.json"
    _json(lock, {"schema": 1})
    runtime = root / "runtimes/python/runtime.json"
    _json(runtime, {"schema": 1, "profile": "cu128"})
    assert "语音运行环境需要更新" in inspect_model_resources(program, root / "models", {"voice": ["test-model"]})["voice"]
    groups = ["voice-core", "torch-cpu", "onnx-cpu", "sherpa-cpu", "modelscope-download"]
    _json(runtime, {"schema": 2, "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(), "groups": groups})
    assert "语音运行环境需要更新" in inspect_model_resources(program, root / "models", {"voice": ["test-model"]})["voice"]
    _json(runtime, {"schema": 2, "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(), "groups": groups + ["onnx-vad"]})
    assert inspect_model_resources(program, root / "models", {"voice": ["test-model"]}) == {}


def test_legacy_manifest_without_receipt_still_requires_current_model(workspace):
    """Legacy manifest（旧安装根清单）只识别待更新状态，不放行旧模型。"""
    program, root, _ = workspace
    (root / "models/test/model" / RECEIPT_NAME).unlink()
    _catalog(program, "v2", {"model.pt": b"new-weight", "config.yaml": b"config", "onnx/model.onnx": b"light"})
    state = ResourceManager(program, root).status("test-model")["resources"][0]
    assert state["state"] == "installed" and state["update_available"] is True
    assert state["installed_version"] is None
    assert state["can_start"] is False
    assert "更新" in inspect_model_resources(program, root / "models", {"voice": ["test-model"]})["voice"]


def test_corrupt_old_model_is_incomplete_and_cannot_start(workspace):
    """Corruption（文件损坏）不能伪装成可用旧版本。"""
    program, root, _ = workspace
    (root / "models/test/model/model.pt").write_bytes(b"bad-weight")
    state = ResourceManager(program, root).status("test-model")["resources"][0]
    assert state["state"] == "incomplete" and state["update_available"] is False
    assert state["error"]


def test_candidate_validation_failure_keeps_old_model(workspace, tmp_path):
    """Candidate mismatch（候选校验失败）不触碰旧模型。"""
    program, root, _ = workspace
    _catalog(program, "v2", {"model.pt": b"new-weight", "config.yaml": b"config"})
    manager = ResourceManager(program, root)
    before = (root / "models/manifest.json").read_bytes()
    with pytest.raises(ResourceError, match="SHA-256"):
        manager.import_source(_source(tmp_path / "bad-source", {"model.pt": b"bad-weight", "config.yaml": b"config"}),
                              resource_id="test-model", task_id="bad-upgrade")
    assert (root / "models/test/model/model.pt").read_bytes() == b"old-weight"
    assert (root / "models/manifest.json").read_bytes() == before


def test_metadata_commit_failure_restores_old_receipt_model_and_release(workspace, tmp_path, monkeypatch):
    """Commit failure（提交失败）恢复旧模型、收据及发行登记。"""
    import voice_terminal.resource_distribution as resources
    program, root, _ = workspace
    target = root / "models/test/model"
    before = {path: path.read_bytes() for path in (target / "model.pt", target / RECEIPT_NAME,
                                                   root / "models/manifest.json", root / "app/release.json")}
    payload = {"model.pt": b"new-weight", "config.yaml": b"config"}
    _catalog(program, "v2", payload)
    manager = ResourceManager(program, root)
    original = resources._atomic_write
    injected = False

    def fail_once(path, content):
        nonlocal injected
        if path == root / "models/manifest.json" and not injected:
            injected = True
            raise OSError("模拟清单写入失败")
        original(path, content)

    monkeypatch.setattr(resources, "_atomic_write", fail_once)
    with pytest.raises(ResourceError, match="安装事务失败"):
        manager.import_source(_source(tmp_path / "failure-source", payload), resource_id="test-model", task_id="failure")
    assert injected
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not manager.transaction_marker.exists()

def _model_manifest_record(root: Path, relative: str) -> dict:
    manifest = json.loads((root / "models/manifest.json").read_text("utf-8"))
    return next(row for row in manifest["files"] if row["path"] == relative)


def test_refreshed_manifest_rehashes_retained_files_changed_after_signing(workspace, tmp_path):
    """Retained file changed after signing（签发后被改写的保留文件）：总清单必须重新读取内容。"""
    program, root, _ = workspace
    retained = root / "models/other/keep.bin"
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_bytes(b"first-content")
    _catalog(program, "v2", {"model.pt": b"new-weight", "config.yaml": b"config"})
    ResourceManager(program, root).import_source(
        _source(tmp_path / "second-source", {"model.pt": b"new-weight", "config.yaml": b"config"}),
        resource_id="test-model", task_id="second")
    assert _model_manifest_record(root, "other/keep.bin")["sha256"] == hashlib.sha256(b"first-content").hexdigest()
    # 同长度改写保留文件，并保证改写时间晚于清单签发时间。
    retained.write_bytes(b"other-content")
    signed_at = (root / "models/manifest.json").stat().st_mtime_ns
    if retained.stat().st_mtime_ns <= signed_at:
        os.utime(retained, ns=(signed_at + 1_000_000, signed_at + 1_000_000))
    assert retained.stat().st_mtime_ns > signed_at
    _catalog(program, "v3", {"model.pt": b"third-weight", "config.yaml": b"config"})
    ResourceManager(program, root).import_source(
        _source(tmp_path / "third-source", {"model.pt": b"third-weight", "config.yaml": b"config"}),
        resource_id="test-model", task_id="third")
    record = _model_manifest_record(root, "other/keep.bin")
    assert record["size"] == len(b"other-content")
    assert record["sha256"] == hashlib.sha256(b"other-content").hexdigest()

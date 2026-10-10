"""FSMN-VAD bundled ONNX update contract（随包轻量资源更新契约）。"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

from voice_terminal.resource_distribution import ResourceCatalog, ResourceManager


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def test_fsmn_vad_onnx_assets_are_fixed_and_materialized_from_program_bundle(tmp_path):
    raw_catalog = json.loads((ROOT / "config/resources/catalog.json").read_text(encoding="utf-8"))
    raw_spec = next(row for row in raw_catalog["resources"] if row["resource_id"] == "fsmn-vad")
    assert raw_spec["version"] == "v2.0.4-onnx-443ac237"
    assert raw_spec["dependency_group"] == "onnx-vad"
    assert next(row for row in raw_spec["files"] if row["path"] == "model.pt")["sha256"] == (
        "b3be75be477f0780277f3bae0fe489f48718f585f3a6e45d7dd1fbb1a4255fc5"
    )

    spec = ResourceCatalog(ROOT).get("fsmn-vad")
    assets = tuple(item for item in spec.files if item.asset is not None)
    assert {item.path for item in assets} == {
        "onnx/model.onnx", "onnx/config.yaml", "onnx/am.mvn", "onnx/manifest.json"
    }
    assert all(item.optional is False for item in assets)
    for item in assets:
        bundled = ROOT / "config/resources" / item.asset
        assert bundled.stat().st_size == item.size_bytes
        assert _sha256(bundled) == item.sha256

    manager = ResourceManager.__new__(ResourceManager)
    manager.program = ROOT
    candidate_spec = replace(spec, files=assets)
    candidate = manager._prepare_candidate(tmp_path / "empty-source", candidate_spec, tmp_path / "task")
    for item in assets:
        installed = candidate.joinpath(*Path(item.path).parts)
        assert installed.read_bytes() == (ROOT / "config/resources" / item.asset).read_bytes()

    manifest = json.loads((candidate / "onnx/manifest.json").read_text(encoding="utf-8"))
    source_weight = next(row for row in raw_spec["files"] if row["path"] == "model.pt")
    assert manifest["source_sha256"] == source_weight["sha256"]
    assert manifest["quantized"] is False


def test_onnx_vad_dependency_group_is_separate_and_uses_official_fun_asr_onnx_wheel():
    lock = json.loads((ROOT / "config/runtime/voice-runtime.lock.json").read_text(encoding="utf-8"))
    group = lock["groups"]["onnx-vad"]
    locked_names = {row["name"] for row in lock["wheels"] if "onnx-vad" in row["groups"]}
    assert locked_names == set(group)
    assert "funasr-onnx" in group
    assert "onnxruntime" in group
    assert "kaldi-native-fbank" in group
    wheel = next(row for row in lock["wheels"] if row["name"] == "funasr-onnx")
    assert wheel["version"] == "0.4.3"
    assert wheel["groups"] == ["onnx-vad"]
    assert wheel["url"] == (
        "https://files.pythonhosted.org/packages/27/17/e19a180c97e916e058b5fc5eecfa431d46cd8673c995699dcba6d978b728/"
        "funasr_onnx-0.4.3-py3-none-any.whl"
    )
    assert wheel["sha256"] == "7dd65d6a506dc171a53e87eb55faf0a3e478f2531f90671246f43c2c56023fe6"

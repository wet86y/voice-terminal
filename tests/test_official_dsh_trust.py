"""Official DSH trust record（官方 DSH 可信记录）requires the pinned artifact and full tree hashes."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

from voice_terminal.dsh.bridge import BRIDGE_PROTOCOL_VERSION
from voice_terminal.dsh.official import (
    OFFICIAL_ASAR,
    OFFICIAL_ASAR_UNPACKED,
    OFFICIAL_CLI,
    OFFICIAL_EXECUTABLE,
    OFFICIAL_OFFICE_CLI,
    OFFICIAL_OFFICE_ENGINE,
    OFFICIAL_SCHEDULE,
    OfficialArtifact,
)
from voice_terminal.resource_distribution import ResourceError, ResourceManager


REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "install_official_dsh_test", REPO / "scripts" / "install-official-dsh.py",
)
assert _SPEC is not None and _SPEC.loader is not None
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)

LOCK_FIELDS = {
    "schema": "可信提取记录格式版本",
    "version": "固定官方 DSH（代理执行宿主）版本",
    "source_sha512": "固定官方安装器完整 SHA-512（文件摘要）十六进制值",
    "manifest_sha256": "完整提取运行清单 SHA-256（文件摘要）十六进制值",
}


def _artifact() -> OfficialArtifact:
    digest = "a" * 128
    return OfficialArtifact(
        version="9.8.7-test",
        compatibility_commit="b" * 40,
        url="https://download.deepseek.com/test.exe",
        filename="deepseek-harness-9.8.7-test-win-x64.exe",
        size_bytes=1,
        sha512_base64=base64.b64encode(bytes.fromhex(digest)).decode("ascii"),
        sha512_hex=digest,
    )


def _runtime(path: Path, artifact: OfficialArtifact) -> tuple[Path, dict, str]:
    runtime = path / "runtime"
    files = {
        OFFICIAL_EXECUTABLE: b"mock executable",
        OFFICIAL_ASAR: b"mock asar",
        OFFICIAL_OFFICE_CLI: b"mock office cli",
        OFFICIAL_OFFICE_ENGINE + "/prebuilds.json": b"{}",
    }
    for relative, content in files.items():
        target = runtime / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (runtime / OFFICIAL_ASAR_UNPACKED).mkdir(parents=True, exist_ok=True)
    installer._install_metadata(runtime, artifact)
    record = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    manifest_sha256 = hashlib.sha256((runtime / "manifest.json").read_bytes()).hexdigest()
    return runtime, record, manifest_sha256


def _manager(program: Path, workspace: Path, artifact: OfficialArtifact, monkeypatch) -> ResourceManager:
    import voice_terminal.resource_distribution as resources

    monkeypatch.setattr(resources, "load_official_artifact", lambda _program: artifact)
    manager = ResourceManager.__new__(ResourceManager)
    manager.program = program
    manager.root = workspace
    manager.dsh_relative = "runtimes/dsh"
    return manager


def _lock(artifact: OfficialArtifact, manifest_sha256: str, *, version: str | None = None,
          source_sha512: str | None = None, manifest_override: str | None = None) -> dict:
    return {
        "schema": 1,
        "version": version or artifact.version,
        "source_sha512": source_sha512 or artifact.sha512_hex,
        "manifest_sha256": manifest_override or manifest_sha256,
        "fields": dict(LOCK_FIELDS),
    }


def _write_lock(program: Path, record: dict) -> None:
    path = program / "config" / "dsh" / "official-runtime.lock.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def test_program_published_record_attests_directory_without_workspace_binding(tmp_path, monkeypatch):
    artifact = _artifact()
    runtime, _manifest, manifest_sha256 = _runtime(tmp_path / "fixture", artifact)
    program = tmp_path / "app"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_lock(program, _lock(artifact, manifest_sha256))
    manager = _manager(program, workspace, artifact, monkeypatch)

    paths, found_sha256 = manager._inspect_official_dsh_tree(runtime)

    assert paths.metadata["version"] == artifact.version
    assert found_sha256 == manifest_sha256


@pytest.mark.parametrize("field", ["version", "source_sha512", "manifest_sha256"])
def test_wrong_published_version_or_digest_rejects_without_local_binding(tmp_path, monkeypatch, field):
    artifact = _artifact()
    runtime, _manifest, manifest_sha256 = _runtime(tmp_path / "fixture", artifact)
    program = tmp_path / "app"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    values = {
        "version": {"version": "other-version"},
        "source_sha512": {"source_sha512": "c" * 128},
        "manifest_sha256": {"manifest_override": "d" * 64},
    }[field]
    _write_lock(program, _lock(artifact, manifest_sha256, **values))
    manager = _manager(program, workspace, artifact, monkeypatch)

    with pytest.raises(ResourceError) as error:
        manager._inspect_official_dsh_tree(runtime)

    assert error.value.code == "official_dsh_unattested"


def test_missing_published_record_needs_local_binding_and_existing_binding_still_works(
    tmp_path, monkeypatch,
):
    artifact = _artifact()
    runtime, manifest, manifest_sha256 = _runtime(tmp_path / "fixture", artifact)
    program = tmp_path / "app"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _manager(program, workspace, artifact, monkeypatch)

    with pytest.raises(ResourceError) as error:
        manager._inspect_official_dsh_tree(runtime)
    assert error.value.code == "official_dsh_unattested"

    release = workspace / "app" / "release.json"
    release.parent.mkdir(parents=True)
    release.write_text(json.dumps({"components": [{
        "kind": "dsh",
        "path": "runtimes/dsh",
        "build": manifest["build"],
        "manifest_sha256": manifest_sha256,
    }]}), encoding="utf-8")
    paths, found_sha256 = manager._inspect_official_dsh_tree(runtime)
    assert paths.manifest["build"] == manifest["build"]
    assert found_sha256 == manifest_sha256

    # A bad optional program lock does not bypass or replace the exact local binding.
    _write_lock(program, _lock(artifact, manifest_sha256, version="other-version"))
    paths, found_sha256 = manager._inspect_official_dsh_tree(runtime)
    assert paths.manifest["build"] == manifest["build"]
    assert found_sha256 == manifest_sha256


def test_published_record_allows_fully_verified_official_bulk_zip(tmp_path, monkeypatch):
    artifact = _artifact()
    runtime, manifest, manifest_sha256 = _runtime(tmp_path / "fixture", artifact)
    program = tmp_path / "app"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = _manager(program, workspace, artifact, monkeypatch)
    task_dir = workspace / "tmp" / "resource-tasks" / "bundle-trust-test"
    task_dir.mkdir(parents=True)

    content_files = [row["path"] for row in manifest["files"]] + ["manifest.json"]
    file_records = []
    archive_path = tmp_path / "dsh-resource.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative in content_files:
            source = runtime / Path(relative)
            content = source.read_bytes()
            package_path = f"resources/dsh/{relative}"
            archive.writestr(package_path, content)
            file_records.append({
                "path": relative,
                "package_path": package_path,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
    with zipfile.ZipFile(archive_path) as archive:
        by_name = {entry.filename: entry for entry in archive.infolist()}
        entry = {
            "trust_level": "program_attested_official_runtime",
            "version": artifact.version,
            "destination": "runtimes/dsh",
            "source_installer_sha512": artifact.sha512_hex,
            "manifest_sha256": manifest_sha256,
            "files": file_records,
        }
        with pytest.raises(ResourceError) as error:
            manager._stage_official_dsh_bundle(archive, by_name, entry, task_dir)
        assert error.value.code == "official_dsh_unattested"

        # The ZIP's own declaration is insufficient; the exact packaged trust lock is required.
        _write_lock(program, _lock(artifact, manifest_sha256))
        candidate = manager._stage_official_dsh_bundle(archive, by_name, entry, task_dir)

    assert candidate.is_dir()
    assert (candidate / "manifest.json").is_file()
    assert hashlib.sha256((candidate / "manifest.json").read_bytes()).hexdigest() == manifest_sha256


def test_export_record_requires_verified_installer_and_full_extraction(tmp_path):
    artifact = _artifact()
    runtime, manifest, manifest_sha256 = _runtime(tmp_path / "fixture", artifact)
    destination = tmp_path / "maintainer-output" / "official-runtime.lock.json"

    result = installer._export_official_runtime_lock(destination, runtime, artifact, artifact.sha512_hex)

    assert result["version"] == artifact.version
    assert result["source_sha512"] == artifact.sha512_hex
    assert result["manifest_sha256"] == manifest_sha256
    assert result["fields"] == LOCK_FIELDS
    assert json.loads(destination.read_text(encoding="utf-8")) == result
    assert manifest["kind"] == "dsh"

    destination.unlink()
    with pytest.raises(ValueError):
        installer._export_official_runtime_lock(destination, runtime, artifact, "e" * 128)
    assert not destination.exists()

    (runtime / OFFICIAL_EXECUTABLE).write_bytes(b"modified after manifest generation")
    with pytest.raises(ValueError):
        installer._export_official_runtime_lock(destination, runtime, artifact, artifact.sha512_hex)
    assert not destination.exists()


def test_directory_source_cannot_export_a_published_trust_record(tmp_path, monkeypatch):
    artifact = _artifact()
    program = tmp_path / "app"
    workspace = tmp_path / "workspace"
    source = tmp_path / "directory-source"
    program.mkdir()
    workspace.mkdir()
    source.mkdir()
    monkeypatch.setattr(installer, "load_official_artifact", lambda _program: artifact)
    output = tmp_path / "must-not-exist.json"

    with pytest.raises(ValueError, match="不能从本地目录导出"):
        installer.install(workspace, program, source, "trust-export-test", export_trust_record=output)

    assert not output.exists()

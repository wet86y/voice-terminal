"""ZIP64 staging paths（ZIP64 暂存路径）stay below Windows MAX_PATH（最大路径）."""
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
import zipfile

from voice_terminal.resource_distribution import ResourceManager, _bundle_stage_dir


def _utf16_units(path: PureWindowsPath) -> int:
    return len(str(path).encode("utf-16-le", "surrogatepass")) // 2


def test_bulk_zip_stage_uses_short_paths_for_long_task_and_dsh_member():
    # This is the longest DSH-relative path（DSH 资源内相对路径）in the offline
    # fixture（离线样本）that reproduced the 260-character failure; payload reads are
    # mocked（模拟）and no model data（模型数据）is used.
    dsh_member = (
        "node_modules/@deepseek-ai/libreoffice-kit-win32-x64/sources/engine/native/patches/"
        "0063-windows-static-string-allocator-initialization.patch"
    )
    task_id = "migration-offline-" + "a" * 32
    assert len(task_id) == 50

    repo = Path(__file__).resolve().parents[1]
    temp_parent = repo / ".local" / "tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="资源 路径 ", dir=temp_parent) as temporary_root:
        root = Path(temporary_root)
        task_dir = root / "tmp" / "resource-tasks" / task_id
        dsh_path = PurePosixPath(dsh_member)
        legacy_prefix = PureWindowsPath(str(task_dir / "bundle-prevalidated" / "dsh-source"))
        shortened = PureWindowsPath(str(_bundle_stage_dir(task_dir, "legacy_dsh")))
        legacy_file = legacy_prefix.joinpath(*dsh_path.parts)
        shortened_file = shortened.joinpath(*dsh_path.parts)
        assert _utf16_units(legacy_file) >= 260
        assert _utf16_units(shortened_file) < 260

        records = [
            {"path": dsh_member, "package_path": f"resources/dsh/{dsh_member}", "size_bytes": 1, "sha256": "a" * 64},
            {"path": "manifest.json", "package_path": "resources/dsh/manifest.json", "size_bytes": 1, "sha256": "b" * 64},
        ]
        by_name = {
            record["package_path"]: zipfile.ZipInfo(record["package_path"])
            for record in records
        }
        for record in records:
            by_name[record["package_path"]].file_size = record["size_bytes"]

        manager = ResourceManager.__new__(ResourceManager)

        def mock_copy(_bundle, _info, destination, expected_size, _expected_sha256):
            destination.write_bytes(b"x" * expected_size)

        manager._copy_zip_member = mock_copy
        manager._validate_dsh_tree = lambda source, _task_dir, **_kwargs: source
        staged = manager._stage_dsh_bundle(
            object(), by_name,
            {"trust_level": "legacy_compatibility_only", "files": records},
            task_dir,
        )
        assert PureWindowsPath(str(staged)).relative_to(PureWindowsPath(str(task_dir))) == PureWindowsPath("b/d")
        assert (staged / dsh_member).is_file()
        assert _bundle_stage_dir(task_dir, "model", "smart-turn").relative_to(task_dir) == Path("b/m/smart-turn")
        assert _bundle_stage_dir(task_dir, "official_dsh").relative_to(task_dir) == Path("b/o")

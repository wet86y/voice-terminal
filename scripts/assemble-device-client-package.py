"""Assemble a component-free remote-client validation package from the current product package."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_terminal.distribution import digest, files, manifest, verify
from voice_terminal.local_layout import default_portable
from voice_terminal.paths import atomic_json

DEVICE_HELPER = "app/config/dsh/profile/node_modules/@voice-terminal/dsh-plugin/native/win32/device-helper.ps1"
PROGRAM_ROOT_FILES = ("VoiceTerminal.exe", "Verify-Package.ps1", "使用与环境说明.md")
MODEL_WEIGHT_SUFFIXES = {
    ".bin", ".ckpt", ".ggml", ".gguf", ".h5", ".model", ".npy", ".npz",
    ".onnx", ".pb", ".pt", ".pth", ".safetensors", ".tflite",
}
CLIENT_APP_EXCLUDES = (
    "app/resources/model-seeds/",
    "app/config/resources/assets/",
    "app/tools/wheels/",
    "app/tools/aniflive-wheels/",
    "app/tools/7zip/",
)


def relative_member(name: str) -> str:
    """Validate relative package paths（校验程序包相对路径） before touching disk."""
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ":" in name
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise ValueError(f"程序包路径无效：{name!r}")
    return path.as_posix()


def source_records(archive: zipfile.ZipFile) -> tuple[dict, dict[str, dict]]:
    """Check the current product package and every source digest（逐项核对来源摘要）."""
    try:
        record = json.loads(archive.read("program-package.json"))
    except (KeyError, json.JSONDecodeError) as error:
        raise ValueError("来源缺少有效的 program-package.json（程序包清单）。") from error
    if record.get("schema") != 1 or record.get("role") != "product":
        raise ValueError("只接受当前正式 product（产品）程序包作为客户端包来源。")
    rows = record.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("来源程序包缺少文件清单。")
    expected: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("来源程序包包含无效文件记录。")
        name = relative_member(row.get("path", ""))
        folded = name.casefold()
        if folded in expected:
            raise ValueError(f"来源程序包存在重复路径：{name}")
        if (type(row.get("size")) is not int or row["size"] < 0
                or not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64):
            raise ValueError(f"来源文件记录的大小或 SHA-256（摘要）无效：{name}")
        expected[folded] = {**row, "path": name}
    actual: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        if info.is_dir() or info.filename == "program-package.json":
            continue
        name = relative_member(info.filename)
        folded = name.casefold()
        if folded in actual:
            raise ValueError(f"来源 ZIP（压缩包）存在大小写冲突路径：{name}")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise ValueError(f"来源 ZIP 不接受符号链接：{name}")
        actual[folded] = info
    if set(actual) != set(expected):
        raise ValueError("来源 ZIP 文件集合与程序包清单不一致。")
    for folded, row in expected.items():
        info = actual[folded]
        if info.file_size != row["size"]:
            raise ValueError(f"来源文件大小不符：{row['path']}")
        hasher = hashlib.sha256()
        with archive.open(info, "r") as source:
            while block := source.read(1024 * 1024):
                hasher.update(block)
        if hasher.hexdigest() != row["sha256"].lower():
            raise ValueError(f"来源文件 SHA-256（摘要）不符：{row['path']}")
    return record, expected


def keep_member(name: str) -> bool:
    """Keep client code and the single managed desktop helper（保留客户端代码与单个受管桌面辅助脚本）."""
    if any(name.startswith(prefix) for prefix in CLIENT_APP_EXCLUDES):
        return False
    if name.startswith("app/config/dsh/"):
        return (name.startswith("app/config/dsh/licenses/") or name == DEVICE_HELPER)
    if name.lower().endswith(".whl"):
        return False
    return True


def copy_source_file(archive: zipfile.ZipFile, row: dict, stage: Path) -> None:
    name = row["path"]
    destination = stage.joinpath(*PurePosixPath(name).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = archive.read(name)
    if len(payload) != row["size"] or hashlib.sha256(payload).hexdigest() != row["sha256"].lower():
        raise ValueError(f"来源文件在读取时发生变化：{name}")
    destination.write_bytes(payload)


def client_catalog(app: Path) -> None:
    path = app / "config/resources/catalog.json"
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("resources"), list):
        raise ValueError("当前模型与组件 catalog.json（目录清单）格式无效。")
    value["resources"] = []
    value["client_profile"] = "remote_device_client"
    fields = value.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("catalog.json（目录清单）字段说明无效。")
    fields["client_profile"] = "远端设备客户端不安装本机模型或 DSH 宿主组件"
    atomic_json(path, value)


def describe(root: Path, paths: list[str]) -> list[dict]:
    result = []
    for relative in paths:
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file():
            raise ValueError(f"客户端程序入口缺失：{relative}")
        result.append({"path": relative, "size": path.stat().st_size, "sha256": digest(path)})
    return result


def verify_client_tree(stage: Path, expected_build: str) -> dict:
    app = stage / "app"
    release_path = app / "release.json"
    release = json.loads(release_path.read_text(encoding="utf-8-sig"))
    release.update(
        build=expected_build,
        client_profile="remote_device_client",
        required_components=[],
        optional_components=[],
        components=[],
        root_files=describe(stage, list(PROGRAM_ROOT_FILES)),
    )
    release.pop("speaker_migration", None)
    release_files = manifest(app, kind="app", build=expected_build)["files"]
    release["files"] = release_files
    fields = release.setdefault("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("app/release.json（应用清单）字段说明无效。")
    fields.update({
        "client_profile": "只含桌面壳、静态界面和远端设备层的客户端配置",
        "required_components": "远端客户端启动所需的外置组件；本包为空",
        "optional_components": "本包不分发的模型或代理宿主组件",
    })
    atomic_json(release_path, release)
    verify(app, release)

    rows = []
    for path in files(stage):
        relative = path.relative_to(stage).as_posix()
        if relative == "program-package.json":
            continue
        rows.append({"path": relative, "size": path.stat().st_size, "sha256": digest(path)})
    package = {
        "schema": 1,
        "role": "product",
        "version": release.get("display_version", "1.0.0"),
        "build": expected_build,
        "client_profile": "remote_device_client",
        "files": rows,
        "fields": {
            "schema": "统一正式程序包格式",
            "role": "product（正式程序）",
            "version": "产品版本",
            "build": "客户端候选构建编号",
            "client_profile": "目标运行角色：remote_device_client（远端设备客户端）",
            "files": "客户端候选固定文件清单",
            "path": "工作区相对路径",
            "size": "文件字节数",
            "sha256": "文件 SHA-256（摘要）",
        },
    }
    atomic_json(stage / "program-package.json", package)

    names = {path.relative_to(stage).as_posix() for path in files(stage)}
    if any(name.startswith(("runtimes/", "models/")) for name in names):
        raise ValueError("轻量客户端包不能含外置 Python、模型或 DSH 运行目录。")
    if any(name.startswith(("app/resources/model-seeds/", "app/config/resources/assets/"))
           or name.lower().endswith((".whl", *MODEL_WEIGHT_SUFFIXES)) for name in names):
        raise ValueError("轻量客户端包仍包含模型权重或 Python 依赖包。")
    dsh_files = {name for name in names if name.startswith("app/config/dsh/")}
    if DEVICE_HELPER not in dsh_files or any(
        not name.startswith("app/config/dsh/licenses/") and name != DEVICE_HELPER
        for name in dsh_files
    ):
        raise ValueError("客户端仅可携带设备桌面辅助脚本及其许可证，不能携带 DSH 宿主或插件。")
    catalog = json.loads((app / "config/resources/catalog.json").read_text(encoding="utf-8-sig"))
    if catalog["resources"] or release["components"] or release["optional_components"]:
        raise ValueError("轻量客户端包仍登记了本机模型或代理宿主组件。")
    return package


def write_archive(stage: Path, archive_path: Path) -> None:
    candidate = archive_path.with_suffix(".pending.zip")
    if candidate.exists() or candidate.is_symlink() or candidate.is_junction():
        raise FileExistsError(f"客户端候选归档暂存目标已存在：{candidate}")
    with zipfile.ZipFile(candidate, "x", zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as output:
        for path in files(stage):
            relative = path.relative_to(stage).as_posix()
            info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = 3
            with path.open("rb") as source, output.open(info, "w", force_zip64=True) as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
    candidate.replace(archive_path)


def assemble(source: Path, output_dir: Path, *, revision: int = 1, verify_entry: bool = True) -> Path:
    source = source.resolve(strict=True)
    output_dir = output_dir.resolve()
    build_root = (ROOT / ".local/build/device-validation").resolve()
    if not output_dir.is_relative_to(build_root):
        raise ValueError("客户端验收产物必须位于受管 device-validation 构建目录。")
    if source == output_dir or source.is_dir():
        raise ValueError("客户端程序包来源必须是受管正式 ZIP（压缩包）文件。")
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        source_record, source_rows = source_records(archive)
        source_build = source_record["build"]
        if type(revision) is not int or revision < 1:
            raise ValueError("客户端候选修订号必须是正整数。")
        client_build = f"client-{source_build.removeprefix('product-')}-r{revision}"
        stage = output_dir / f"client-payload-{client_build}"
        archive_path = output_dir / f"client-package-{client_build}.zip"
        if stage.exists() or stage.is_symlink() or stage.is_junction() or archive_path.exists():
            raise FileExistsError("该客户端候选已存在；保留原件并使用新的产品构建编号。")
        stage.mkdir()
        for row in source_rows.values():
            if keep_member(row["path"]):
                copy_source_file(archive, row, stage)
    app = stage / "app"
    if not (app / "config/default.yaml").is_file() or not (app / "scripts/backend-entry.py").is_file():
        raise ValueError("当前 Tauri 启动上下文仍需要配置模板和后端入口源码；运行时解释器不会被复制。")
    client_catalog(app)
    verify_client_tree(stage, client_build)
    if verify_entry:
        result = subprocess.run(
            [str(stage / "VoiceTerminal.exe"), "--verify-package"],
            cwd=stage,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"正式本体校验拒绝客户端候选，退出码 {result.returncode}：{result.stderr[-1000:]}")
    write_archive(stage, archive_path)
    # portable.json（工作区路径布局）是每个实际安装目录的独立身份，不进入可复用程序包。
    portable = default_portable()
    portable["workspace_id"] = f"device-client-{uuid.uuid4().hex}"
    portable.setdefault("fields", {})["workspace_id"] = "本次远端客户端验收工作区编号"
    atomic_json(stage / "portable.json", portable)
    package_info = {
        "schema": 1,
        "source_build": source_build,
        "client_build": client_build,
        "archive": archive_path.name,
        "size": archive_path.stat().st_size,
        "sha256": digest(archive_path),
        "fields": {
            "schema": "轻量客户端构建记录格式",
            "source_build": "作为 UI 与原生设备层来源的正式应用构建编号",
            "client_build": "客户端候选构建编号",
            "archive": "客户端候选归档文件名",
            "size": "归档字节数",
            "sha256": "归档 SHA-256（摘要）",
        },
    }
    atomic_json(output_dir / f"{client_build}-package-info.json", package_info)
    print(f"轻量客户端候选：{archive_path}；构建 {client_build}；{archive_path.stat().st_size / 1024**2:.1f} MiB（兆二进制字节）。")
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".local/build/program-package/program.zip",
                        help="source（已校验的统一正式程序包）")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".local/build/device-validation/cleanup/fixtures",
                        help="output-dir（受管客户端验收产物目录）")
    parser.add_argument("--revision", type=int, default=1,
                        help="revision（客户端包筛选与清单修订号，生成新构建编号）")
    parser.add_argument("--skip-entry-verification", action="store_true",
                        help="skip-entry-verification（仅在无可执行本机 bootstrap 时跳过正式包验收）")
    args = parser.parse_args()
    assemble(args.source, args.output_dir, revision=args.revision,
             verify_entry=not args.skip_entry_verification)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

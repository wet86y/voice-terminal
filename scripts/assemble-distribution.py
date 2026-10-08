"""Assemble distribution（组装分发包）from an existing local baseline without modifying it."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
import zipfile
from urllib.parse import urlsplit

ROOT = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import digest, files, manifest, safe_path, verify
from voice_terminal.local_layout import default_portable
from voice_terminal.paths import atomic_json

PART_SIZE = 1024 ** 3  # 分片（每片 1 GiB）。
ARCHIVE_NAMES = {"app": "app.zip", "python": "runtime.zip", "webview2": "webview2.zip"}


def load_desktop_assembler():
    spec = importlib.util.spec_from_file_location("assemble_desktop", ROOT / "scripts/assemble-desktop.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载桌面程序收集器。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_verified_component(source: Path, destination: Path, kind: str) -> dict:
    source = Path(source)
    record_path = source / "manifest.json"
    if not record_path.is_file():
        raise FileNotFoundError(f"缺少 {kind}（组件）基线 manifest.json（文件清单）。")
    record = json.loads(record_path.read_text(encoding="utf-8-sig"))
    if record.get("kind") != kind:
        raise ValueError(f"{kind}（组件）基线清单类型不匹配。")
    verify(source, record)
    destination.mkdir(parents=True)
    for source_file in files(source):
        relative = source_file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
    verify(destination, record)
    return record


def tree_size(root: Path) -> int:
    return sum(path.stat().st_size for path in files(root))


def zip_tree(root: Path, destination: Path, arc_prefix: str) -> dict:
    """Create an uncompressed ZIP（无压缩归档）to keep CUDA files fast and byte-stable."""
    if destination.exists():
        raise FileExistsError(destination)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in files(root):
            relative = path.relative_to(root).as_posix()
            archive.write(path, f"{arc_prefix}/{relative}")
    return {"file": destination.name, "size": destination.stat().st_size, "sha256": digest(destination)}


def split_archive(path: Path) -> list[dict]:
    if path.stat().st_size <= PART_SIZE:
        return []
    parts: list[dict] = []
    with path.open("rb") as source:
        index = 0
        while True:
            part = path.with_name(f"{path.name}.part{index:03d}")
            hasher = hashlib.sha256()
            remaining = PART_SIZE
            with part.open("xb") as target:
                while remaining:
                    block = source.read(min(8 * 1024 * 1024, remaining))
                    if not block:
                        break
                    target.write(block)
                    hasher.update(block)
                    remaining -= len(block)
            if part.stat().st_size == 0:
                part.unlink()
                break
            parts.append({"url": part.name, "size": part.stat().st_size, "sha256": hasher.hexdigest(),
                          "sha512": "", "path": part.name})
            index += 1
            if remaining:
                break
    return parts


def source_base(value: str, qualification: bool) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    secure = parsed.scheme == "https" and bool(parsed.hostname)
    local_qualification = qualification and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    if (not (secure or local_qualification) or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("SourceBase（发布来源）必须是 HTTPS 地址；Qualification（本地验收）仅允许无凭据的 127.0.0.1 HTTP 地址。")
    return value


def create_catalog(output: Path, source: str, qualification: bool) -> dict:
    packages = []
    for kind, relative in (("app", "app"), ("python", "runtimes/python"), ("webview2", "runtimes/webview2")):
        root = output / relative
        filename = ARCHIVE_NAMES[kind]
        archive_path = output / filename
        archive_record = zip_tree(root, archive_path, relative)
        parts = split_archive(archive_path)
        packages.append({
            "kind": kind,  # kind（组件类别）。
            "archive": {"url": filename, "size": archive_record["size"], "sha256": archive_record["sha256"],
                        "sha512": "", "path": filename},
            "parts": parts,  # parts（大于 1 GiB 时的有序分片）。
            "unpacked_size": tree_size(root),  # unpacked_size（展开后的组件字节数）。
        })
    catalog = {
        "schema": 1,  # schema（分发清单格式）。
        "source_base": source,  # source_base（下载来源根地址）。
        "packages": packages,  # packages（应用、Python 与 WebView2 组件）。
        "fields": {
            "schema": "分发清单格式版本",
            "source_base": "发布归档的 HTTPS（加密传输）来源根地址；可留空并在部署时配置",
            "packages": "引导程序可下载的基础组件清单",
            "kind": "组件类别：app（桌面应用）、python（Python 运行环境）、webview2（网页引擎）",
            "archive": "完整 ZIP（归档）的下载地址、字节数与 SHA-256（摘要）",
            "parts": "完整归档超过 1 GiB 时，按顺序下载并合并的分片清单",
            "unpacked_size": "展开组件所需的字节数",
            "url": "归档或分片的相对下载地址",
            "size": "归档或分片的字节数",
            "sha256": "完整归档或分片的 SHA-256（摘要）",
            "sha512": "可选 SHA-512（摘要），当前包留空并使用 SHA-256",
            "path": "发布目录中的归档或分片文件名",
            "qualification": "是否允许 Qualification（本地验收）标记",
        },
        "qualification": bool(qualification),  # qualification（本地验收构建标记）。
    }
    atomic_json(output / "catalog.json", catalog)
    return catalog


def write_workspace(output: Path, build: str) -> None:
    (output / "models").mkdir(exist_ok=True)
    (output / "runtimes/dsh").mkdir(parents=True, exist_ok=True)
    portable = default_portable()
    portable["workspace_id"] = str(uuid.uuid4())
    portable["fields"]["workspace_id"] = "分发包目录的工作区稳定编号"
    atomic_json(output / "portable.json", portable)
    (output / "使用与环境说明.md").write_text(
        "# Voice Terminal distribution（分发包）\n\n"
        "启动 VoiceTerminal.exe（独立根入口）。首次运行时，应用会按内嵌 catalog.json（分发清单）获取 app.zip（桌面应用）、"
        "runtime.zip（Python 运行环境）与 webview2.zip（网页引擎），并放入所选工作区。\n\n"
        "程序发布要求 Python 与 WebView2；models（模型）和 dsh（代理运行环境）为可选组件，不随分发包携带。"
        "用户数据保存在工作区 data（数据）目录。\n\n"
        "catalog.json 中的 source_base（发布来源）可在部署归档时设置；留空时需由运行环境提供有效来源配置。\n",
        encoding="utf-8")
    stamp_path = output / "build-stamp.json"
    if not stamp_path.is_file():
        raise FileNotFoundError("缺少 build-stamp.json（构建戳）。")


def _trusted_checksum_records(output: Path) -> dict[str, tuple[int, str]]:
    """Reuse checksums（摘要）from already-verified release metadata（发行元数据）."""
    expected: dict[str, tuple[int, str]] = {}

    def add(relative: str, size: int, sha256: str) -> None:
        path = safe_path(output, relative)
        if (not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(sha256, str) or len(sha256) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in sha256)):
            raise ValueError("发行元数据中存在无效文件摘要记录。")
        key = path.relative_to(output).as_posix().casefold()
        record = (size, sha256.lower())
        if key in expected and expected[key] != record:
            raise ValueError("发行元数据中的文件摘要记录重复且不一致。")
        expected[key] = record

    catalog = json.loads((output / "catalog.json").read_text(encoding="utf-8-sig"))
    packages = catalog.get("packages") if isinstance(catalog, dict) else None
    if not isinstance(packages, list):
        raise ValueError("缺少有效的 packages（组件包）目录。")
    for package in packages:
        archive = package.get("archive") if isinstance(package, dict) else None
        if not isinstance(archive, dict):
            raise ValueError("分发目录中的 archive（归档）记录无效。")
        add(archive.get("path") or archive.get("url"), archive.get("size"), archive.get("sha256"))
        parts = package.get("parts")
        if not isinstance(parts, list):
            raise ValueError("分发目录中的 parts（分片）记录无效。")
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("分发目录中的分片记录无效。")
            add(part.get("path") or part.get("url"), part.get("size"), part.get("sha256"))

    component_roots = {"app": "app", "python": "runtimes/python", "webview2": "runtimes/webview2"}
    for kind, relative_root in component_roots.items():
        root = output / relative_root
        metadata_name = "release.json" if kind == "app" else "manifest.json"
        metadata_path = root / metadata_name
        if not metadata_path.is_file():
            raise FileNotFoundError(f"缺少 {kind}（组件）文件清单。")
        record = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        if (not isinstance(record, dict) or record.get("kind") != kind
                or not isinstance(record.get("files"), list)):
            raise ValueError(f"{kind}（组件）文件清单无效。")
        listed_paths = {metadata_name.casefold()}
        for item in record["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError(f"{kind}（组件）文件清单包含无效路径。")
            component_path = safe_path(root, item["path"])
            relative = component_path.relative_to(output).as_posix()
            key = relative.casefold()
            if key in listed_paths:
                raise ValueError(f"{kind}（组件）文件清单包含重复路径。")
            listed_paths.add(key)
            add(relative, item.get("size"), item.get("sha256"))
        metadata_relative = metadata_path.relative_to(output).as_posix()
        add(metadata_relative, metadata_path.stat().st_size, digest(metadata_path))

    return expected


def finalize(output: Path, bootstrap: Path, build_stamp: Path) -> None:
    shutil.copy2(bootstrap, output / "VoiceTerminal.exe")
    shutil.copy2(build_stamp, output / "build-stamp.json")
    trusted = _trusted_checksum_records(output)
    hashes = []
    all_paths = list(files(output))
    actual = {path.relative_to(output).as_posix().casefold(): path for path in all_paths}
    if set(trusted) - set(actual):
        raise ValueError("实际发行文件集合缺少清单记录中的路径。")
    for kind, relative_root in (("app", "app"), ("python", "runtimes/python"),
                                ("webview2", "runtimes/webview2")):
        prefix = relative_root.casefold() + "/"
        actual_component = {key for key in actual if key.startswith(prefix)}
        expected_component = {key for key in trusted if key.startswith(prefix)}
        if actual_component != expected_component:
            raise ValueError(f"{kind}（组件）文件集合与已校验清单不一致。")
    for path in sorted(all_paths, key=lambda item: item.relative_to(output).as_posix().casefold()):
        relative = path.relative_to(output).as_posix()
        if relative == "SHA256SUMS.txt":
            continue
        record = trusted.get(relative.casefold())
        if record is not None:
            size, sha256 = record
            if path.stat().st_size != size:
                raise ValueError(f"发行文件大小与已校验清单不一致：{relative}")
        else:
            sha256 = digest(path)
        hashes.append(f"{sha256}  {relative}")
    (output / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")


def finalize_only(output: Path, bootstrap: Path, build_stamp: Path) -> None:
    output = output.resolve()
    if not (output / "catalog.json").is_file():
        raise FileNotFoundError("最终化前缺少 catalog.json（分发清单）。")
    finalize(output, bootstrap.resolve(), build_stamp.resolve())


def assemble(output: Path, desktop: Path, python_runtime: Path, webview2_runtime: Path,
             source: str, qualification: bool, build_stamp: Path, *, bootstrap: Path | None = None) -> None:
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("分发暂存目录必须为空。")
    output.mkdir(parents=True, exist_ok=True)
    desktop = desktop.resolve()
    if not (desktop / "VoiceTerminalDesktop.exe").is_file():
        raise FileNotFoundError("缺少 VoiceTerminalDesktop.exe（桌面主程序）。")
    assembler = load_desktop_assembler()
    app = output / "app"
    assembler.collect_program(app, desktop)
    python_record = copy_verified_component(python_runtime, output / "runtimes/python", "python")
    webview_record = copy_verified_component(webview2_runtime, output / "runtimes/webview2", "webview2")
    app_record = manifest(app, kind="app", build=datetime.now(timezone.utc).strftime("distribution-%Y%m%d-%H%M%S"))
    components = [
        {"kind": "python", "path": "runtimes/python", "build": python_record["build"],
         "manifest_sha256": digest(output / "runtimes/python/manifest.json")},
        {"kind": "webview2", "path": "runtimes/webview2", "build": webview_record["build"],
         "manifest_sha256": digest(output / "runtimes/webview2/manifest.json")},
    ]
    app_record.update(
        display_version="0.8.0",  # display_version（显示版本）。
        channel="distribution",  # channel（启动渠道）。
        required_components=["python", "webview2"],  # required_components（必需组件）。
        optional_components=["models", "dsh"],  # optional_components（可选组件）。
        components=components,  # components（组件清单）。
        compatibility={"desktop_protocol": 1, "data_format": 1},
        fields={
            "schema": "组件清单格式版本",
            "kind": "组件类别 app（桌面应用）",
            "build": "程序构建编号",
            "files": "应用文件字节数与 SHA-256（摘要）清单",
            "display_version": "程序关于页与发布清单显示版本",
            "channel": "distribution（分发）启动渠道",
            "required_components": "启动必需组件：Python 与 WebView2",
            "optional_components": "可选组件：models（模型）与 dsh（代理运行环境）",
            "components": "随分发提供的基础组件及各自清单摘要",
            "components.kind": "组件类别",
            "components.path": "工作区内的组件相对目录",
            "components.build": "组件构建编号",
            "components.manifest_sha256": "组件 manifest.json（文件清单）的 SHA-256（摘要）",
            "compatibility": "桌面协议与用户数据格式兼容版本",
            "compatibility.desktop_protocol": "桌面协议版本",
            "compatibility.data_format": "用户数据格式版本",
        },
    )
    atomic_json(app / "release.json", app_record)
    verify(app, app_record)
    atomic_json(output / "build-stamp.json", json.loads(build_stamp.read_text(encoding="utf-8-sig")))
    write_workspace(output, str(app_record["build"]))
    create_catalog(output, source, qualification)
    if bootstrap is not None:
        finalize(output, bootstrap, build_stamp)


def refresh_app(output: Path, desktop: Path, build_stamp: Path) -> None:
    """Refresh app（刷新程序包）in a Qualification stage while preserving unchanged runtime archives."""
    output = output.resolve()
    releases = (ROOT / ".local/releases").resolve()
    if output == releases or not output.is_relative_to(releases) or output.is_symlink() or not output.is_dir():
        raise ValueError("app 刷新目标必须是 .local/releases（发布目录）内的现有验收暂存目录。")
    catalog_path = output / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    if catalog.get("qualification") is not True or not isinstance(catalog.get("packages"), list):
        raise ValueError("仅支持具有有效 packages（组件集合）的 Qualification（本地验收）分发目录。")
    package_by_kind = {row.get("kind"): row for row in catalog["packages"] if isinstance(row, dict)}
    old_release = json.loads((output / "app/release.json").read_text(encoding="utf-8-sig"))
    old_components = {row.get("kind"): row for row in old_release.get("components", []) if isinstance(row, dict)}
    for kind, relative, archive_name in (("python", "runtimes/python", "runtime.zip"),
                                         ("webview2", "runtimes/webview2", "webview2.zip")):
        manifest_path = output / relative / "manifest.json"
        package = package_by_kind.get(kind)
        archive = package.get("archive") if isinstance(package, dict) else None
        record = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        old_component = old_components.get(kind)
        if (record.get("kind") != kind or not isinstance(old_component, dict)
                or digest(manifest_path) != old_component.get("manifest_sha256")
                or not isinstance(archive, dict) or archive.get("path") != archive_name
                or not (output / archive_name).is_file()
                or (output / archive_name).stat().st_size != archive.get("size")):
            raise ValueError(f"现有 {kind}（组件）基线、ZIP（归档）或清单摘要不匹配。")
        for part in package.get("parts", []):
            if (not isinstance(part, dict) or not isinstance(part.get("path"), str)
                    or Path(part["path"]).name != part["path"]
                    or not (output / part["path"]).is_file()
                    or (output / part["path"]).stat().st_size != part.get("size")):
                raise ValueError(f"现有 {kind}（组件）ZIP 分片缺失或大小不符。")
    stamp = json.loads(build_stamp.read_text(encoding="utf-8-sig"))
    if stamp.get("launch_channel") != "distribution" or stamp.get("qualification") is not True:
        raise ValueError("当前构建戳必须属于 Qualification distribution（分发验收）渠道。")

    assembler = load_desktop_assembler()
    temporary = output / (".app-refresh-" + uuid.uuid4().hex)
    temporary.mkdir()
    candidate = temporary / "app"
    backup = output / (".app-backup-" + uuid.uuid4().hex)
    zip_backup = output / (".app-zip-backup-" + uuid.uuid4().hex)
    zip_candidate = temporary / "app.zip"
    catalog_candidate = temporary / "catalog.json"
    catalog_before = catalog_path.read_bytes()
    moved_app = moved_zip = wrote_catalog = False
    try:
        assembler.collect_program(candidate, desktop.resolve())
        components = []
        for kind, relative in (("python", "runtimes/python"), ("webview2", "runtimes/webview2")):
            record = json.loads((output / relative / "manifest.json").read_text(encoding="utf-8-sig"))
            components.append({"kind": kind, "path": relative, "build": record["build"],
                               "manifest_sha256": digest(output / relative / "manifest.json")})
        app_record = manifest(candidate, kind="app", build=datetime.now(timezone.utc).strftime("distribution-%Y%m%d-%H%M%S"))
        app_record.update(
            display_version=old_release["display_version"], channel=old_release["channel"],
            required_components=old_release["required_components"], optional_components=old_release["optional_components"],
            components=components, compatibility=old_release["compatibility"], fields=old_release["fields"])
        atomic_json(candidate / "release.json", app_record)
        verify(candidate, app_record)
        archive_record = zip_tree(candidate, zip_candidate, "app")
        parts = split_archive(zip_candidate)
        app_package = package_by_kind.get("app")
        if not isinstance(app_package, dict) or app_package.get("parts"):
            raise ValueError("catalog.json（分发清单）缺少 app（桌面应用）组件。")
        app_package["archive"] = {"url": "app.zip", "size": archive_record["size"],
                                  "sha256": archive_record["sha256"], "sha512": "", "path": "app.zip"}
        app_package["parts"] = parts
        app_package["unpacked_size"] = tree_size(candidate)
        atomic_json(catalog_candidate, catalog)

        os.replace(output / "app", backup)
        os.replace(candidate, output / "app")
        moved_app = True
        os.replace(output / "app.zip", zip_backup)
        os.replace(zip_candidate, output / "app.zip")
        moved_zip = True
        for old_part in output.glob("app.zip.part*"):
            old_part.unlink()
        for part in parts:
            os.replace(temporary / part["path"], output / part["path"])
        os.replace(catalog_candidate, catalog_path)
        wrote_catalog = True
        atomic_json(output / "build-stamp.json", stamp)
    except Exception:
        if wrote_catalog:
            atomic_json(catalog_path, json.loads(catalog_before.decode("utf-8-sig")))
        if moved_zip:
            (output / "app.zip").unlink(missing_ok=True)
        if zip_backup.exists():
            os.replace(zip_backup, output / "app.zip")
        if moved_app and (output / "app").exists():
            shutil.rmtree(output / "app")
        if backup.exists():
            os.replace(backup, output / "app")
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        zip_backup.unlink(missing_ok=True)
    print("Qualification app.zip（验收程序归档）已刷新；Python 与 WebView2 归档保持原样。", flush=True)


def _check_verified_component_layout(root: Path, kind: str, expected: dict) -> None:
    """核对已验证组件的文件集合与大小；复用摘要，不重扫大文件内容。"""
    if expected.get("kind") != kind or not isinstance(expected.get("files"), list):
        raise ValueError(f"Qualification（验收）基线的 {kind}（组件）清单无效。")
    listed = {}
    for row in expected["files"]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ValueError(f"{kind}（组件）清单包含无效文件记录。")
        path = safe_path(root, row["path"])
        key = path.relative_to(root).as_posix().casefold()
        size, sha256 = row.get("size"), row.get("sha256")
        if (key in listed or not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(sha256, str) or len(sha256) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in sha256)):
            raise ValueError(f"{kind}（组件）清单的文件路径、大小或 SHA-256（摘要）无效。")
        listed[key] = (size, sha256.lower())
    actual = {path.relative_to(root).as_posix().casefold(): path for path in files(root)}
    metadata_key = "manifest.json"
    if kind == "app":
        metadata_key = "release.json"
    if metadata_key not in actual:
        raise FileNotFoundError(f"{kind}（组件）缺少 {metadata_key}（清单文件）。")
    expected_keys = set(listed) | {metadata_key.casefold()}
    if set(actual) != expected_keys:
        raise ValueError(f"{kind}（组件）文件集合与已验证清单不一致。")
    for key, (size, _sha256) in listed.items():
        if actual[key].stat().st_size != size:
            raise ValueError(f"{kind}（组件）文件大小与已验证清单不一致：{key}")


def _hardlink_tree(source: Path, destination: Path) -> None:
    """在同卷内建立硬链接，复用已校验的 Python/WebView2 文件字节。"""
    destination.mkdir(parents=True, exist_ok=False)
    for source_file in files(source):
        relative = source_file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source_file, target)


def assemble_reusing_qualification(output: Path, desktop: Path, baseline: Path,
                                    source: str, build_stamp: Path, *,
                                    bootstrap: Path | None = None) -> None:
    """Build a production catalog while reusing already-verified runtime assets by hard link."""
    output = Path(output)
    releases = (ROOT / ".local/releases").resolve()
    baseline = baseline.resolve()
    expected_baseline = releases / "qualification"
    if output.is_symlink():
        raise ValueError("production（正式分发）暂存目录不能是链接或目录联接。")
    output = output.resolve()
    if output == releases or output.parent != releases:
        raise ValueError("production（正式分发）暂存目录必须是 .local/releases（发布目录）内的空直接子目录。")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ValueError("production（正式分发）暂存目录必须为空。")
    else:
        output.mkdir()
    if baseline != expected_baseline or baseline.is_symlink() or not baseline.is_dir():
        raise ValueError("复用来源必须是固定的 Qualification（验收）发布目录。")

    old_catalog = json.loads((baseline / "catalog.json").read_text(encoding="utf-8-sig"))
    if old_catalog.get("qualification") is not True or not isinstance(old_catalog.get("packages"), list):
        raise ValueError("复用来源不是有效的 Qualification（验收）包。")
    old_release = json.loads((baseline / "app/release.json").read_text(encoding="utf-8-sig"))
    if (old_release.get("channel") != "distribution"
            or old_release.get("required_components") != ["python", "webview2"]
            or old_release.get("optional_components") != ["models", "dsh"]):
        raise ValueError("Qualification（验收）程序的渠道或组件要求不符。")
    package_by_kind = {row.get("kind"): row for row in old_catalog["packages"] if isinstance(row, dict)}
    old_components = {row.get("kind"): row for row in old_release.get("components", []) if isinstance(row, dict)}

    sums_path = baseline / "SHA256SUMS.txt"
    if not sums_path.is_file():
        raise FileNotFoundError("Qualification（验收）包缺少 SHA256SUMS.txt（摘要表）。")
    baseline_sums = {}
    for line in sums_path.read_text(encoding="utf-8-sig").splitlines():
        parts = line.split("  ", 1)
        if (len(parts) != 2 or len(parts[0]) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in parts[0])):
            raise ValueError("Qualification（验收）摘要表包含无效记录。")
        key = parts[1].casefold()
        if key in baseline_sums:
            raise ValueError("Qualification（验收）摘要表含重复路径。")
        baseline_sums[key] = parts[0].lower()

    component_records = {}
    for kind, relative in (("python", "runtimes/python"), ("webview2", "runtimes/webview2")):
        component_root = baseline / relative
        manifest_path = component_root / "manifest.json"
        record = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        release_component = old_components.get(kind)
        package = package_by_kind.get(kind)
        archive = package.get("archive") if isinstance(package, dict) else None
        if (not isinstance(release_component, dict) or not isinstance(archive, dict)
                or record.get("kind") != kind
                or digest(manifest_path) != release_component.get("manifest_sha256")
                or record.get("build") != release_component.get("build")):
            raise ValueError(f"Qualification（验收）{kind}（组件）元数据不一致。")
        _check_verified_component_layout(component_root, kind, record)
        component_records[kind] = record
        for record_path, record_size, record_hash in (
            (relative + "/manifest.json", manifest_path.stat().st_size, digest(manifest_path)),
            *((relative + "/" + row["path"], row["size"], row["sha256"])
              for row in record["files"]),
        ):
            if baseline_sums.get(record_path.casefold()) != record_hash.lower():
                raise ValueError(f"Qualification（验收）{kind}（组件）摘要表记录不符：{record_path}")
            actual_path = safe_path(baseline, record_path)
            if not actual_path.is_file() or actual_path.stat().st_size != record_size:
                raise ValueError(f"Qualification（验收）{kind}（组件）文件大小不符：{record_path}")
        archive_path = archive.get("path") or archive.get("url")
        archive_file = safe_path(baseline, archive_path)
        if (not archive_file.is_file() or archive_file.stat().st_size != archive.get("size")
                or baseline_sums.get(archive_path.casefold()) != archive.get("sha256", "").lower()):
            raise ValueError(f"Qualification（验收）{kind}（组件）ZIP（归档）记录不符。")
        for part in package.get("parts", []):
            if not isinstance(part, dict):
                raise ValueError(f"Qualification（验收）{kind}（组件）ZIP 分片记录无效。")
            part_path = part.get("path") or part.get("url")
            part_file = safe_path(baseline, part_path)
            if (not part_file.is_file() or part_file.stat().st_size != part.get("size")
                    or baseline_sums.get(part_path.casefold()) != part.get("sha256", "").lower()):
                raise ValueError(f"Qualification（验收）{kind}（组件）ZIP 分片摘要不符。")

    # 仅复用 Python 与 WebView2（网页引擎）的已验证原始目录、归档和分片。
    for kind, relative in (("python", "runtimes/python"), ("webview2", "runtimes/webview2")):
        _hardlink_tree(baseline / relative, output / relative)
        package = package_by_kind[kind]
        archive_path = package["archive"].get("path") or package["archive"].get("url")
        os.link(safe_path(baseline, archive_path), output / Path(archive_path).name)
        for part in package.get("parts", []):
            part_path = part.get("path") or part.get("url")
            os.link(safe_path(baseline, part_path), output / Path(part_path).name)

    build_stamp_record = json.loads(build_stamp.read_text(encoding="utf-8-sig"))
    if (build_stamp_record.get("launch_channel") != "distribution"
            or build_stamp_record.get("qualification") is not False):
        raise ValueError("桌面构建戳必须属于非 Qualification distribution（正式分发）渠道。")
    shutil.copy2(build_stamp, output / "build-stamp.json")

    assembler = load_desktop_assembler()
    app = output / "app"
    assembler.collect_program(app, desktop.resolve())
    components = []
    for kind, relative in (("python", "runtimes/python"), ("webview2", "runtimes/webview2")):
        record = component_records[kind]
        components.append({"kind": kind, "path": relative, "build": record["build"],
                           "manifest_sha256": digest(output / relative / "manifest.json")})
    app_record = manifest(app, kind="app", build=datetime.now(timezone.utc).strftime("distribution-%Y%m%d-%H%M%S"))
    app_record.update(
        display_version="0.8.0",  # display_version（显示版本）。
        channel="distribution",  # channel（启动渠道）。
        required_components=["python", "webview2"],  # required_components（启动必需组件）。
        optional_components=["models", "dsh"],  # optional_components（可选组件）。
        components=components,  # components（组件清单）。
        compatibility={"desktop_protocol": 1, "data_format": 1},
        fields={
            "schema": "组件清单格式版本",
            "kind": "组件类别 app（桌面应用）",
            "build": "程序构建编号",
            "files": "应用文件字节数与 SHA-256（摘要）清单",
            "display_version": "程序关于页与发布清单显示版本",
            "channel": "distribution（分发）启动渠道",
            "required_components": "启动必需组件：Python 与 WebView2",
            "optional_components": "可选组件：models（模型）与 dsh（代理运行环境）",
            "components": "随分发提供的基础组件及各自清单摘要",
            "components.kind": "组件类别",
            "components.path": "工作区内的组件相对目录",
            "components.build": "组件构建编号",
            "components.manifest_sha256": "组件 manifest.json（文件清单）的 SHA-256（摘要）",
            "compatibility": "桌面协议与用户数据格式兼容版本",
            "compatibility.desktop_protocol": "桌面协议版本",
            "compatibility.data_format": "用户数据格式版本",
        },
    )
    atomic_json(app / "release.json", app_record)
    verify(app, app_record)
    app_archive = output / ARCHIVE_NAMES["app"]
    archive_record = zip_tree(app, app_archive, "app")
    app_parts = split_archive(app_archive)

    catalog = json.loads(json.dumps(old_catalog))
    catalog["qualification"] = False
    catalog["source_base"] = source_base(source, False)
    for package in catalog["packages"]:
        if package.get("kind") == "app":
            package["archive"] = {"url": app_archive.name, "size": archive_record["size"],
                                  "sha256": archive_record["sha256"], "sha512": "", "path": app_archive.name}
            package["parts"] = app_parts
            package["unpacked_size"] = tree_size(app)
    atomic_json(output / "catalog.json", catalog)
    write_workspace(output, app_record["build"])
    if bootstrap is not None:
        finalize(output, bootstrap, build_stamp)
    print("Production app（正式程序）与 catalog.json（分发清单）已生成；Python 与 WebView2 使用同卷硬链接复用已验证基线。", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble the app and runtime ZIP catalogs without changing .local baselines")
    parser.add_argument("--output", type=Path, required=True, help="output（空的分发暂存目录）")
    parser.add_argument("--desktop", type=Path, required=True, help="desktop（桌面主程序构建目录）")
    parser.add_argument("--python-runtime", type=Path, default=ROOT / ".local/runtimes/python", help="python-runtime（只读 Python 环境基线）")
    parser.add_argument("--webview2-runtime", type=Path, default=ROOT / ".local/runtimes/webview2", help="webview2-runtime（只读网页引擎基线）")
    parser.add_argument("--source-base", default="", help="source-base（HTTPS 发布来源；可留空）")
    parser.add_argument("--qualification", action="store_true", help="qualification（本地验收构建）")
    parser.add_argument("--reuse-components-from", type=Path,
                        help="reuse-components-from（从固定 Qualification 包硬链接复用已验证的 Python/WebView2 基线）")
    parser.add_argument("--build-stamp", type=Path, required=True, help="build-stamp（构建戳路径）")
    parser.add_argument("--bootstrap", type=Path, help="bootstrap（最终嵌入目录清单后的根启动入口）；提供后执行最终化")
    parser.add_argument("--finalize-only", action="store_true", help="finalize-only（仅复制最终启动入口并生成 SHA-256 校验表）")
    parser.add_argument("--refresh-app", action="store_true", help="refresh-app（仅刷新 Qualification app.zip 与 catalog，复用不变的 Python/WebView2 包）")
    args = parser.parse_args()
    if args.finalize_only:
        if args.bootstrap is None:
            parser.error("--finalize-only 需要 --bootstrap")
        finalize_only(args.output, args.bootstrap, args.build_stamp)
        print(f"最终根入口与 SHA256SUMS.txt（摘要表）已写入：{args.output}", flush=True)
        return
    if args.refresh_app:
        refresh_app(args.output, args.desktop, args.build_stamp)
        return
    if args.reuse_components_from is not None:
        if args.qualification:
            parser.error("--reuse-components-from 仅用于非 Qualification（非验收）分发。")
        assemble_reusing_qualification(
            args.output, args.desktop, args.reuse_components_from,
            source_base(args.source_base, False), args.build_stamp, bootstrap=args.bootstrap)
        print(f"正式分发目录与 catalog.json（分发清单）已组装：{args.output}", flush=True)
        return
    assemble(args.output, args.desktop, args.python_runtime, args.webview2_runtime,
             source_base(args.source_base, args.qualification), args.qualification, args.build_stamp,
             bootstrap=args.bootstrap)
    print(f"分发目录与 catalog.json（分发清单）已组装：{args.output}", flush=True)


if __name__ == "__main__":
    main()

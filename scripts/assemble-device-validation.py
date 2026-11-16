"""Assemble the full server-validation workspace（仅组装完整服务端验收工作区；不用于轻量客户端）。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_terminal.distribution import digest, files, verify  # noqa: E402
from voice_terminal.local_files import remove_tree  # noqa: E402

_assembler_spec = importlib.util.spec_from_file_location("desktop_assembler", ROOT / "scripts/assemble-desktop.py")
if _assembler_spec is None or _assembler_spec.loader is None:
    raise RuntimeError("无法读取正式产品的核心模型组装规则。")
_assembler = importlib.util.module_from_spec(_assembler_spec)
_assembler_spec.loader.exec_module(_assembler)


BUILD_ROOT = ROOT / ".local/build/program-package"
OUTPUT_ROOT = ROOT / ".local/build/device-validation"
OUTPUT_PATH = OUTPUT_ROOT / "server-workspace.zip"
REPORT_PATH = ROOT / ".local/cache/reports/device-validation-bundle-latest.json"
APP_DELTA_PATH = OUTPUT_ROOT / "program-package-readonly-devices.zip"
APP_DELTA_REPORT_PATH = ROOT / ".local/cache/reports/device-validation-app-delta-latest.json"


def ensure_managed_directory(path: Path) -> None:
    """ensure_managed_directory（受管目录检查）：不穿过链接或重解析点。"""
    root = ROOT.resolve()
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"验收构建路径越出仓库：{path}") from exc
    root_info = root.lstat()
    if (stat.S_ISLNK(root_info.st_mode)
            or getattr(root_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ValueError(f"验收构建路径越出仓库：{path}")
    current = root
    for part in relative.parts:
        current = current / part
        item = current
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    or not item.is_dir()):
                raise ValueError(f"验收构建目录不能穿过链接或非目录：{item}")
        else:
            item.mkdir()


def read_and_verify(root: Path, manifest_name: str, expected_kind: str | None = None) -> dict:
    """read_and_verify（读取并核验组件）：检查清单和每个登记文件摘要。"""
    manifest_path = root / manifest_name
    if not manifest_path.is_file():
        raise FileNotFoundError(f"组件缺少清单：{root.name}/{manifest_name}")
    record = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if expected_kind is not None and record.get("kind") != expected_kind:
        raise ValueError(f"组件类型不符：{root.name}")
    total = sum(int(item["size"]) for item in record["files"])
    print(f"核验 {record.get('kind', '正式应用')}：{len(record['files'])} 个文件，{total / 1024**3:.2f} GiB。", flush=True)
    verify(root, record)
    return record


def prepare_core_models() -> tuple[Path, dict]:
    """prepare_core_models（准备模型子集）：复用正式包的六个核心模型选择规则。"""
    target = OUTPUT_ROOT / "models"
    if target.exists() or target.is_symlink():
        record = read_and_verify(target, "manifest.json", "models")
        return target, record
    ensure_managed_directory(OUTPUT_ROOT)
    target.mkdir()
    _assembler.collect_models(ROOT / ".local/models", target)
    return target, read_and_verify(target, "manifest.json", "models")


def file_entries(source: Path, target_prefix: str):
    """file_entries（归档条目）：拒绝链接和重解析点，返回相对包路径。"""
    if source.is_file():
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError(f"验收包来源不能是链接：{source}")
        yield source, target_prefix
        return
    if not source.is_dir():
        raise FileNotFoundError(f"验收包来源缺失：{source}")
    for path in files(source):
        relative = path.relative_to(source).as_posix()
        yield path, f"{target_prefix}/{relative}"


def assemble() -> dict:
    """assemble（组装）：只纳入产品、运行组件和模型，不纳入用户数据或工具链。"""
    payload = BUILD_ROOT / "payload"
    package_info_path = BUILD_ROOT / "package-info.json"
    if not package_info_path.is_file():
        raise FileNotFoundError("缺少已准备的统一产品包记录；请先执行 npm run desktop:prepare。")
    package_info = json.loads(package_info_path.read_text(encoding="utf-8-sig"))
    archive_source = BUILD_ROOT / "program.zip"
    if not archive_source.is_file() or digest(archive_source) != package_info.get("sha256"):
        raise ValueError("统一产品包摘要与准备记录不符。")

    ensure_managed_directory(OUTPUT_ROOT)
    app = payload / "app"
    release = read_and_verify(app, "release.json")
    components = {
        "runtimes/python": (ROOT / ".local/runtimes/python", "python"),
        "runtimes/dsh": (ROOT / ".local/runtimes/dsh", "dsh"),
    }
    component_records = {}
    for target, (source, kind) in components.items():
        component_records[target] = read_and_verify(source, "manifest.json", kind)
    models_root, models_record = prepare_core_models()
    components["models"] = (models_root, "models")
    component_records["models"] = models_record

    # The product archive separates optional runtimes from app/release.json.
    # This portable acceptance workspace includes those exact, verified trees,
    # so bind their manifests in the workspace copy without changing the
    # canonical product package under .local/build/program-package.
    bound_release = json.loads(json.dumps(release))
    included_paths = set(components)
    bound_release["components"] = [
        row for row in bound_release.get("components", [])
        if not isinstance(row, dict) or row.get("path") not in included_paths
    ]
    component_bindings = []
    for relative, (source, kind) in components.items():
        record = component_records[relative]
        binding = {
            "kind": kind,
            "path": relative,
            "build": record["build"],
            "manifest_sha256": digest(source / "manifest.json"),
            "optional": True,
        }
        bound_release["components"].append(binding)
        component_bindings.append(binding)
    encoded_release = json.dumps(bound_release, ensure_ascii=False, indent=2).encode("utf-8")

    package_manifest_path = payload / "program-package.json"
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8-sig"))
    release_entry = next(
        (item for item in package_manifest.get("files", []) if item.get("path") == "app/release.json"),
        None,
    )
    if release_entry is None:
        raise ValueError("正式产品包清单缺少 app/release.json（应用发布清单）。")
    release_entry["size"] = len(encoded_release)
    release_entry["sha256"] = hashlib.sha256(encoded_release).hexdigest()
    encoded_package_manifest = json.dumps(package_manifest, ensure_ascii=False, indent=2).encode("utf-8")

    portable = {
        "schema": 2,
        "app": "app",
        "python": "runtimes/python/python.exe",
        "models": "models",
        "dsh": "runtimes/dsh",
        "webview2": None,
        "data_dir": "data",
        "workspace_id": f"device-validation-{package_info['build'].removeprefix('product-')}",
        "fields": {
            "schema": "便携配置格式版本",
            "app": "正式产品应用目录",
            "python": "产品后端解释器相对路径",
            "models": "经完整性核验的模型组件目录",
            "dsh": "正式代理运行组件目录",
            "webview2": "使用主机系统网页引擎",
            "data_dir": "验收专用持久数据目录",
            "workspace_id": "与用户正式工作区隔离的验收工作区编号",
        },
    }
    info = {
        "schema_version": 1,
        "product_build": package_info["build"],
        "product_version": release.get("display_version"),
        "workspace_id": portable["workspace_id"],
        "components": {
            path: {
                "kind": record["kind"],
                "build": record["build"],
                "manifest_sha256": digest(source / "manifest.json"),
                "file_count": len(record["files"]),
                "bytes": sum(item["size"] for item in record["files"]),
            }
            for path, (source, _kind) in components.items()
            for record in (component_records[path],)
        },
        "component_bindings": component_bindings,
        "included_paths": ["app", "VoiceTerminal.exe", "Verify-Package.ps1", "使用与环境说明.md",
                           "program-package.json", "portable.json", *components.keys()],
        "workspace_release_sha256": hashlib.sha256(encoded_release).hexdigest(),
        "excluded": "用户数据、凭据、构建工具链、源码检出、其他工作区与缓存",
        "fields": {
            "schema_version": "验收工作区包格式版本",
            "product_build": "已核验的正式程序构建编号",
            "product_version": "正式产品显示版本",
            "workspace_id": "本次验收工作区编号",
            "components": "已核验的产品运行组件摘要",
            "component_bindings": "在验收副本发布清单内绑定的已核验组件",
            "workspace_release_sha256": "验收副本应用发布清单 SHA-256 摘要",
            "kind": "组件类型",
            "build": "组件构建编号",
            "manifest_sha256": "组件清单摘要",
            "file_count": "清单登记文件数",
            "bytes": "组件登记文件字节数",
            "included_paths": "归档内的相对路径集合",
            "excluded": "明确排除的本机数据和开发资产",
        },
    }

    ensure_managed_directory(OUTPUT_ROOT)
    ensure_managed_directory(REPORT_PATH.parent)
    if OUTPUT_PATH.exists():
        output_info = OUTPUT_PATH.lstat()
        if (stat.S_ISLNK(output_info.st_mode)
                or getattr(output_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or not OUTPUT_PATH.is_file()):
            raise ValueError(f"验收归档目标必须是普通文件：{OUTPUT_PATH}")
    entries = []
    entries.extend(file_entries(app, "app"))
    for name in ("VoiceTerminal.exe", "Verify-Package.ps1", "使用与环境说明.md", "program-package.json"):
        entries.extend(file_entries(payload / name, name))
    for relative, (source, _kind) in components.items():
        entries.extend(file_entries(source, relative))
    metadata_overrides = {
        "app/release.json": encoded_release,
        "program-package.json": encoded_package_manifest,
    }
    encoded_portable = json.dumps(portable, ensure_ascii=False, indent=2).encode("utf-8")
    encoded_info = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")

    seen: set[str] = set()
    total_size = sum(
        len(metadata_overrides[relative]) if relative in metadata_overrides else source.stat().st_size
        for source, relative in entries
    )
    bundle_id = uuid.uuid4().hex
    staging_root = OUTPUT_ROOT / f"hardlink-stage-{bundle_id}"
    temporary = OUTPUT_ROOT / f"server-workspace-{bundle_id}.pending.zip"
    if staging_root.exists() or staging_root.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise FileExistsError("验收归档的唯一暂存路径已占用，保留现场并停止。")
    archive_tool = ROOT / ".local/build/tools/7zip/7z.exe"
    if not archive_tool.is_file():
        raise FileNotFoundError("缺少本机受管 7-Zip 归档工具；不在远端安装工具链。")
    staging_root.mkdir()
    try:
        for source, relative in sorted(entries, key=lambda item: item[1].casefold()):
            normalized = relative.replace("\\", "/")
            if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts or ":" in normalized:
                raise ValueError(f"验收包路径无效：{relative}")
            if normalized.casefold() in seen:
                raise ValueError(f"验收包含重复路径：{relative}")
            seen.add(normalized.casefold())
            linked = staging_root.joinpath(*Path(normalized).parts)
            linked.parent.mkdir(parents=True, exist_ok=True)
            if relative in metadata_overrides:
                linked.write_bytes(metadata_overrides[relative])
            else:
                os.link(source, linked)
        (staging_root / "portable.json").write_bytes(encoded_portable)
        (staging_root / "acceptance-bundle.json").write_bytes(encoded_info)

        archive_members = sorted(path.name for path in staging_root.iterdir())
        command = [str(archive_tool), "a", "-tzip", "-mx=0", "-bd", "-y", str(temporary), *archive_members]
        print(f"使用本机受管 7-Zip 归档器写入约 {total_size / 1024**3:.1f} GiB 验收内容。", flush=True)
        completed = subprocess.run(command, cwd=staging_root, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"本机验收归档失败，7-Zip 退出码为 {completed.returncode}。")
        with zipfile.ZipFile(temporary, "r") as archive:
            top_level = {name.replace("\\", "/").split("/", 1)[0] for name in archive.namelist() if name}
            required_top = {"app", "runtimes", "models", "VoiceTerminal.exe", "portable.json", "acceptance-bundle.json"}
            if not required_top.issubset(top_level):
                raise ValueError("7-Zip 归档缺少验收工作区必要入口或组件目录。")
        os.replace(temporary, OUTPUT_PATH)
    finally:
        if temporary.exists() or temporary.is_symlink():
            remove_tree(temporary, OUTPUT_ROOT)
        if staging_root.exists() or staging_root.is_symlink():
            remove_tree(staging_root, OUTPUT_ROOT)

    result = {
        "archive": str(OUTPUT_PATH.relative_to(ROOT)).replace("\\", "/"),
        "size_bytes": OUTPUT_PATH.stat().st_size,
        "sha256": digest(OUTPUT_PATH),
        "product_build": package_info["build"],
        "workspace_id": portable["workspace_id"],
        "components": info["components"],
        "fields": {
            "archive": "远端验收工作区归档相对路径",
            "size_bytes": "便携验收归档字节数",
            "sha256": "便携验收归档 SHA-256 摘要",
            "product_build": "正式产品构建编号",
            "workspace_id": "与用户数据隔离的验收工作区编号",
            "components": "已核验运行组件摘要",
        },
    }
    if REPORT_PATH.exists() or REPORT_PATH.is_symlink():
        report_info = REPORT_PATH.lstat()
        if (stat.S_ISLNK(report_info.st_mode)
                or getattr(report_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or not REPORT_PATH.is_file()):
            raise ValueError(f"验收构建报告目标必须是普通文件：{REPORT_PATH}")
    REPORT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def assemble_app_delta(output_path: Path = APP_DELTA_PATH) -> dict:
    """assemble_app_delta（组装应用差量包）：绑定复用组件，仅归档更新后的正式应用。"""
    archive_path = Path(output_path)
    if not archive_path.is_absolute():
        archive_path = ROOT / archive_path
    archive_path = Path(os.path.abspath(archive_path))
    ensure_managed_directory(archive_path.parent)
    try:
        archive_path.relative_to(OUTPUT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("应用差量候选必须保存在固定本机验收构建目录内。") from exc
    payload = BUILD_ROOT / "payload"
    package_info_path = BUILD_ROOT / "package-info.json"
    archive_source = BUILD_ROOT / "program.zip"
    if not package_info_path.is_file() or not archive_source.is_file():
        raise FileNotFoundError("缺少本机正式产品包及其构建记录。")
    package_info = json.loads(package_info_path.read_text(encoding="utf-8-sig"))
    if digest(archive_source) != package_info.get("sha256"):
        raise ValueError("本机正式产品包摘要与构建记录不符。")

    prior_receipt_path = OUTPUT_ROOT / "device-validation-bundle-before-readonly-devices.json"
    if not prior_receipt_path.is_file():
        raise FileNotFoundError("缺少前一完整验收包的组件验证收据；拒绝复用未经核对的组件。")
    prior_receipt = json.loads(prior_receipt_path.read_text(encoding="utf-8-sig"))
    prior_components = prior_receipt.get("components")
    if not isinstance(prior_components, dict):
        raise ValueError("前一验收收据没有组件清单。")

    payload_app = payload / "app"
    release = read_and_verify(payload_app, "release.json")
    package_manifest_path = payload / "program-package.json"
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8-sig"))
    entries = package_manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("正式产品包文件清单无效。")
    package_entry = next((item for item in entries if item.get("path") == "app/release.json"), None)
    if package_entry is None:
        raise ValueError("正式产品包清单缺少 app/release.json（应用发布清单）。")

    components = {
        "runtimes/python": (ROOT / ".local/runtimes/python", "python"),
        "runtimes/dsh": (ROOT / ".local/runtimes/dsh", "dsh"),
        "models": (OUTPUT_ROOT / "models", "models"),
    }
    component_records: dict[str, dict] = {}
    component_bindings: list[dict] = []
    for relative, (source, kind) in components.items():
        prior = prior_components.get(relative)
        manifest_path = source / "manifest.json"
        if not isinstance(prior, dict) or not manifest_path.is_file():
            raise ValueError(f"缺少先前验证收据或当前组件清单：{relative}")
        record = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        current_manifest_hash = digest(manifest_path)
        if (record.get("kind") != kind or record.get("build") != prior.get("build")
                or current_manifest_hash != prior.get("manifest_sha256")):
            raise ValueError(f"复用组件身份与先前完整验收不符：{relative}")
        component_records[relative] = record
        component_bindings.append({
            "kind": kind,
            "path": relative,
            "build": record["build"],
            "manifest_sha256": current_manifest_hash,
            "optional": True,
        })

    bound_release = json.loads(json.dumps(release))
    included_paths = set(components)
    bound_release["components"] = [
        row for row in bound_release.get("components", [])
        if not isinstance(row, dict) or row.get("path") not in included_paths
    ]
    bound_release["components"].extend(component_bindings)
    encoded_release = json.dumps(bound_release, ensure_ascii=False, indent=2).encode("utf-8")
    package_entry["size"] = len(encoded_release)
    package_entry["sha256"] = hashlib.sha256(encoded_release).hexdigest()
    encoded_package_manifest = json.dumps(package_manifest, ensure_ascii=False, indent=2).encode("utf-8")

    ensure_managed_directory(APP_DELTA_REPORT_PATH.parent)
    if archive_path.exists() or archive_path.is_symlink():
        raise FileExistsError(f"应用差量候选已存在；保留原件并停止：{archive_path}")
    if APP_DELTA_REPORT_PATH.exists() or APP_DELTA_REPORT_PATH.is_symlink():
        report_info = APP_DELTA_REPORT_PATH.lstat()
        if (stat.S_ISLNK(report_info.st_mode)
                or getattr(report_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or not APP_DELTA_REPORT_PATH.is_file()):
            raise ValueError(f"应用差量报告必须是普通文件：{APP_DELTA_REPORT_PATH}")

    names: set[str] = set()
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=3, allowZip64=True) as archive:
        for row in entries:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise ValueError("正式产品包包含无效文件记录。")
            name = row["path"].replace("\\", "/")
            pure = Path(name)
            if (not name or pure.is_absolute() or ".." in pure.parts or ":" in name
                    or name.casefold() in names):
                raise ValueError(f"正式产品包相对路径无效或重复：{name}")
            names.add(name.casefold())
            source = payload.joinpath(*pure.parts)
            if not source.is_file() or source.is_symlink():
                raise FileNotFoundError(f"正式产品包来源文件缺失或为链接：{name}")
            if name == "app/release.json":
                data = encoded_release
            else:
                if source.stat().st_size != int(row.get("size", -1)) or digest(source) != row.get("sha256"):
                    raise ValueError(f"正式产品包来源与登记摘要不符：{name}")
                data = None
            if data is None:
                archive.write(source, name)
            else:
                archive.writestr(name, data)
        if "program-package.json" in names:
            raise ValueError("程序包文件清单不能递归登记自身。")
        archive.writestr("program-package.json", encoded_package_manifest)

    with zipfile.ZipFile(archive_path, "r") as archive:
        if archive.testzip() is not None:
            raise ValueError("应用差量验收包 CRC（循环冗余校验）检查失败。")
        if archive.read("program-package.json") != encoded_package_manifest:
            raise ValueError("应用差量验收包内的产品清单与预期不一致。")
        bundled_release = json.loads(archive.read("app/release.json"))
        if bundled_release.get("components") != component_bindings:
            raise ValueError("应用差量验收包内的运行组件绑定不一致。")

    result = {
        "schema_version": 1,
        "artifact_type": "product_app_delta",
        "archive": str(archive_path.relative_to(ROOT)).replace("\\", "/"),
        "size_bytes": archive_path.stat().st_size,
        "sha256": digest(archive_path),
        "product_build": package_info["build"],
        "workspace_id": f"device-validation-{package_info['build'].removeprefix('product-')}",
        "source_program_package_sha256": package_info["sha256"],
        "component_bindings": component_bindings,
        "reused_component_receipt": str(prior_receipt_path.relative_to(ROOT)).replace("\\", "/"),
        "fields": {
            "schema_version": "差量验收包报告格式版本",
            "artifact_type": "产品应用差量类型",
            "archive": "本机受管应用差量包路径",
            "size_bytes": "应用差量包字节数",
            "sha256": "应用差量包 SHA-256 摘要",
            "product_build": "正式产品构建编号",
            "workspace_id": "验收工作区编号",
            "source_program_package_sha256": "本机正式产品包摘要",
            "component_bindings": "复用且身份与之前完整验收匹配的组件",
            "reused_component_receipt": "组件此前完整验证收据路径",
            "kind": "组件类型",
            "path": "便携工作区内的组件相对路径",
            "build": "组件构建编号",
            "manifest_sha256": "组件清单摘要",
            "optional": "发行清单中的外置组件标识",
        },
    }
    report_temp = APP_DELTA_REPORT_PATH.with_name(f"{APP_DELTA_REPORT_PATH.name}.{uuid.uuid4().hex}.tmp")
    report_temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(report_temp, APP_DELTA_REPORT_PATH)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument(
        "--server-workspace",
        action="store_true",
        help="server-workspace（明确确认生成包含 Python、DSH 和模型的完整服务端工作区归档）",
    )
    parser.add_argument("--archive", type=Path, default=OUTPUT_PATH,
        help="archive（固定验收构建目录内的归档输出路径；默认路径冲突时可指定构建专属候选名）")
    operation.add_argument("--app-delta", action="store_true", help="app-delta（只生成已绑定组件的正式应用差量包）")
    args = parser.parse_args()
    if args.app_delta:
        result = assemble_app_delta(args.archive)
    else:
        if args.archive.resolve() != OUTPUT_PATH.resolve():
            raise ValueError("验收工作区归档只能写入本机固定 .local/build/device-validation 路径。")
        result = assemble()
    if args.app_delta:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))

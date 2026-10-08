#!/usr/bin/env python3
"""Resource package tool（资源包工具）：export or prevalidate ZIP64 resource bundles."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile
import uuid


class _CliArgumentError(ValueError):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliArgumentError(message)


def _defaults() -> tuple[Path, Path]:
    program = Path(os.environ.get("VOICE_PROGRAM_ROOT") or Path(__file__).resolve().parents[1]).resolve()
    explicit = os.environ.get("VOICE_INSTALL_ROOT")
    if explicit:
        root = Path(explicit).resolve()
    elif (program / ".local" / "portable.json").is_file():
        root = program / ".local"
    elif (program / "portable.json").is_file():
        root = program
    elif (program.parent / "portable.json").is_file():
        root = program.parent
    else:
        root = program / ".local"
    return program, root


def _parser() -> argparse.ArgumentParser:
    program, root = _defaults()
    parser = _JsonArgumentParser(description="Export or validate locked model resource packages")
    parser.add_argument("--root", type=Path, default=root, help="workspace or local install root (工作区或 .local 安装根目录)")
    parser.add_argument("--program", type=Path, default=program, help="program/catalog root (程序与清单目录)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_paths(item: argparse.ArgumentParser) -> None:
        item.add_argument("--root", type=Path, default=argparse.SUPPRESS, help="workspace or local install root (工作区或 .local 安装根目录)")
        item.add_argument("--program", type=Path, default=argparse.SUPPRESS, help="program/catalog root (程序与清单目录)")

    export = sub.add_parser("export", help="export installed resources to a ZIP64 bundle (导出 ZIP64 资源包)")
    add_paths(export)
    export.add_argument("--output", type=Path, required=True, help="output bundle path (导出压缩包路径)")
    export.add_argument("--resource-id", action="append", help="resource ID to include; repeatable (资源编号，可重复)")
    validate = sub.add_parser("validate", help="prevalidate a bundle without installing it (预校验资源包，不安装)")
    add_paths(validate)
    validate.add_argument("--source", type=Path, required=True, help="ZIP64 resources package (ZIP64 资源包)")
    validate.add_argument("--task-id", default=uuid.uuid4().hex, help="temporary task ID (临时任务编号)")
    return parser


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _export_model(manager, spec) -> dict:
    state = manager._model_state(spec)
    if state["state"] != "installed":
        raise ValueError("所选模型当前未按固定清单安装。")
    target = manager._spec_target(spec)
    files = []
    for item in spec.files:
        source = target.joinpath(*item.path.split("/"))
        if not source.exists() and item.optional:
            continue
        manager._check_source_file(source, item)
        package_path = f"resources/{spec.resource_id}/{item.path}"
        files.append({
            "path": item.path,
            "package_path": package_path,
            "size_bytes": source.stat().st_size,
            "sha256": _hash(source),
        })
    return {
        "resource_id": spec.resource_id,
        "kind": "model",
        "version": spec.version,
        "destination": spec.destination,
        "origin": "locked_catalog",
        "source": {key: value for key, value in spec.source.items() if key in {"platform", "repo_id", "revision", "page_url"}},
        "files": files,
    }


def _export_dsh_legacy(manager, spec) -> dict:
    """Export the installed legacy runtime with a non-official provenance label."""
    target = manager._spec_target(spec)
    try:
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8-sig"))
        runtime = json.loads((target / "runtime.json").read_text(encoding="utf-8-sig"))
        lock = json.loads((manager.program / "config" / "dsh" / "runtime.lock.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("当前 DSH 兼容运行目录缺少有效清单。") from exc
    if (manifest.get("schema") != 1 or manifest.get("kind") != "dsh" or not manifest.get("files")
            or runtime.get("version") != lock.get("version") or runtime.get("commit") != lock.get("commit")):
        raise ValueError("当前 DSH 运行目录与本项目保留的兼容版本不匹配。")
    expected_manifest = (manager.catalog.get("dsh").legacy_export or {}).get("manifest_sha256")
    if not isinstance(expected_manifest, str) or _hash(target / "manifest.json") != expected_manifest:
        raise ValueError("当前 DSH 运行清单与本项目固定的旧兼容版本不匹配。")
    records = []
    names: set[str] = set()
    for entry in manifest["files"]:
        rel = entry.get("path")
        if not isinstance(rel, str) or "\\" in rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise ValueError("DSH 兼容运行清单包含无效路径。")
        path = target.joinpath(*rel.split("/"))
        if not path.is_file() or path.is_symlink() or path.stat().st_size != entry.get("size") or _hash(path) != entry.get("sha256"):
            raise ValueError("DSH 兼容运行清单与本机文件摘要不匹配。")
        package_path = f"resources/dsh/{rel}"
        records.append({"path": rel, "package_path": package_path, "size_bytes": path.stat().st_size,
                        "sha256": entry["sha256"]})
        names.add(rel)
    manifest_file = target / "manifest.json"
    if "manifest.json" not in names:
        records.append({"path": "manifest.json", "package_path": "resources/dsh/manifest.json",
                        "size_bytes": manifest_file.stat().st_size, "sha256": _hash(manifest_file)})
    return {
        "resource_id": "dsh",
        "kind": "dsh",
        "version": runtime["version"],
        "destination": "runtimes/dsh",
        "origin": "legacy_runtime",
        "trust_level": "legacy_compatibility_only",
        "source": {"repository": lock.get("repository"), "tag": lock.get("tag"), "commit": lock.get("commit")},
        "files": records,
    }


def _export_dsh_official(manager, spec) -> dict:
    """Export only a fully validated official tree bound by the local program manifest."""
    target = manager._spec_target(spec)
    paths, manifest_sha256 = manager._inspect_official_dsh_tree(target)
    records = []
    names: set[str] = set()
    for item in paths.manifest["files"]:
        rel = item["path"]
        path = target.joinpath(*Path(rel).parts)
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != item["size"]
                or _hash(path) != item["sha256"]):
            raise ValueError(f"官方 DSH 运行清单与本机文件摘要不匹配：{rel}")
        records.append({"path": rel, "package_path": f"resources/dsh/{rel}",
                        "size_bytes": path.stat().st_size, "sha256": item["sha256"]})
        names.add(rel)
    manifest_file = target / "manifest.json"
    if "manifest.json" not in names:
        records.append({"path": "manifest.json", "package_path": "resources/dsh/manifest.json",
                        "size_bytes": manifest_file.stat().st_size, "sha256": _hash(manifest_file)})
    return {
        "resource_id": "dsh",
        "kind": "dsh",
        "version": paths.metadata["version"],
        "destination": manager.dsh_relative,
        "origin": "official_runtime",
        "trust_level": "program_attested_official_runtime",
        "source_installer_sha512": paths.metadata["source_sha512"],
        "manifest_sha256": manifest_sha256,
        "files": records,
    }


def _resource_entries(manager, requested: list[str] | None) -> list[tuple[dict, list[tuple[str, Path]]]]:
    if requested:
        specs = [manager.catalog.get(resource_id) for resource_id in requested]
        if len({spec.resource_id for spec in specs}) != len(specs):
            raise ValueError("资源编号重复。")
    else:
        specs = list(manager.catalog.specs.values())
    entries: list[tuple[dict, list[tuple[str, Path]]]] = []
    for spec in specs:
        if spec.kind == "dsh":
            state = manager._dsh_state(spec)
            if not requested and state["state"] != "installed":
                continue
            if requested and state["state"] != "installed":
                raise ValueError("当前 DSH 兼容运行目录尚未安装或不完整。")
            entry = (_export_dsh_official(manager, spec) if state.get("origin") == "official_runtime"
                     else _export_dsh_legacy(manager, spec))
            target = manager._spec_target(spec)
            files = [(record["package_path"], target.joinpath(*record["path"].split("/"))) for record in entry["files"]]
            entries.append((entry, files))
        else:
            state = manager._model_state(spec)
            if not requested and state["state"] != "installed":
                continue
            entry = _export_model(manager, spec)
            target = manager._spec_target(spec)
            files = [(record["package_path"], target.joinpath(*record["path"].split("/"))) for record in entry["files"]]
            entries.append((entry, files))
    if not entries:
        raise ValueError("没有找到可导出的已安装资源。")
    return entries


def _export(manager, output: Path, resource_ids: list[str] | None) -> dict:
    entries = _resource_entries(manager, resource_ids)
    resources = [entry for entry, _ in entries]
    file_items = [item for _, files in entries for item in files]
    output = output.expanduser().resolve()
    protected = [manager.models_root.resolve(), manager._spec_target(manager.catalog.get("dsh")).resolve()]
    if any(output == folder or folder in output.parents for folder in protected):
        raise ValueError("资源包不能写入正在打包的模型或 DSH 运行目录。")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".resource-export.part", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    index = {
        "schema_version": 1,
        "format": "ZIP64",
        "resources": resources,
        "fields": {
            "schema_version": "离线资源包清单格式版本", "format": "压缩容器格式", "resources": "资源条目列表",
            "resource_id": "资源稳定编号", "version": "项目认可版本", "destination": "安装目标相对路径",
            "origin": "该资源在本机的导出来源", "trust_level": "来源可信等级",
            "files": "包内文件清单", "path": "资源内相对路径", "package_path": "ZIP 内相对路径",
            "size_bytes": "文件字节数", "sha256": "SHA-256（摘要）十六进制值",
            "source_installer_sha512": "固定官方安装器 SHA-512（摘要）",
            "manifest_sha256": "本机程序签发并绑定的完整运行清单 SHA-256（摘要）",
        },
    }
    total_bytes = 0
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            bundle.writestr("resources.json", json.dumps(index, ensure_ascii=False, indent=2) + "\n")
            for package_path, source in file_items:
                bundle.write(source, package_path)
                total_bytes += source.stat().st_size
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"output_path": str(output), "resource_ids": [item["resource_id"] for item in resources],
            "file_count": len(file_items), "payload_bytes": total_bytes, "zip64": True}


def _write_json(value: dict) -> None:
    if "ok" in value:
        value.setdefault("fields", {"ok": "操作是否成功", "result": "操作结果", "error": "安全错误对象",
                                     "code": "稳定错误代码", "message": "不包含凭据的用户提示"})
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _CliArgumentError:
        _write_json({"ok": False, "error": {"code": "invalid_arguments", "message": "资源包命令参数缺失或格式无效。"}})
        return 2
    sys.path.insert(0, str(args.program.resolve()))
    resource_error_type = None
    try:
        from voice_terminal.resource_distribution import ResourceError, _install_lock, make_manager
        resource_error_type = ResourceError

        manager = make_manager(args.program, args.root)
        if args.command == "validate":
            result = manager.validate_bundle(args.source, args.task_id)
        else:
            with _install_lock(manager.root):
                result = _export(manager, args.output, args.resource_id)
        _write_json({"ok": True, "result": result})
        return 0
    except Exception as exc:
        if resource_error_type is not None and isinstance(exc, resource_error_type):
            error = {"code": exc.code, "message": exc.message, **exc.details}
        else:
            error = {"code": "package_operation_failed", "message": "资源包操作失败；未输出本机文件内容或下载凭据。"}
        _write_json({"ok": False, "error": error})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

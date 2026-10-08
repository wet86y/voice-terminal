#!/usr/bin/env python3
"""Resource CLI（资源命令行）：JSON interface for the native desktop manager."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid


class _CliArgumentError(ValueError):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliArgumentError(message)


def _defaults() -> tuple[Path, Path]:
    program = Path(os.environ.get("VOICE_PROGRAM_ROOT") or Path(__file__).resolve().parents[1]).resolve()
    explicit_install = os.environ.get("VOICE_INSTALL_ROOT")
    if explicit_install:
        install = Path(explicit_install).resolve()
    elif (program / ".local" / "portable.json").is_file():
        install = program / ".local"
    elif (program / "portable.json").is_file():
        install = program
    elif (program.parent / "portable.json").is_file():
        install = program.parent
    else:
        install = program / ".local"
    return program, install


def _parser() -> argparse.ArgumentParser:
    program_default, root_default = _defaults()
    parser = _JsonArgumentParser(description="Locked model resource status, download plan, and import manager")
    parser.add_argument("--root", type=Path, default=root_default, help="workspace or local install root (工作区或 .local 安装根目录)")
    parser.add_argument("--program", type=Path, default=program_default, help="program/catalog root (程序与清单目录)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_paths(item: argparse.ArgumentParser) -> None:
        # Accept explicit path flags before or after the subcommand; SUPPRESS keeps global values intact.
        item.add_argument("--root", type=Path, default=argparse.SUPPRESS, help="workspace or local install root (工作区或 .local 安装根目录)")
        item.add_argument("--program", type=Path, default=argparse.SUPPRESS, help="program/catalog root (程序与清单目录)")

    status = sub.add_parser("status", help="show locked resource state (读取资源状态)")
    add_paths(status)
    status.add_argument("--resource-id", help="stable resource ID (资源编号)")

    for command in ("prepare", "download-plan"):
        item = sub.add_parser(command, help="return a fixed transfer plan (返回固定下载计划)")
        add_paths(item)
        item.add_argument("--resource-id", required=True, help="stable resource ID (资源编号)")
        item.add_argument("--task-id", required=True, help="stable task ID (任务编号)")

    preview = sub.add_parser("preview", help="prevalidate a bundle or model folder without installing it (安装前预校验)")
    add_paths(preview)
    preview.add_argument("--source", type=Path, required=True, help="folder or resources bundle (资源文件夹或批量包)")
    preview.add_argument("--resource-id", help="stable resource ID for a single-resource source (单资源编号)")
    preview.add_argument("--task-id", required=True, help="stable task ID (任务编号)")

    for command in ("install", "import"):
        item = sub.add_parser(command, help="install a verified folder or ZIP64 bundle (安装校验后的文件夹或资源包)")
        add_paths(item)
        item.add_argument("--source", type=Path, required=True, help="folder, locked archive, or resources bundle (资源目录或压缩包)")
        item.add_argument("--resource-id", help="stable resource ID for a single-resource import (单资源编号；批量包自动识别)")
        item.add_argument("--task-id", default=None, help="stable task ID (任务编号)")

    worker = sub.add_parser("download-modelscope", help="download and install a fixed ModelScope resource (魔搭 SDK 下载与安装)")
    add_paths(worker)
    worker.add_argument("--resource-id", required=True, help="stable resource ID (资源编号)")
    worker.add_argument("--task-id", required=True, help="stable task ID (任务编号)")
    worker.add_argument("--download-only", action="store_true", help="download and validate only (仅下载并校验，暂不安装)")
    return parser


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
        _write_json({"ok": False, "error": {"code": "invalid_arguments", "message": "资源命令参数缺失或格式无效。"}})
        return 2
    sys.path.insert(0, str(args.program.resolve()))
    resource_error_type = None
    try:
        # Local import keeps ordinary CLI parsing independent of model and voice-service modules.
        from voice_terminal.resource_distribution import ResourceError, make_manager
        resource_error_type = ResourceError

        manager = make_manager(args.program, args.root)
        if args.command == "status":
            if manager.transaction_marker.exists():
                manager.recover_pending_transaction()
            result = manager.status(args.resource_id)
            _write_json({"ok": True, "result": result})
            return 0
        if args.command == "download-plan":
            _write_json({"ok": True, "result": manager.download_plan(args.resource_id, args.task_id)})
            return 0
        if args.command == "preview":
            _write_json({"ok": True, "result": manager.preview_source(args.source, resource_id=args.resource_id, task_id=args.task_id)})
            return 0
        if args.command == "prepare":
            _write_json({"ok": True, "result": manager.prepare(args.resource_id, args.task_id)})
            return 0
        if args.command in {"install", "import"}:
            task_id = args.task_id or uuid.uuid4().hex
            result = manager.import_source(args.source, resource_id=args.resource_id, task_id=task_id)
            _write_json({"ok": True, "result": result})
            return 0
        if args.command == "download-modelscope":
            result = manager.download_modelscope(args.resource_id, args.task_id, download_only=args.download_only)
            _write_json({"ok": True, "result": result})
            return 0
    except Exception as exc:
        if resource_error_type is not None and isinstance(exc, resource_error_type):
            error = {"code": exc.code, "message": exc.message}
            if exc.details:
                error.update(exc.details)
        else:
            # SDK exception bodies can contain temporary signed URLs or request headers.
            error = {"code": "resource_operation_failed", "message": "资源操作失败；详细诊断未输出，以避免暴露下载凭据。"}
        _write_json({"ok": False, "error": error})
        return 1
    _write_json({"ok": False, "error": {"code": "unknown_command", "message": "不支持的资源操作。"}})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

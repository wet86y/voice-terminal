"""DSH distribution adapter（宿主分发适配）：与原生入口读取同一份声明。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

from ..distribution import safe_path, verify, files, digest

SOURCE_KIND = "npm_package"  # SOURCE_KIND（npm 包来源）。
COMPATIBILITY_KEYS = ("source_kind", "adapter", "package_name", "version", "bridge_protocol",
                      "entry", "modules", "cli_entry", "schedule", "pnpm_entry", "package_manager", "required_files")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("DSH（代理宿主）配置必须是对象。")
    return value


def target_spec(program: Path) -> dict:
    lock = read_json(program / "config/dsh/runtime.lock.json")
    spec = dict(lock["distribution"])
    spec.update(version=lock["version"], package_manager=lock["package_manager"])
    if spec.get("source_kind") != SOURCE_KIND or spec.get("adapter") != "npm_cli_v1":
        raise ValueError("DSH（代理宿主）分发适配器不受支持。")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", spec["version"]):
        raise ValueError("DSH（代理宿主）必须指定精确版本。")
    for key in ("entry", "modules", "cli_entry", "schedule", "pnpm_entry", "plugin_record"):
        safe_path(program, spec[key])
    return spec


def is_current(metadata: dict, spec: dict) -> bool:
    return all(metadata.get(key) == spec.get(key) for key in COMPATIBILITY_KEYS)


def check_plugin(program: Path, spec: dict) -> None:
    path = safe_path(program, spec["plugin_record"])
    if not path.is_file() and (program / "apps/desktop/Cargo.toml").is_file():
        path = program / ".local/build/dsh/bundles/voice-terminal/dsh-compatibility.json"
    record = read_json(path)
    if (record.get("dsh_version") != spec["version"] or record.get("bridge_protocol") != spec["bridge_protocol"]
            or record.get("adapter") != spec["adapter"]):
        raise ValueError("项目插件尚未适配目标 DSH（代理宿主）版本，请更新程序。")


def node_version_matches(version: str, ranges: str) -> bool:
    actual = tuple(int(v) for v in version.removeprefix("v").split("."))
    for term in ranges.split("||"):
        match = re.fullmatch(r"\s*(\^|>=|=)?(\d+)\.(\d+)\.(\d+)\s*", term)
        if not match:
            raise ValueError("Node.js（运行环境）版本范围不受支持。")
        operator, *parts = match.groups()
        wanted = tuple(int(v) for v in parts)
        if ((operator == ">=" and actual >= wanted) or (operator == "^" and actual >= wanted and actual[0] == wanted[0])
                or (operator in (None, "=") and actual == wanted)):
            return True
    return False


def discover_node(spec: dict, *, require_npm: bool = False) -> tuple[Path, Path | None]:
    candidates = []
    selected = shutil.which("node")
    if selected:
        candidates.append(Path(selected))
    for directory in os.get_exec_path():
        candidates.append(Path(directory) / ("node.exe" if os.name == "nt" else "node"))
    for key in ("ProgramFiles", "LOCALAPPDATA"):
        if os.environ.get(key):
            candidates.extend(Path(os.environ[key]) / rel for rel in ("nodejs/node.exe", "Programs/nodejs/node.exe"))
    env = {key: value for key, value in os.environ.items() if key.upper() not in {"NODE_OPTIONS", "ELECTRON_RUN_AS_NODE"}}
    for node in dict.fromkeys(candidates):
        if not node.is_file():
            continue
        try:
            result = subprocess.run([str(node), "-p", "JSON.stringify({version:process.versions.node,platform:process.platform,arch:process.arch})"],
                                    capture_output=True, text=True, timeout=10, env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, check=True)
            identity = json.loads(result.stdout)
            if (identity["platform"] != "win32" or identity["arch"] != "x64"
                    or not node_version_matches(identity["version"], spec["node_version_range"])):
                continue
            npm = node.parent / "node_modules/npm/bin/npm-cli.js"
            if require_npm:
                if not npm.is_file():
                    continue
                subprocess.run([str(node), str(npm), "--version"], capture_output=True, timeout=10, env=env, check=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return node.resolve(), npm if npm.is_file() else None
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            continue
    raise ValueError(f"需要 Windows x64（64 位系统）的 Node.js（运行环境）{spec['node_version_range']}"
                     + ("及 npm（包管理器）" if require_npm else "") + "，请安装后重新检测或重启程序。")


def inspect_runtime(program: Path, root: Path, *, full: bool = False) -> dict:
    from ..startup_logging import startup_event
    started = time.perf_counter()
    startup_event("代理组件文件盘点")
    if root.is_symlink() or root.is_junction():
        raise ValueError("DSH（代理宿主）运行目录不能是链接。")
    metadata = read_json(root / "runtime.json")
    if metadata.get("schema") != 3 or metadata.get("source_kind") != SOURCE_KIND or metadata.get("adapter") != "npm_cli_v1":
        raise ValueError("npm（包管理器）宿主运行记录无效。")
    package = read_json(safe_path(root, f"{metadata['modules']}/{metadata['package_name']}/package.json"))
    if package.get("name") != metadata.get("package_name") or package.get("version") != metadata.get("version"):
        raise ValueError("DSH（代理宿主）实际包名或版本与安装记录不一致。")
    tool_path = f"{metadata['modules']}/pnpm/package.json"
    tool = read_json(safe_path(root, tool_path))
    if tool.get("name") != "pnpm" or f"pnpm@{tool.get('version')}" != metadata.get("package_manager"):
        raise ValueError("私有 pnpm（插件包管理器）实际版本与记录不一致。")
    manifest = read_json(root / "manifest.json")
    entries = {row["path"] for row in manifest.get("files", [])}
    required = {"runtime.json", "package.json", tool_path, f"{metadata['modules']}/{metadata['package_name']}/package.json",
                *(metadata[key] for key in ("entry", "cli_entry", "schedule", "pnpm_entry")), *metadata["required_files"]}
    if manifest.get("kind") != "dsh" or not required.issubset(entries):
        raise ValueError("DSH（代理宿主）清单缺少必要入口。")
    for relative in required:
        path = safe_path(root, relative)
        if not path.is_file() or path.is_symlink() or path.is_junction():
            raise ValueError("DSH（代理宿主）必要入口缺失。")
    if full:
        verify(root, manifest)
    # 常规启动只检查上述必要入口；完整扫描由 full（完整维护模式）负责。
    spec = target_spec(program)
    if is_current(metadata, spec):
        check_plugin(program, spec)
    startup_event("代理组件文件盘点", event="completed", files_checked=len(entries) if full else len(required),
                  elapsed_ms=(time.perf_counter() - started) * 1000)
    return metadata


def check_binding(root: Path) -> None:
    """Check binding（安装绑定）：导入候选校验不调用，已安装组件必须登记清单摘要。"""
    workspace = root.parents[1]
    release = read_json(workspace / "app/release.json")
    manifest = read_json(root / "manifest.json")
    if not any(isinstance(row, dict) and row.get("kind") == "dsh" and row.get("path") == "runtimes/dsh"
               and row.get("build") == manifest.get("build") and row.get("manifest_sha256") == digest(root / "manifest.json")
               for row in release.get("components", [])):
        raise ValueError("DSH（代理宿主）安装清单绑定不一致，请修复组件。")


def validate_runtime(program: Path, root: Path, *, full: bool = False) -> tuple[dict, Path]:
    from ..startup_logging import startup_stage
    metadata = inspect_runtime(program, root, full=full)
    with startup_stage("核验代理组件安装绑定"):
        check_binding(root)
    spec = target_spec(program)
    if not is_current(metadata, spec):
        raise ValueError("DSH（代理宿主）版本或适配要求已更新，请在组件管理页更新。")
    with startup_stage("检测 Node.js（代理运行环境）"):
        node, _ = discover_node(spec)
    return metadata, node

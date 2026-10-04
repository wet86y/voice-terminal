"""DSH runtime（代理宿主运行产物）：复用上游生产依赖闭包，生成无联接目录。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import digest, files, manifest, safe_path, verify, collect_vc_runtime
from voice_terminal.paths import atomic_json


def run(*arguments, cwd=None, env=None) -> str:
    process = subprocess.run([str(v) for v in arguments], cwd=cwd, env=env, text=True, encoding="utf-8", capture_output=True)
    if process.returncode:
        raise RuntimeError(f"构建命令失败：{process.stdout[-1600:]}\n{process.stderr[-1600:]}")
    return process.stdout.strip()


def materialize(source: Path, target: Path, *, omit_nested_modules=False, ancestors=()) -> None:
    """将构建机链接展开为普通文件；递归循环明确失败，绝不将链接交给发行端。"""
    resolved = source.resolve(strict=True)
    if resolved in ancestors:
        raise ValueError(f"依赖闭包含递归链接：{source.name}")
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for path in source.iterdir():
            if path.name == ".bin" or (omit_nested_modules and path.name == "node_modules"):
                continue
            materialize(path, target / path.name, omit_nested_modules=omit_nested_modules, ancestors=(*ancestors, resolved))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def stage_workspace(source: Path, destination: Path) -> None:
    """在独立构建副本改变依赖布局，避免生产部署清掉源码调试的目录联接。"""
    for parent, directories, names in os.walk(source, followlinks=False):
        directories[:] = [name for name in directories if name not in {"node_modules", ".git", ".cache", ".desktop-build", ".vite", ".pnpm-store"}]
        for name in directories:
            origin = Path(parent) / name
            if origin.is_symlink() or origin.is_junction():
                raise ValueError("构建输入含未声明的外部目录链接")
        for name in names:
            origin = Path(parent) / name
            if origin.is_symlink() or origin.is_junction():
                raise ValueError("构建输入含未声明的外部链接")
            target = destination / origin.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)


def collect_office_engine(output: Path, cache: Path) -> None:
    """Web（网页）配置的文档预览需要原生转换引擎，不能只带接口包。"""
    package = "@deepseek-ai/libreoffice-kit-win32-x64"
    entry = json.loads((output / "node_modules/@deepseek-ai/libreoffice-kit/package.json").read_text(encoding="utf-8"))
    version = entry["optionalDependencies"][package]
    target = output / "node_modules" / package
    if (target / "prebuilds.json").is_file():
        return
    cache.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"https://registry.npmjs.org/{package}/{version}", timeout=60) as response:
        metadata = json.load(response)
    archive = cache / f"libreoffice-kit-win32-x64-{version}.tgz"
    if not archive.is_file():
        print("下载文档预览的 Windows（操作系统）原生转换组件……", flush=True)
        with urllib.request.urlopen(metadata["dist"]["tarball"], timeout=120) as incoming, archive.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    algorithm, expected = metadata["dist"]["integrity"].split("-", 1)
    hasher = hashlib.new(algorithm)
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): hasher.update(block)
    if base64.b64encode(hasher.digest()).decode() != expected:
        raise ValueError("原生文档转换组件的发布摘要不符；保留下载诊断后重新获取。")
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            relative = member.name.removeprefix("package/")
            if member.isdir(): continue
            if not member.isfile() or relative == member.name:
                raise ValueError("原生组件归档含外部路径、链接或特殊文件")
            destination = safe_path(target, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    if not (target / "prebuilds.json").is_file():
        raise FileNotFoundError("原生文档转换组件缺少预编译清单")


def main():
    parser = argparse.ArgumentParser(description="构建 DSH（代理执行宿主）生产运行目录")
    parser.add_argument("--source", type=Path, default=ROOT / ".runtime/dsh/source")
    parser.add_argument("--node", type=Path, required=True, help="明确的 Node.js（脚本运行环境）可执行文件")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pnpm", type=Path, help="明确的 pnpm（依赖构建工具）入口文件；仅构建机需要")
    parser.add_argument("--reuse-closure", type=Path, help="复用已校验且源码版本一致的物理依赖闭包；重新生成运行清单")
    parser.add_argument('--vc-redist', type=Path, required=True, help='已授权 MSVC（微软构建工具）的 Redist（可再分发）版本下 x64（64 位）目录')
    args = parser.parse_args()
    source, output, node = args.source.resolve(), args.output.resolve(), args.node.resolve()
    if output.exists():
        raise FileExistsError("输出目录已存在；请选择新的产物标识。")
    lock = json.loads((ROOT / "config/dsh/runtime.lock.json").read_text(encoding="utf-8-sig"))
    commit = run("git", "-C", source, "rev-parse", "HEAD")
    version = json.loads((source / "package.json").read_text(encoding="utf-8"))["version"]
    if commit != lock["commit"] or version != lock["version"]:
        raise ValueError("宿主源码提交或版本与锁定记录不一致。")
    pnpm = args.pnpm or Path(shutil.which("pnpm.cmd") or "")
    if not pnpm.is_file():
        raise FileNotFoundError("构建机缺少 pnpm（依赖构建工具）。")
    # 通过 Node（脚本运行环境）运行真正的包管理器入口，避免 cmd（命令解释器）再解释路径。
    if pnpm.suffix.lower() == ".cmd":
        entry = pnpm.parent / "node_modules/pnpm/bin/pnpm.cjs"
        if not entry.is_file():
            raise FileNotFoundError("请使用 --pnpm 指定 pnpm.cjs（包管理器脚本入口）。")
        pnpm = entry
    env = {**os.environ, "PATH": str(node.parent) + os.pathsep + os.environ.get("PATH", "")}
    if run(node, pnpm, "--version", cwd=source, env=env) != lock["package_manager"].split("@")[1]:
        raise ValueError("构建机包管理器版本与宿主锁定记录不一致。")
    print(run(node, pnpm, "run", "verify-runtime-closure", cwd=source, env=env), flush=True)
    deploy = output.with_name(output.name + ".deploy")
    if deploy.exists():
        raise FileExistsError("构建暂存目录已存在；请保留诊断并选择新输出目录。")
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.reuse_closure:
        previous = args.reuse_closure.resolve()
        verify(previous, json.loads((previous / "manifest.json").read_text(encoding="utf-8-sig")))
        previous_metadata = json.loads((previous / "runtime.json").read_text(encoding="utf-8-sig"))
        if previous_metadata["version"] != version or previous_metadata["commit"] != commit:
            raise ValueError("复用的依赖闭包与当前锁定源码版本不一致")
        print("复用已校验的物理依赖闭包……", flush=True)
        shutil.copytree(previous, output)
    else:
        workspace = output.with_name(output.name + ".workspace")
        if workspace.exists(): raise FileExistsError("独立构建工作目录已存在；请选择新的产物标识。")
        print("准备独立生产构建输入……", flush=True)
        stage_workspace(source, workspace)
        options = ("--config.allow-unused-patches=true", "--config.node-linker=hoisted", "--config.auto-install-peers=false", "--config.link-workspace-packages=true")
        run(node, pnpm, "install", "--prod", "--frozen-lockfile", "--ignore-scripts", *options, cwd=workspace, env=env)
        print("收集生产依赖闭包……", flush=True)
        run(node, pnpm, "--filter", "dsh-python-runtime-closure", "deploy", "--legacy", "--prod", *options, deploy, cwd=workspace, env=env)
        materialize(deploy / "runtime-bootstrap.mjs", output / "runtime-bootstrap.mjs")
        materialize(deploy / "package.json", output / "package.json")
        modules = output / "node_modules"
        modules.mkdir()
        for package in (deploy / "node_modules").iterdir():
            if package.name != ".bin": materialize(package, modules / package.name, omit_nested_modules=True)
        closure = json.loads((workspace / "python/sdk-runtime/package.json").read_text(encoding="utf-8"))
        for name in closure["dependencies"]:
            if not (modules / name).is_dir():
                materialize(workspace / "python/sdk-runtime/node_modules" / name, modules / name, omit_nested_modules=True)
    collect_office_engine(output, ROOT / ".runtime/package-build/downloads")
    vc_runtime = collect_vc_runtime(output / 'node_modules/@deepseek-ai/libreoffice-kit-win32-x64/bin', args.vc_redist.resolve(strict=True))
    shutil.copyfile(node, output / "node.exe")
    protocol = yaml.safe_load((ROOT / "config/default.yaml").read_text(encoding="utf-8-sig"))["dsh"]["bridge_protocol_version"]
    metadata = {"schema": 1, "version": version, "commit": commit, "bridge_protocol": str(protocol),
                "node": "node.exe", "entry": "runtime-bootstrap.mjs", "modules": "node_modules",
                "schedule": "node_modules/@deepseek-ai/dsh-schedule/lib/index.js", "node_version": run(node, "--version"), "office_vc_runtime": vc_runtime}
    for key in ("node", "entry", "schedule"):
        if not (output / metadata[key]).is_file():
            raise FileNotFoundError(f"运行产物缺少必要文件：{key}")
    print(run(output / "node.exe", output / "runtime-bootstrap.mjs", "--version", cwd=output.parent), flush=True)
    atomic_json(output / "runtime.json", metadata)
    for path in files(output):
        if path.suffix in {".json", ".yml", ".yaml"} and path.stat().st_size < 1024 * 1024:
            text = path.read_text(encoding="utf-8", errors="replace")
            normalized = text.replace("\\\\", "/").replace("\\", "/").lower()
            if any(str(path).replace("\\", "/").lower() in normalized for path in (source, output.with_name(output.name + ".workspace"))):
                raise ValueError(f"产物仍包含开发机路径：{path.relative_to(output)}")
    record = manifest(output, kind="dsh", build="pending")
    record["build"] = commit[:8] + "-" + hashlib.sha256(json.dumps(record["files"], sort_keys=True).encode()).hexdigest()[:12]
    atomic_json(output / "manifest.json", record)
    verify(output, record)
    print(f"DSH（代理执行宿主）生产目录收集、版本和逐文件校验通过：{output}")


if __name__ == "__main__":
    main()

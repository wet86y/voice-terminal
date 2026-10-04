"""Assemble desktop（组装桌面分发）：应用、环境与模型按清单收集，用户数据不进入发布包。"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import digest, files, manifest, require_stopped, safe_path, verify, verify_install
from voice_terminal.dsh.profile import build_profile
from voice_terminal.paths import atomic_json

CORE_MODELS = ("kws/sherpa-onnx-kws-zipformer-zh-en-3M", "funasr/paraformer-zh-streaming", "funasr/fsmn-vad", "analysis/smart-turn", "analysis/sensevoice", "speaker/wespeaker-cnceleb-resnet34")


def copy_tree(source: Path, target: Path, *, excludes=()) -> None:
    for path in files(source):
        relative = path.relative_to(source)
        if any(part in {"__pycache__", ".cache", ".git", "node_modules", *excludes} for part in relative.parts) or path.suffix == ".pyc":
            continue
        copy = target / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)


def copy_component(source: Path, target: Path, *, kind: str, build: str) -> dict:
    # 组件闭包需要所有依赖目录；此处不能使用源码收集器的 node_modules（依赖目录）过滤。
    for path in files(source):
        relative = path.relative_to(source)
        copy = target / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)
    record = manifest(target, kind=kind, build=build)
    atomic_json(target / "manifest.json", record)
    verify(target, record)
    return record


def archive(root: Path, destination: Path, *, exclude_roots=(), include_roots=(), overrides=None) -> dict:
    if include_roots:
        candidates = []
        for relative in include_roots:
            safe_path(root, relative)
            entry = root
            for part in Path(relative).parts:
                entry = entry / part
                attributes = entry.lstat()
                if stat.S_ISLNK(attributes.st_mode) or getattr(attributes,'st_file_attributes',0) & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
                    raise ValueError(f"发布来源不能是链接或重解析点：{relative}")
            candidates.extend(files(entry) if entry.is_dir() else (entry,))
    else:
        candidates = files(root, exclude_roots=exclude_roots)
    seen = set()
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as output:
        for path in candidates:
            relative = path.relative_to(root)
            name = relative.as_posix()
            if name.casefold() in seen:
                raise ValueError("发布归档有重复文件。")
            seen.add(name.casefold())
            if name in (overrides or {}):
                output.writestr(name, overrides[name])
            else:
                output.write(path, name)
    return {"file": destination.name, "size": destination.stat().st_size, "sha256": digest(destination)}


def collect_program(program: Path, desktop: Path) -> None:
    """只收集程序与默认模板；开发依赖和个人配置不进入程序目录。"""
    program.mkdir(parents=True)
    shutil.copy2(desktop / "VoiceTerminalDesktop.exe", program / "VoiceTerminalDesktop.exe")
    for relative in ("voice_terminal", "apps/server", "apps/web/dist", "config/dsh", "config/shadow_agent", "config/templates"):
        copy_tree(ROOT / relative, program / relative)
    (program / "scripts").mkdir()
    for name in ("backend-entry.py", "managed-node.cjs", "check-desktop-environment.py", "prepare-webview2.ps1"):
        shutil.copy2(ROOT / "scripts" / name, program / "scripts" / name)
    for name in ("package.json", "pyproject.toml"):
        shutil.copy2(ROOT / name, program / name)
    config = (ROOT / "config/default.yaml").read_text(encoding="utf-8-sig").splitlines()
    config[0] = 'model_dir: "models" # 由启动上下文传入实际只读模型目录。'
    (program / "config/default.yaml").write_text("\n".join(config)+"\n", encoding="utf-8")
    shutil.copy2(ROOT / "config/runtime-resources.json", program / "config/runtime-resources.json")
    build_profile(ROOT, ROOT / ".runtime/dsh/bundles", program / "config/dsh/profile")


def root_files(output: Path, desktop: Path) -> list[dict]:
    shutil.copy2(desktop / "VoiceTerminal.exe", output / "VoiceTerminal.exe")
    shutil.copy2(ROOT / "scripts/verify-package.ps1", output / "Verify-Package.ps1")
    if (ROOT / "docs/desktop-application.md").is_file():
        shutil.copy2(ROOT / "docs/desktop-application.md", output / "使用与环境说明.md")
    return [{"path":name,"size":(output / name).stat().st_size,"sha256":digest(output / name)} for name in ("VoiceTerminal.exe", "Verify-Package.ps1")]


def atomic_bytes(path: Path, value: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.build-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def refresh_program(output: Path, desktop: Path, build: str) -> None:
    """构建机停机刷新候选目录，复用已校验组件；不承接终端在线更新事务。"""
    require_stopped(ROOT, output)
    verify_install(output)
    portable = json.loads((output / "portable.json").read_text(encoding="utf-8-sig"))
    data = Path(portable["data_dir"])
    data = (data if data.is_absolute() else output / data).resolve()
    active = data / "state/active.json"
    selected = json.loads(active.read_text(encoding="utf-8-sig")) if active.is_file() else portable
    previous = json.loads((safe_path(output, selected["app"]) / "release.json").read_text(encoding="utf-8-sig"))
    program = output / "apps" / build
    if program.exists():
        raise FileExistsError("新的程序构建目录已存在，请使用新的构建标识。")
    selection = {key:value for key,value in selected.items() if key != "data_dir"}
    selection["app"] = f"apps/{build}"
    # 新程序及根入口先在未启用位置校验，提交失败恢复入口和记录；不修改任何业务数据。
    with tempfile.TemporaryDirectory(prefix='voice-refresh-', dir=output.parent) as directory:
        stage = Path(directory)
        staged_program = stage / 'app'
        staged_root = stage / 'root'
        staged_root.mkdir()
        collect_program(staged_program, desktop)
        release = {**previous, **manifest(staged_program, kind="app", build=build), "root_files":root_files(staged_root, desktop)}
        atomic_json(staged_program / "release.json", release)
        verify(staged_program, release)
        staged = list(staged_root.iterdir())
        targets = [*(output / file.name for file in staged), output / 'portable.json']
        if active.is_file():
            targets.append(active)
        backups = {path: path.read_bytes() if path.is_file() else None for path in targets}
        require_stopped(ROOT, output)
        program.resolve().relative_to(output / 'apps')
        staged_program.resolve().relative_to(stage)
        committed_program = False
        try:
            staged_program.rename(program)
            committed_program = True
            for file in staged:
                atomic_bytes(output / file.name, file.read_bytes())
            if active in backups:
                atomic_json(active, selection)
            atomic_json(output / "portable.json", {**portable, **selection})
        except Exception:
            for path, content in backups.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_bytes(path, content)
            if committed_program:
                program.rename(staged_program)
            raise
    print(f"候选程序已刷新：{program}；环境、模型及用户数据保留，旧程序目录保留以供检查。")


def main():
    parser = argparse.ArgumentParser(description="组装完整便携候选或明确标记的环境辅助桌面包")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), help="唯一构建标识，不强制语义版本")
    parser.add_argument("--desktop", type=Path, default=ROOT / "apps/desktop/target/release")
    parser.add_argument("--python-runtime", type=Path)
    parser.add_argument("--external-python", type=Path, help="未完成便携验证时，明确指定已有业务环境")
    parser.add_argument("--models", type=Path)
    parser.add_argument("--external-models", action="store_true", help="环境辅助包明确引用外部模型")
    parser.add_argument("--webview2", type=Path, help="包含 msedgewebview2.exe 的实际固定版本目录")
    parser.add_argument("--dsh-runtime", type=Path)
    parser.add_argument("--refresh-program", action="store_true", help="构建机停机刷新已有候选的程序，复用环境和模型；终端更新留到后续专题")
    parser.add_argument("--zip", action="store_true", help="生成完整压缩包与组件压缩包，排除用户数据")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and not args.refresh_program:
        raise FileExistsError("输出目录已存在，请选择新的目录。")
    if not args.refresh_program and not args.python_runtime and not args.external_python:
        raise ValueError("必须指定独立解释器组件或明确的外部解释器退路。")
    if args.python_runtime and args.external_python:
        raise ValueError("只能选择一种解释器运行方式。")
    if not args.build or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in args.build):
        raise ValueError("构建标识只能使用字母、数字、横线和下划线。")
    desktop = args.desktop.resolve()
    for name in ("VoiceTerminal.exe", "VoiceTerminalDesktop.exe"):
        if not (desktop / name).is_file():
            raise FileNotFoundError(f"缺少桌面构建产物：{name}")
    if args.refresh_program:
        if args.zip:
            raise ValueError("候选程序刷新后，使用 package-desktop.py（分发归档工具）单独校验并归档。")
        refresh_program(output, desktop, args.build)
        return
    if not args.models or not args.dsh_runtime:
        raise ValueError("首次组装必须提供完整模型和代理宿主运行目录。")
    for source in (args.python_runtime, args.dsh_runtime):
        if source:
            verify(source.resolve(), json.loads((source / "manifest.json").read_text(encoding="utf-8-sig")))
    program = output / "apps" / args.build
    collect_program(program, desktop)
    components = []
    selection = {"schema":1, "app":f"apps/{args.build}", "python":"", "models":"", "webview2":None, "dsh":None, "node":None, "external_python":None, "external_models":None}
    def collect(source, kind, identifier):
        relative = f"runtimes/{kind}/{identifier}"
        record = copy_component(source, output / relative, kind=kind, build=identifier)
        components.append({"kind":kind,"path":relative,"manifest_sha256":digest(output / relative / "manifest.json"),"build":record["build"]})
        return relative
    if args.python_runtime:
        metadata = json.loads((args.python_runtime / "manifest.json").read_text(encoding="utf-8-sig"))
        selection["python"] = collect(args.python_runtime.resolve(), "python", metadata["build"]) + "/python.exe"
    else:
        selection["external_python"] = str(args.external_python.resolve(strict=True))
    dsh_metadata = json.loads((args.dsh_runtime / "manifest.json").read_text(encoding="utf-8-sig"))
    selection["dsh"] = collect(args.dsh_runtime.resolve(), "dsh", dsh_metadata["build"])
    if args.webview2:
        if not (args.webview2 / "msedgewebview2.exe").is_file():
            raise ValueError("固定网页运行时目录不完整。")
        selection["webview2"] = collect(args.webview2.resolve(), "webview2", digest(args.webview2 / "msedgewebview2.exe")[:16])
    if args.external_models:
        selection["external_models"] = str(args.models.resolve(strict=True))
    else:
        model_target = output / "models/core"
        for relative in CORE_MODELS:
            source = args.models / relative
            if not source.is_dir():
                raise FileNotFoundError(f"缺少本轮运行所需模型：{relative}")
            copy_tree(source, model_target / relative, excludes=("keywords.txt", "keywords.raw.txt", "keywords_raw.txt"))
        record = manifest(model_target, kind="models", build="core")
        record["build"] = hashlib.sha256(json.dumps(record["files"], sort_keys=True).encode()).hexdigest()[:16]
        resource_path = f"models/{record['build']}"
        model_target.rename(output / resource_path)
        model_target = output / resource_path
        atomic_json(model_target / "manifest.json", record)
        components.append({"kind":"models","path":resource_path,"manifest_sha256":digest(model_target / "manifest.json"),"build":record["build"]})
        selection["models"] = resource_path
    release = manifest(program, kind="app", build=args.build)
    release.update({"display_version":"0.7-dev", "compatibility":{"desktop_protocol":1,"data_format":1,"minimum_windows":"10-22H2-x64"},"components":components,"root_files":root_files(output, desktop),"portable_candidate":bool(args.python_runtime and args.webview2 and not args.external_models),"external_requirements":{"python":not bool(args.python_runtime),"models":args.external_models,"system_webview2":not bool(args.webview2)}})
    atomic_json(program / "release.json", release)
    atomic_json(output / "portable.json", {"data_dir":"data", **selection})
    verify(program, release)
    if args.zip:
        archives = [archive(output, output.with_suffix(".zip"), exclude_roots=("data",))]
        component_dir = output.with_name(output.name + "-components")
        component_dir.mkdir()
        archives.append(archive(program, component_dir / f"app-{args.build}.zip"))
        for component in components:
            archives.append(archive(output / component["path"], component_dir / f"{component['kind']}-{component['build']}.zip"))
        atomic_json(component_dir / "downloads.json", {"schema":1,"archives":archives,"signed":False})
    print(f"桌面目录组装完成：{output}；便携候选={release['portable_candidate']}（仍需干净系统验收）。")


if __name__ == "__main__":
    main()

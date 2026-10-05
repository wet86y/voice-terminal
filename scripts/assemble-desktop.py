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


def collect_frontend(source: Path, target: Path) -> None:
    """只收集单个产品入口的构建闭包，拒绝样例、陈旧文件及开发资源。"""
    manifest_path = source / "desktop-manifest.json"
    if not manifest_path.is_file():
        raise ValueError("缺少界面产物清单，请执行 npm run frontend:build（静态界面构建）。")
    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(records, dict) or not records:
        raise ValueError("界面产物清单无效。")
    entries = [key for key, row in records.items() if isinstance(row, dict) and row.get("isEntry")]
    if entries != ["index.html"]:
        raise ValueError("正式界面必须只有产品入口，不能包含样例页面。")
    allowed = {"index.html", "desktop-manifest.json"}
    visited = set()

    def visit(key):
        if key in visited:
            return
        visited.add(key)
        row = records.get(key)
        if not isinstance(row, dict) or any(part in key for part in ("ui-demo", "replay-data", "fixtures")):
            raise ValueError("正式界面依赖包含样例或无效资源。")
        values = [row.get("file"), *row.get("css", []), *row.get("assets", [])]
        for value in values:
            if not isinstance(value, str) or not value:
                raise ValueError("界面资源路径无效。")
            path = safe_path(source, value)
            if not path.is_file():
                raise ValueError("界面依赖文件缺失：" + value)
            allowed.add(value)
        for imported in [*row.get("imports", []), *row.get("dynamicImports", [])]:
            visit(imported)

    visit("index.html")
    actual = {path.relative_to(source).as_posix() for path in files(source)}
    if actual != allowed:
        raise ValueError("界面产物存在缺失、样例或陈旧文件，请重新构建。")
    for relative in sorted(allowed):
        path = safe_path(source, relative)
        copy = target / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)


def collect_program(program: Path, desktop: Path) -> None:
    """只收集程序与默认模板；开发依赖和个人配置不进入程序目录。"""
    program.mkdir(parents=True)
    shutil.copy2(desktop / "VoiceTerminalDesktop.exe", program / "VoiceTerminalDesktop.exe")
    collect_frontend(ROOT / "apps/frontend/dist", program / "apps/frontend/dist")
    for relative in ("voice_terminal", "apps/server", "config/dsh", "config/shadow_agent", "config/templates"):
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


def _read_dsh_lock(program: Path) -> dict:
    """读取候选应用中的 DSH 锁版（宿主版本与源码提交号）。"""
    lock_path = program / "config/dsh/runtime.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
    if not isinstance(lock, dict):
        raise ValueError("新程序中的 DSH 锁版无效。")
    version, commit = lock.get("version"), lock.get("commit")
    if (lock.get("schema_version") != 1 or not isinstance(version, str) or not version.strip()
            or not isinstance(commit, str) or len(commit) != 40
            or any(character not in "0123456789abcdefABCDEF" for character in commit)):
        raise ValueError("新程序中的 DSH 锁版无效。")
    return lock


def _validated_dsh_source(source: Path) -> tuple[Path, dict, dict]:
    """校验 DSH 组件清单（文件摘要）并读取 runtime 版本记录。"""
    if source.is_symlink() or source.is_junction():
        raise ValueError("DSH 运行目录不能是链接或重解析点。")
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("DSH 运行产物必须是目录。")
    record = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
    if (not isinstance(record, dict) or record.get("schema") != 1 or record.get("kind") != "dsh"
            or not isinstance(record.get("files"), list)
            or any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
                   or not isinstance(item.get("size"), int) or not isinstance(item.get("sha256"), str)
                   for item in record["files"])):
        raise ValueError("DSH 运行产物清单无效。")
    build = record.get("build")
    if (not isinstance(build, str) or not build
            or any(not (character.isascii() and (character.isalnum() or character in "-_")) for character in build)):
        raise ValueError("DSH 运行产物构建标识无效。")
    verify(source, record)
    metadata = json.loads((source / "runtime.json").read_text(encoding="utf-8-sig"))
    if not isinstance(metadata, dict) or metadata.get("schema") != 1:
        raise ValueError("DSH 运行产物版本记录无效。")
    return source, record, metadata


def _dsh_component_path(output: Path, build: str) -> tuple[str, Path]:
    """解析输出根内的 DSH 组件路径，并拒绝路径上的链接目录。"""
    relative = f"runtimes/dsh/{build}"
    root = output.resolve()
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("DSH 组件路径不能经过链接或重解析点。")
    return relative, safe_path(root, relative)


def _reuse_dsh_component(target: Path, expected: dict, version: str, commit: str) -> dict:
    """只复用同 build 且文件摘要和 runtime 版本完全一致的组件。"""
    if not target.is_dir():
        raise ValueError("同标识 DSH 组件已存在，但不是有效目录。")
    record = json.loads((target / "manifest.json").read_text(encoding="utf-8-sig"))
    if record != expected:
        raise ValueError("同标识 DSH 组件与新运行产物不一致，拒绝覆盖。")
    verify(target, record)
    metadata = json.loads((target / "runtime.json").read_text(encoding="utf-8-sig"))
    if (metadata.get("schema") != 1 or metadata.get("version") != version
            or str(metadata.get("commit", "")).lower() != commit.lower()):
        raise ValueError("同标识 DSH 组件版本与新应用锁版不一致。")
    return record


def _install_dsh_component(output: Path, stage: Path, source: Path, source_record: dict,
                           metadata: dict, lock: dict) -> tuple[str, dict, bool]:
    """把已验证 DSH 组件放入未启用目录，返回路径、清单及本次新建标记。"""
    _validate_dsh_lock_match(source_record, metadata, lock)
    relative, target = _dsh_component_path(output, source_record["build"])
    if target.exists():
        return relative, _reuse_dsh_component(target, source_record, lock["version"], lock["commit"]), False

    staged = stage / "dsh-component"
    copied_record = copy_component(source, staged, kind="dsh", build=source_record["build"])
    if copied_record != source_record:
        raise ValueError("DSH 复制后的组件清单与输入产物不一致。")
    verify(staged, copied_record)

    target.parent.mkdir(parents=True, exist_ok=True)
    # mkdir（排他创建）先占用唯一 build 路径；回滚只会移除本次确实创建的目录。
    target.mkdir()
    try:
        for source_file in files(staged):
            relative_file = source_file.relative_to(staged)
            destination = target / relative_file
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_file.replace(destination)
        verify(target, copied_record)
    except Exception:
        if target.exists() and not target.is_symlink() and not target.is_junction():
            for _ in files(target):
                pass
            shutil.rmtree(target)
        raise
    return relative, copied_record, True


def _validate_dsh_lock_match(record: dict, metadata: dict, lock: dict) -> None:
    """检查 runtime 版本、源码提交号和 manifest build（清单构建标识）均属于锁版。"""
    commit = lock["commit"].lower()
    if (metadata.get("version") != lock["version"]
            or str(metadata.get("commit", "")).lower() != commit):
        raise ValueError("DSH 运行产物版本与新应用锁版不一致。")
    if not record["build"].lower().startswith(commit[:8] + "-"):
        raise ValueError("DSH 运行产物构建标识与新应用锁版不一致。")


def _remove_new_dsh_component(output: Path, relative: str) -> None:
    """仅移除本次新增且仍位于 output/runtimes/dsh 下的组件目录。"""
    root = output.resolve()
    parts = Path(relative).parts
    if len(parts) != 3 or parts[:2] != ("runtimes", "dsh"):
        raise ValueError("回滚 DSH 组件路径不在受管目录内。")
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("回滚 DSH 组件路径不能经过链接或重解析点。")
    target = safe_path(root, relative)
    if target.is_symlink() or target.is_junction() or target.parent != current:
        raise ValueError("回滚 DSH 组件路径不在受管目录内。")
    for _ in files(target):
        pass
    shutil.rmtree(target)


def refresh_program(output: Path, desktop: Path, build: str, dsh_runtime: Path | None = None) -> None:
    """构建机停机刷新已有正式目录，复用已校验组件并保留完整用户数据。"""
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
        components = list(previous.get("components", []))
        new_dsh_relative = None
        new_dsh_created = False
        dsh_plan = None
        dsh_component = None
        if dsh_runtime is not None:
            lock = _read_dsh_lock(staged_program)
            source, source_record, metadata = _validated_dsh_source(dsh_runtime)
            _validate_dsh_lock_match(source_record, metadata, lock)
            new_dsh_relative, _ = _dsh_component_path(output, source_record["build"])
            dsh_plan = (source, source_record, metadata, lock)
            dsh_component = {"kind":"dsh", "path":new_dsh_relative,
                             "manifest_sha256":digest(source / "manifest.json"),
                             "build":source_record["build"]}
            replaced = False
            updated = []
            for component in components:
                if component.get("kind") == "dsh":
                    if not replaced:
                        updated.append(dsh_component)
                        replaced = True
                else:
                    updated.append(component)
            if not replaced:
                updated.append(dsh_component)
            components = updated
            selection["dsh"] = new_dsh_relative
        release = {**previous, **manifest(staged_program, kind="app", build=build),
                   "components":components, "root_files":root_files(staged_root, desktop)}
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
            if dsh_plan is not None:
                source, source_record, metadata, lock = dsh_plan
                installed_relative, dsh_record, new_dsh_created = _install_dsh_component(
                    output, stage, source, source_record, metadata, lock)
                if installed_relative != new_dsh_relative or dsh_record["build"] != source_record["build"]:
                    raise ValueError("DSH 组件路径或构建标识在提交前发生变化。")
                dsh_component["manifest_sha256"] = digest(safe_path(output, installed_relative) / "manifest.json")
                atomic_json(staged_program / "release.json", release)
                verify(staged_program, release)
            staged_program.rename(program)
            committed_program = True
            for file in staged:
                atomic_bytes(output / file.name, file.read_bytes())
            if active in backups:
                atomic_json(active, selection)
            atomic_json(output / "portable.json", {**portable, **selection})
            verify_install(output)
        except Exception:
            if committed_program:
                for path, content in backups.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_bytes(path, content)
                program.rename(staged_program)
            if new_dsh_created and new_dsh_relative is not None:
                _remove_new_dsh_component(output, new_dsh_relative)
            raise
    print(f"正式程序已更新：{program}；环境、模型及完整用户数据保留，旧程序目录保留以供检查。")


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
    parser.add_argument("--refresh-program", action="store_true", help="构建机停机更新已有正式程序，保留环境、模型及完整用户数据；终端更新留到后续专题")
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
        refresh_program(output, desktop, args.build, args.dsh_runtime)
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

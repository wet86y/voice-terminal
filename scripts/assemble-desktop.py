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

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal import __version__  # __version__（源码版本）：避免发布清单显示旧版本。
from voice_terminal.distribution import digest, files, manifest, require_stopped, safe_path, verify, verify_install
from voice_terminal.dsh.profile import build_profile
from voice_terminal.dsh.official import OFFICIAL_SOURCE_KIND, load_official_artifact, validate_official_runtime
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


def collect_models(source: Path, target: Path) -> dict:
    """collect_models（收集核心模型）：包含结束判断的显卡模型与中央处理器回退模型。"""
    for relative in CORE_MODELS:
        folder = source / relative
        if not folder.is_dir():
            raise FileNotFoundError(f"缺少本轮运行所需模型：{relative}")
        copy_tree(folder, target / relative, excludes=("keywords.txt", "keywords.raw.txt", "keywords_raw.txt"))
    record = manifest(target, kind="models", build="core")
    record["build"] = hashlib.sha256(json.dumps(record["files"], sort_keys=True).encode()).hexdigest()[:16]
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
    if set(entries) != {"index.html", "management.html"}:
        raise ValueError("正式界面必须只有产品入口，不能包含样例页面。")
    allowed = {"index.html", "management.html", "startup.html", "startup.css", "app-icon.svg", "desktop-manifest.json"}
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
    visit("management.html")
    actual = {path.relative_to(source).as_posix() for path in files(source)}
    if actual != allowed:
        raise ValueError("界面产物存在缺失、样例或陈旧文件，请重新构建。")
    for relative in sorted(allowed):
        path = safe_path(source, relative)
        copy = target / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, copy)


def collect_desktop_crt(program: Path) -> None:
    """从锁定并验过的 DSH（代理宿主）基线收集桌面 EXE（可执行文件）所需的 MSVC CRT（微软 C++ 运行库）。"""
    runtime = ROOT / ".local/runtimes/dsh"
    metadata_path = runtime / "runtime.json"
    manifest_path = runtime / "manifest.json"
    if not metadata_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("缺少已锁定的 DSH（代理宿主）运行基线，无法收集桌面 C++ 运行库。")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    runtime_record = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    verify(runtime, runtime_record)
    lock = json.loads((ROOT / "config/dsh/runtime.lock.json").read_text(encoding="utf-8-sig"))
    if (metadata.get("schema") != 1
            or metadata.get("version") != lock.get("version")
            or metadata.get("commit") != lock.get("commit")):
        raise ValueError("DSH（代理宿主）运行基线与项目锁版不一致，拒绝收集桌面 C++ 运行库。")

    crt = metadata.get("office_vc_runtime")
    rows = crt.get("files") if isinstance(crt, dict) else None
    if (not isinstance(crt, dict) or crt.get("source") != "Microsoft.VC145.CRT"
            or not isinstance(crt.get("version"), str) or not crt["version"]
            or not isinstance(rows, list) or not rows):
        raise ValueError("锁定的 DSH（代理宿主）基线缺少 Microsoft.VC145.CRT（C++ 运行库）文件清单。")

    bin_relative = "node_modules/@deepseek-ai/libreoffice-kit-win32-x64/bin"
    bin_root = safe_path(runtime, bin_relative)
    runtime_files = {row.get("path"): row for row in runtime_record.get("files", []) if isinstance(row, dict)}
    names: set[str] = set()
    required = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
    for row in rows:
        name, size, sha256 = row.get("file"), row.get("size"), row.get("sha256")
        if (not isinstance(name, str) or Path(name).name != name or Path(name).suffix.lower() != ".dll"
                or name.casefold() in names or not isinstance(size, int) or size <= 0
                or not isinstance(sha256, str) or len(sha256) != 64
                or any(char not in "0123456789abcdef" for char in sha256.lower())):
            raise ValueError("Microsoft.VC145.CRT（C++ 运行库）文件清单包含无效记录。")
        names.add(name.casefold())
        relative = f"{bin_relative}/{name}"
        source = safe_path(runtime, relative)
        manifest_row = runtime_files.get(relative)
        if (manifest_row is None or manifest_row.get("size") != size or manifest_row.get("sha256") != sha256
                or not source.is_file() or source.stat().st_size != size or digest(source) != sha256):
            raise ValueError(f"Microsoft.VC145.CRT（C++ 运行库）文件未通过锁定清单校验：{name}")
        shutil.copy2(source, program / name)

    if not required.issubset(names):
        raise ValueError("Microsoft.VC145.CRT（C++ 运行库）缺少桌面程序所需的核心 DLL（动态链接库）。")
    if (bin_root.is_dir()
            and {path.name.casefold() for path in bin_root.glob("*.dll")} != names):
        raise ValueError("Microsoft.VC145.CRT（C++ 运行库）实际 DLL（动态链接库）集合与锁定清单不一致。")
    notice = bin_root / "VC-Runtime-Notice.txt"
    if notice.is_file():
        expected_notice = runtime_files.get(f"{bin_relative}/VC-Runtime-Notice.txt")
        if (expected_notice is None or notice.stat().st_size != expected_notice.get("size")
                or digest(notice) != expected_notice.get("sha256")):
            raise ValueError("C++ 运行库再分发说明未通过 DSH（代理宿主）锁定清单校验。")
        shutil.copy2(notice, program / notice.name)


def collect_program(program: Path, desktop: Path, *, include_desktop: bool = True) -> None:
    """只收集程序与默认模板；开发依赖和个人配置不进入程序目录。"""
    program.mkdir(parents=True)
    if include_desktop:
        shutil.copy2(desktop / "VoiceTerminalDesktop.exe", program / "VoiceTerminalDesktop.exe")
        collect_desktop_crt(program)
    else:
        crt_target = program / "tools/python-crt"
        crt_target.mkdir(parents=True)
        collect_desktop_crt(crt_target)
    collect_frontend(ROOT / ".local/build/frontend", program / "apps/frontend/dist")
    # 原生入口已内嵌资源；网页握手入口复用同一份加载画面。
    startup_assets = program / "apps/desktop/boot"
    startup_assets.mkdir(parents=True)
    for name in ("startup.css", "app-icon.svg"):
        shutil.copy2(ROOT / "apps/desktop/boot" / name, startup_assets / name)
    for relative in ("voice_terminal", "apps/server", "config/dsh", "config/shadow_agent", "config/templates", "config/resources", "config/runtime", "assets/smart-turn"):
        copy_tree(ROOT / relative, program / relative)
    (program / "scripts").mkdir()
    for name in ("backend-entry.py", "managed-node.cjs", "check-desktop-environment.py", "prepare-webview2.ps1", "local-context.ps1",
                 "manage-resources.py", "package-resources.py", "install-official-dsh.py", "relocate-workspace.py", "voice-environment-worker.py"):
        shutil.copy2(ROOT / "scripts" / name, program / "scripts" / name)
    seven_source = ROOT / ".local/build/tools/7zip"
    if seven_source.is_dir():
        seven_target = program / "tools/7zip"
        seven_target.mkdir(parents=True, exist_ok=True)
        for name in ("7z.exe", "7z.dll", "License.txt", "readme.txt", "build.json"):
            source = seven_source / name
            if not source.is_file():
                raise FileNotFoundError(f"7-Zip（压缩工具）构建目录缺少 {name}（分发文件）。")
            shutil.copy2(source, seven_target / name)
    for name in ("package.json", "pyproject.toml"):
        shutil.copy2(ROOT / name, program / name)
    config = (ROOT / "config/default.yaml").read_text(encoding="utf-8-sig").splitlines()
    config[0] = 'model_dir: "models" # 由启动上下文传入实际只读模型目录。'
    (program / "config/default.yaml").write_text("\n".join(config)+"\n", encoding="utf-8")
    shutil.copy2(ROOT / "config/runtime-resources.json", program / "config/runtime-resources.json")
    build_profile(ROOT, ROOT / ".local/build/dsh/bundles", program / "config/dsh/profile")


def root_files(output: Path, desktop: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    for name in ("VoiceTerminal.exe", "VoiceTerminalUIDemo.exe"):
        shutil.copy2(desktop / name, output / name)
    shutil.copy2(ROOT / "scripts/verify-package.ps1", output / "Verify-Package.ps1")
    # 交付说明必须自包含，不能复制依赖源码文档相对链接的页面。
    (output / '使用与环境说明.md').write_text('''# 使用与环境说明

双击 VoiceTerminal.exe（正式入口）启动。应用关闭窗口后隐藏到托盘；使用菜单或托盘的退出操作，才能正常回收所属服务。Verify-Package.ps1（整包校验）使用随包解释器核对当前程序和组件摘要。

portable.json（便携配置）是唯一组件路径来源：schema=2（第二版），app=app（程序），python=runtimes/python/python.exe（后端解释器），models=models（模型），dsh=runtimes/dsh（代理宿主，含脚本解释器），webview2=runtimes/webview2（网页引擎），data_dir=data（用户数据）。请整体移动安装根目录，不单独改变组件相对位置。

用户配置、人格、声纹、会话、任务、工作区与凭据保存在 data（持久数据）中。首次启动只补缺失模板。backups/user-data（唯一滚动备份）只收集持久数据，先核对新备份摘要再替换旧备份；模型、依赖、日志、浏览器缓存及字节码不备份。恢复先在隔离目录核对 backup.json（备份清单），停机后再恢复所需数据。系统凭据存储保持原状。

在完整源码工程中维护时，npm run desktop:publish（覆盖发布）构建并校验候选，停机后同盘重命名换入固定目录；成功回收旧组件，失败原位恢复，中断记录在下次发布前处理。程序更新不覆盖 data（用户数据），没有变化的模型及运行依赖直接复用。明确导出时执行 scripts/package-desktop.py（最新交付导出），不自动制作整包备份。

VoiceTerminalUIDemo.exe（演示入口）面向完整源码工程，使用固定调试宿主及前端源码；交付设备使用正式入口。演示不启动语音业务。源码业务、正式业务和无界面后端共用同一环境、模型及数据。
''', encoding='utf-8')
    return [{"path":name,"size":(output / name).stat().st_size,"sha256":digest(output / name)} for name in ("VoiceTerminal.exe", "VoiceTerminalUIDemo.exe", "Verify-Package.ps1", "使用与环境说明.md")]


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


def refresh_program(output: Path, desktop: Path, build: str, dsh_runtime=None, python_runtime=None, models=None, webview2=None) -> None:
    """固定目录发布：仅暂存变更组件；旧组件同盘重命名，事务成功后回收。"""
    from voice_terminal.local_layout import default_portable, read_portable
    from voice_terminal.local_files import remove_tree
    from voice_terminal.local_update import install, recover, update_lock
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with update_lock(output):
        # 先恢复中断事务；不能用新构建覆盖上一次恢复所需的暂存文件。
        if (output / 'tmp/update-transaction.json').exists():
            require_stopped(ROOT, output)
        recover(output)
        if (output / 'portable.json').is_file():
            read_portable(output)
        stage = output / 'tmp/publish'
        remove_tree(stage, output)
        candidate = stage / 'new'
        try:
            program = candidate / 'app'
            collect_program(program, desktop)
            print('程序与统一插件候选已收集。', flush=True)
            lock = json.loads((program / 'config/dsh/runtime.lock.json').read_text(encoding='utf-8-sig'))
            components, changed = [], ['app']
            sources = {'python': python_runtime, 'dsh': dsh_runtime, 'models': models, 'webview2': webview2}
            for kind, relative in (('python', 'runtimes/python'), ('dsh', 'runtimes/dsh'), ('webview2', 'runtimes/webview2'), ('models', 'models')):
                source = Path(sources[kind]).resolve() if sources[kind] else output / relative
                metadata_path = source / 'manifest.json'
                if not metadata_path.is_file():
                    if kind != 'models' or not sources[kind]:
                        raise ValueError(f'缺少已校验组件清单：{kind}（运行组件）')
                    record = collect_models(source, candidate / relative)
                    source = candidate / relative
                else:
                    record = json.loads(metadata_path.read_text(encoding='utf-8-sig'))
                    verify(source, record)
                print(f'{kind}（运行组件）候选清单校验通过。', flush=True)
                if record.get('kind') != kind:
                    raise ValueError('运行组件清单类型不符')
                if kind == 'dsh':
                    metadata = json.loads((source / 'runtime.json').read_text(encoding='utf-8-sig'))
                    if metadata.get('source_kind') == OFFICIAL_SOURCE_KIND:
                        artifact = load_official_artifact(program)
                        validate_official_runtime(source, artifact)
                    elif metadata.get('version') != lock['version'] or metadata.get('commit') != lock['commit']:
                        raise ValueError('代理宿主版本与程序锁版不一致')
                current = output / relative
                same = current.is_dir() and (current / 'manifest.json').read_bytes() == (source / 'manifest.json').read_bytes()
                if same:
                    if source != current:
                        verify(current, record)
                    if source == candidate / relative:
                        remove_tree(source, output)
                else:
                    if source != candidate / relative:
                        if source.is_relative_to(output / 'tmp') and not stage.is_relative_to(source):
                            # 工程临时区的已验证候选直接重命名，避免再复制一份大环境。
                            (candidate / relative).parent.mkdir(parents=True, exist_ok=True)
                            source.rename(candidate / relative)
                        else:
                            copy_component(source, candidate / relative, kind=kind, build=record['build'])
                    changed.append(relative)
                effective = current if same else candidate / relative
                components.append({'kind': kind, 'path': relative, 'build': record['build'], 'manifest_sha256': digest(effective / 'manifest.json')})
            roots = root_files(candidate, desktop)
            release = manifest(program, kind='app', build=build)
            release.update(display_version=__version__, compatibility={'desktop_protocol': 1, 'data_format': 1}, components=components, root_files=roots,
                           fields={'build': '构建编号，仅作记录', 'display_version': '与源码一致的程序显示版本', 'components': '固定组件清单', 'root_files': '根启动入口摘要'})
            atomic_json(program / 'release.json', release)
            atomic_json(candidate / 'portable.json', default_portable())
            verify(program, release)
            print('待换入程序已校验，开始停机检查与更新事务。', flush=True)
            changed += [item['path'] for item in roots] + ['portable.json']
            if (candidate / '使用与环境说明.md').exists() and '使用与环境说明.md' not in changed:
                changed.append('使用与环境说明.md')
            install(output, changed, stopped=lambda: require_stopped(ROOT, output))
        except Exception:
            if not (output / 'tmp/update-transaction.json').exists():
                remove_tree(stage, output)
            raise
    print('覆盖发布完成：固定程序及变更组件已校验，短期旧组件与暂存已回收；用户数据未覆盖。')


def main():
    parser = argparse.ArgumentParser(description='固定目录覆盖发布，不自动生成交付压缩包')
    parser.add_argument('--output', type=Path, default=ROOT / '.local', help='output（本机安装根目录）')
    parser.add_argument('--desktop', type=Path, default=ROOT / '.local/build/desktop/development/release', help='desktop（development 开发渠道的原生构建目录）')
    parser.add_argument('--build', default=datetime.now(timezone.utc).strftime('desktop-%Y%m%d-%H%M%S'), help='build（清单构建编号）')
    parser.add_argument('--refresh-program', action='store_true', help='保留公开命令参数，发布始终采用覆盖更新')
    parser.add_argument('--dsh-runtime', type=Path, help='dsh-runtime（新的闭合代理运行组件）')
    parser.add_argument('--python-runtime', type=Path, help='python-runtime（新的后端运行组件）')
    parser.add_argument('--models', type=Path, help='models（新的必要模型集合）')
    parser.add_argument('--webview2', type=Path, help='webview2（新的网页渲染组件）')
    args = parser.parse_args()
    refresh_program(args.output, args.desktop, args.build, args.dsh_runtime, args.python_runtime, args.models, args.webview2)


if __name__ == '__main__':
    main()

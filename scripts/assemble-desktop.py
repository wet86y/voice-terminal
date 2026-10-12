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

CORE_MODELS = ("kws/sherpa-onnx-kws-zipformer-zh-en-3M", "funasr/paraformer-zh-streaming", "funasr/fsmn-vad", "analysis/smart-turn", "analysis/sensevoice", "speaker/campplus-zh-cn-common")


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
    """从独立可信 CRT（C++ 运行库）缓存收集，不依赖 DSH（代理宿主）版本。"""
    source = ROOT / ".local/build/tools/desktop-crt"
    record_path = source / "manifest.json"
    if not record_path.is_file():
        raise FileNotFoundError("缺少独立桌面 C++（编译语言）运行库，请运行 scripts/prepare-desktop-crt.py（运行库准备）。")
    record = json.loads(record_path.read_text("utf-8-sig"))
    if record.get("kind") != "desktop-crt":
        raise ValueError("桌面 C++（编译语言）运行库清单类型无效")
    verify(source, record)
    required = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
    if not required.issubset({row["path"] for row in record["files"]}):
        raise ValueError("桌面 C++（编译语言）运行库缺少必要文件")
    for row in record["files"]:
        name = row["path"]
        if Path(name).name != name:
            raise ValueError("桌面运行库不允许嵌套路径")
        shutil.copy2(source / name, program / name)


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
                 "manage-resources.py", "package-resources.py", "install-official-dsh.py", "dsh-npm.mjs", "relocate-workspace.py", "voice-environment-worker.py"):
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
    from voice_terminal.dsh.distribution import target_spec, check_plugin
    check_plugin(program, target_spec(program))


def root_files(output: Path, desktop: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(desktop / 'VoiceTerminal.exe', output / 'VoiceTerminal.exe')
    shutil.copy2(ROOT / 'scripts/verify-package.ps1', output / 'Verify-Package.ps1')
    (output / '使用与环境说明.md').write_text("""# 使用与环境说明

双击 VoiceTerminal.exe（正式入口）启动。正式入口从自身位置解析工作区，使用 app（已发布本体）内的业务与静态界面，无须源码仓库。系统 WebView2（网页运行引擎）由安装器引导安装。关闭窗口隐藏到托盘；请通过应用或托盘的退出操作回收服务。

首次安装和重装使用 VoiceTerminalSetup.exe（分发安装器）。安装器展开本体，并用随包官方种子权重离线安装CAM++（声纹嵌入模型），卸载旧WeSpeaker（旧声纹模型）资源及旧注册档案。既有CAM++档案、其他用户数据、工作区编号、其他模型和运行环境保留，模型总清单及组件登记同步更新；失败或未提交中断恢复旧内容。其他组件进入正式程序的组件管理页面完成下载、导入与配置。Verify-Package.ps1（本体校验）不依赖后端环境。新模型需重新录制主人声纹，既有筛选及主人打断设置偏好保留。

portable.json（工作区布局）使用 schema=2（第二版）：app（程序本体）、runtimes/python（后端环境）、runtimes/dsh（代理宿主）、models（模型）、data（用户数据）、cache（缓存）、tmp（临时区）、backups（备份）。请整体移动工作区；移动后可直接双击正式入口，不使用系统用户目录定位记录。应用提供工作区切换与整体迁移。

正式程序的“应用更新”页面保留，在线更新后续接入 GitHub（代码与发布托管平台），并使用同一正式程序包与本体替换事务。分发安装器承担首次安装及重装。

源码项目的 npm run desktop:start（正式启动）与双击共用正式入口。npm run desktop:publish（本地发布）构建并事务替换程序本体；npm run dev（源码热更新）通过同一正式入口显式传入 --dev（调试开关）与 --source-root（源码目录）。源码调试保持同一工作区、组件和退出逻辑。同一工作区不能同时启动普通模式与源码调试模式。界面演示使用独立缓存。
""", encoding='utf-8')
    return [{"path":name,"size":(output / name).stat().st_size,"sha256":digest(output / name)} for name in ('VoiceTerminal.exe','Verify-Package.ps1','使用与环境说明.md')]


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
                    if metadata.get('source_kind') == 'npm_package':
                        from voice_terminal.dsh.distribution import inspect_runtime, is_current, target_spec
                        inspect_runtime(program, source, full=True)
                        if sources[kind] and not is_current(metadata, target_spec(program)):
                            raise ValueError('DSH（代理宿主）候选与新程序适配要求不符')
                    elif metadata.get('source_kind') == OFFICIAL_SOURCE_KIND:
                        artifact = load_official_artifact(program)
                        validate_official_runtime(source, artifact)
                    elif lock.get('distribution', {}).get('source_kind') == 'npm_package':
                        catalog = json.loads((program / 'config/resources/catalog.json').read_text(encoding='utf-8-sig'))
                        resource = next(row for row in catalog['resources'] if row['kind'] == 'dsh')
                        historical = resource['source'].get('official_compatibility', lock)
                        if (metadata.get('version') != historical['version'] or metadata.get('commit') != historical['commit']
                                or digest(source / 'manifest.json') != resource.get('legacy_export', {}).get('manifest_sha256')):
                            raise ValueError('旧代理宿主与保留的兼容记录不一致')
                    elif metadata.get('version') != lock['version'] or metadata.get('commit') != lock['commit']:
                        raise ValueError('代理宿主版本与程序兼容配置不一致')
                    if sources[kind] and metadata.get('source_kind') != 'npm_package' and lock.get('distribution', {}).get('source_kind') == 'npm_package':
                        raise ValueError('新分发目标不接受旧 DSH（代理宿主）候选；现用旧组件可保留并在组件页迁移')
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
            release.update(display_version=__version__, role='product', required_components=[], optional_components=['python','models','dsh','webview2'], compatibility={'desktop_protocol': 1, 'data_format': 1}, components=components, root_files=roots,
                           fields={'build': '构建编号，仅作记录', 'display_version': '与源码一致的程序显示版本', 'components': '固定组件清单', 'root_files': '根启动入口摘要'})
            atomic_json(program / 'release.json', release)
            portable = read_portable(output) if (output / 'portable.json').is_file() else default_portable()
            portable['webview2'] = None
            atomic_json(candidate / 'portable.json', portable)
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
    parser.add_argument('--desktop', type=Path, default=ROOT / '.local/build/desktop/product/release', help='desktop（统一正式程序构建目录）')
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

"""Distribution（便携分发）：离线清单校验、停机复制迁移与环境检测。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from .paths import AppPaths, atomic_json


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def collect_vc_runtime(output: Path, redist: Path) -> dict:
    """从已授权构建工具的 x64（64 位）可再分发目录原样收集，不复制系统目录。"""
    directories = list(redist.glob('Microsoft.VC*.CRT'))
    if len(directories) != 1 or 'debug_nonredist' in str(redist).lower():
        raise ValueError('请明确指定 MSVC（微软原生编译工具）Redist（可再分发）版本下的 x64（64 位）目录。')
    required = ('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
    if not all((directories[0] / name).is_file() for name in required):
        raise FileNotFoundError('可再分发目录缺少业务依赖所需的 C++（原生运行库）文件。')
    sources = sorted(directories[0].glob('*.dll'))
    for directory in redist.glob('Microsoft.VC*.OpenMP'):
        sources.extend(sorted(directory.glob('*.dll')))
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for source in sources:
        shutil.copy2(source, output / source.name)
        records.append({'file': source.name, 'size': source.stat().st_size, 'sha256': digest(source)})
    (output / 'VC-Runtime-Notice.txt').write_text(
        'Microsoft Visual C++ Runtime（微软 C++ 运行库）\n'
        'Files are copied unmodified from the licensed build tool redistributable directory.\n'
        'License terms and distributable-code list（许可与可再分发文件说明）：\n'
        'https://visualstudio.microsoft.com/license-terms/\n'
        'https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution\n'
        'https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files\n', encoding='utf-8')
    return {'source': directories[0].name, 'version': redist.parent.name, 'files': records}


def files(root: Path, *, exclude_roots=()):
    """拒绝链接与重解析目录；不能把开发机绝对联接当成发行文件。"""
    if root.is_symlink() or root.is_junction():
        raise ValueError(f"发行／迁移来源根目录不能是链接：{root.name}")
    excluded = {root / path for path in exclude_roots}
    def walk(directory: Path):
        # scandir（带文件属性的目录枚举）复用系统返回的状态，避免每个依赖文件重复打开查询。
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            if Path(entry.path) in excluded:
                continue
            info = entry.stat(follow_symlinks=False)
            path = Path(entry.path)
            if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError(f"发行目录含链接或重解析点：{path.relative_to(root)}")
            if entry.is_dir(follow_symlinks=False):
                yield from walk(path)
            elif entry.is_file(follow_symlinks=False):
                yield path
            else:
                raise ValueError(f"发行目录含特殊文件：{path.relative_to(root)}")
    yield from walk(root)


def safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts or ":" in relative or "\\" in relative:
        raise ValueError(f"文件记录包含不安全路径：{relative}")
    resolved = (root / path).resolve()
    resolved.relative_to(root.resolve())
    return resolved


def manifest(root: Path, *, kind: str, build: str) -> dict:
    candidates = [path for path in files(root) if path.relative_to(root).as_posix() not in {"manifest.json", "release.json"}]
    def describe(path):
        return {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": digest(path)}
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="release-hash") as workers:
        records = list(workers.map(describe, candidates))
    return {"schema": 1, "kind": kind, "build": build, "files": records}


def verify(root: Path, record: dict, *, exact=True) -> None:
    if record.get("schema") != 1 or not isinstance(record.get("files"), list):
        raise ValueError("不支持的组件清单格式")
    expected = set()
    for item in record["files"]:
        relative = item["path"]
        if not relative or Path(relative).is_absolute() or '..' in Path(relative).parts or ':' in relative or '\\' in relative:
            raise ValueError(f'文件记录包含不安全路径：{relative}')
        if relative.casefold() in expected:
            raise ValueError(f"组件清单含重复文件：{relative}")
        expected.add(relative.casefold())
    actual_paths = None
    if exact:
        # 先遍历并拒绝所有重解析点，再按已枚举的物理文件核对；避免每项重复规范化根路径。
        actual_paths = {p.relative_to(root).as_posix().casefold(): p for p in files(root)
                        if p.relative_to(root).as_posix().casefold() not in {'manifest.json', 'release.json'}}
        if set(actual_paths) != expected:
            raise ValueError('组件文件集合与发布清单不一致')
    def validate_file(item):
        relative = item["path"]
        path = actual_paths[relative.casefold()] if actual_paths is not None else safe_path(root, relative)
        if not path.is_file() or path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError(f"文件校验失败：{relative}")
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="release-verify") as workers:
        for _ in workers.map(validate_file, record["files"]):
            pass


def verify_install(root: Path) -> None:
    """验证唯一便携配置与当前组件组合，不将用户数据纳入发布文件。"""
    root = root.resolve()
    from .local_layout import read_portable
    selected = read_portable(root)
    program = safe_path(root, selected["app"])
    release = json.loads((program / "release.json").read_text(encoding="utf-8-sig"))
    verify(program, release)
    required = set(release.get('required_components', ['python', 'models', 'dsh', 'webview2']))
    optional = set(release.get('optional_components', []))
    kinds = {item['kind'] for item in release['components']}
    if not required.issubset(kinds) or kinds - (required | optional):
        raise ValueError('发行组合与必要／可选组件定义不符')
    if required | optional != {'python', 'models', 'dsh', 'webview2'}:
        raise ValueError('发行组合必须定义全部四类组件')
    for component in release["components"]:
        directory = safe_path(root, component["path"])
        metadata = directory / "manifest.json"
        if component['kind'] in optional and not metadata.is_file():
            continue
        if digest(metadata) != component["manifest_sha256"]:
            raise ValueError(f"组件清单摘要不符：{component['kind']}")
        record = json.loads(metadata.read_text(encoding="utf-8-sig"))
        if record.get("kind") != component["kind"] or record.get("build") != component["build"]:
            raise ValueError("组件标识与程序要求不符")
        selected_path = selected.get(component["kind"])
        if component["kind"] == "python":
            selected_path = str(Path(selected_path).parent).replace("\\", "/") if selected_path else None
        if selected_path != component["path"]:
            raise ValueError(f"便携配置不符合程序所需组合：{component['kind']}")
        verify(directory, record)
    for item in release.get("root_files", []):
        path = safe_path(root, item["path"])
        if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError(f"根入口校验失败：{item['path']}")


def unpack(archive: Path, target: Path) -> None:
    """安全解包：拒绝穿越、链接、重复名称和 Windows（操作系统）特殊路径。"""
    target.mkdir(parents=True, exist_ok=True)
    seen = set()
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            # ZipInfo（归档条目）在 Windows（操作系统）会规范化反斜杠；校验原始发布名称。
            relative = entry.orig_filename.rstrip("/")
            if not relative:
                continue
            destination = safe_path(target, relative)
            if relative.casefold() in seen or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("压缩包含重复文件或链接")
            seen.add(relative.casefold())
            if any(part.rstrip(" .") != part or part.split(".")[0].casefold() in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))} for part in Path(relative).parts):
                raise ValueError("压缩包含系统特殊名称")
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as incoming, destination.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)


MIGRATION_ITEMS = {
    ".env.local": ".env.local",  # 凭据文件只复制，不写入输出或清单内容。
    "config/local.yaml": "config/local.yaml",
    "config/product-settings.json": "config/product-settings.json",
    "config/model-registry.json": "config/model-registry.json",
    "config/runtime-resources.json": "config/runtime-resources.json",
    "config/continuity-state.json": "config/continuity-state.json",
    "config/voice-mailbox.json": "config/voice-mailbox.json",
    "config/wake-tasks.json": "config/wake-tasks.json",
    "config/task-delivery-state.json": "config/task-delivery-state.json",
    "config/dsh-task-deliveries.json": "config/dsh-task-deliveries.json",
    "config/continuity-handoff.json": "config/continuity-handoff.json",
    "config/handoff-pending-transfer.json": "config/handoff-pending-transfer.json",
    "config/persona": "persona",
    "config/speaker": "speaker",
    ".runtime/continuity": "history",
    ".runtime/dsh/session-mappings.json": "dsh/session-mappings.json",
    ".runtime/dsh/voice-session.json": "history/active-voice-session.json",
    ".runtime/dsh/active-voice-session.json": "history/active-voice-session.json",
    "logs": "logs",
}


def require_stopped(source: Path, target: Path) -> None:
    import psutil
    # 仅检查属于本项目的进程；不用端口或无创建时间的旧 PID（进程标识）猜测归属。
    roots = [source.resolve(), target.resolve()]
    for process in psutil.process_iter(["cmdline", "pid"]):
        if process.pid == os.getpid():
            continue
        try:
            command = " ".join(process.info["cmdline"] or []).casefold()
            if not any(module in command for module in ("voice_terminal.application.launch", "apps.server.main", "apps.server.asr_worker", "apps.server.analysis_worker", "voice_terminal.speaker.worker", "voiceterminaldesktop.exe", "voiceterminal.exe", "managed-node.cjs", "runtime-bootstrap.mjs", "apps/cli/lib/bin.js", "apps\\cli\\lib\\bin.js")):
                continue
            locations = []
            for argument in process.info['cmdline'] or []:
                candidate = Path(argument.split('=',1)[-1].strip('"'))
                if candidate.is_absolute():
                    locations.append(candidate.resolve())
            for getter in (process.cwd, process.exe):
                try:
                    locations.append(Path(getter()).resolve())
                except (psutil.Error, OSError):
                    pass
            if any(location.is_relative_to(root) for root in roots for location in locations):
                raise RuntimeError("请先退出应用和所属业务服务，再执行数据迁移。")
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue


def migrate(source: Path, target: Path, *, check_running=True) -> dict:
    source, target = source.resolve(), target.resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source / "config"):
        raise ValueError("迁移目标不能覆盖原项目或配置目录")
    if check_running:
        require_stopped(source, target)
    mapping = dict(MIGRATION_ITEMS)
    dsh_home = source / ".runtime/dsh/home"
    if dsh_home.is_symlink() or dsh_home.is_junction():
        raise ValueError("用户迁移不能跟随外部宿主目录链接")
    if dsh_home.is_dir():
        # 只迁移用户状态；profiles（配置档）的插件、依赖和发行模板重建。
        for path in dsh_home.iterdir():
            if path.name not in {"profiles", "node_modules", "cache", "logs", ".pnpm-store"}:
                mapping[path.relative_to(source).as_posix()] = "dsh/" + path.name
        profile = dsh_home / "profiles/web/cordis.patch.yml"
        if profile.is_file():
            mapping[profile.relative_to(source).as_posix()] = "dsh/profiles/web/cordis.patch.yml"
    planned = []
    destinations = set()
    for origin, destination in mapping.items():
        old = source / origin
        if old.is_symlink() or old.is_junction():
            raise ValueError("用户迁移不能跟随外部链接")
        if not old.exists():
            continue
        incoming = list(files(old)) if old.is_dir() else [old]
        for path in incoming:
            relative = Path(destination) / path.relative_to(old) if old.is_dir() else Path(destination)
            if path.is_symlink() or path.is_junction():
                raise ValueError("用户迁移不能跟随外部链接")
            new = safe_path(target, relative.as_posix())
            if new.exists():
                raise FileExistsError(f"迁移目标已有同名数据：{relative.as_posix()}")
            key = relative.as_posix().casefold()
            if key in destinations:
                raise FileExistsError(f"旧数据存在两个映射到同一文件的来源：{relative.as_posix()}")
            destinations.add(key)
            planned.append((path, relative))
    target.parent.mkdir(parents=True, exist_ok=True)
    # 在同卷暂存完整副本；中断时旧数据仍是权威，重跑不会覆盖冲突。
    with tempfile.TemporaryDirectory(prefix="voice-migration-", dir=target.parent) as staging:
        staging = Path(staging)
        records = []
        for old, relative in planned:
            copy = staging / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, copy)
            fingerprint = digest(old)
            if digest(copy) != fingerprint:
                raise OSError(f"迁移副本校验失败：{relative}")
            records.append({"path": relative.as_posix(), "size": copy.stat().st_size, "sha256": fingerprint})
        target.mkdir(parents=True, exist_ok=True)
        for _, relative in planned:
            new = safe_path(target, relative.as_posix())
            new.parent.mkdir(parents=True, exist_ok=True)
            # 排他创建避免检查后并发覆盖，单文件复制也会再校验。
            with (staging / relative).open("rb") as incoming, new.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            if digest(new) != digest(staging / relative):
                raise OSError(f"迁移目标校验失败：{relative}")
        record = {"schema": 1, "status": "copied_verified", "files": records, "source": str(source), "data_format": 1}
        atomic_json(target / "state/migration.json", record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="便携目录离线校验与停机数据导入")
    tasks = parser.add_subparsers(dest="task", required=True)
    command = tasks.add_parser("migrate", help="复制并验证旧数据，保留原目录")
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--data", type=Path, required=True)
    command = tasks.add_parser("verify", help="验证只读组件")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--manifest", type=Path, required=True)
    command = tasks.add_parser("verify-install", help="校验整个启用组合，排除用户数据")
    command.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.task == "migrate":
        record = migrate(args.source, args.data)
        print(f"已复制并验证 {len(record['files'])} 个用户文件；原目录保留。凭据管理器密钥不随文件迁移。")
    elif args.task == "verify-install":
        verify_install(args.root)
        print("程序、根入口、组件清单与当前启用组合的 SHA-256（文件摘要）校验通过；用户数据未参与发布验证。")
    else:
        verify(args.root, json.loads(args.manifest.read_text(encoding="utf-8-sig")))
        print("组件大小、SHA-256（文件摘要）及文件集合校验通过。")


if __name__ == "__main__":
    main()

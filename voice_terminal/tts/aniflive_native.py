"""AnifLive（本地语音引擎）的固定源码与 Windows（操作系统）兼容准备。"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile

UPSTREAM_COMMIT = "9bc349d3a71362b3bfdccbc630983809800954fd"
ARCHIVE_SHA256 = "67929fedc70df998a6006028b6385743611c5d5a78a5ede3b6713f64435eada2"
PATCH_REVISION = 3  # PATCH_REVISION（本机兼容补丁修订）。
PATCHED_SHA256 = {
    "scripts/setup_shared_assets.py": "13c89086b7a499b0be3d408f476032832c7961e5840fbd3c9764ee98a430dc84",
    "minimal_inference/export_onnx.py": "f554f3f3cc36c24650a31dde5fb4313179340fd1024653b8ae10ba32e764c4a8",
    "minimal_inference/GPT_SoVITS/text/korean.py": "07b5f540a6664b028bfcdabaed1c0a064d0e3a2d2fc61d9f87d1a111bb974fe7",
    "src/aniflive_tts/service.py": "ba3ed25c5eac40522af6986450e2e4f03407bfa531770517edd6ecf3d56be29e",
    "src/aniflive_tts/backend/trt_builder.py": "70f8b41a9f71dc0311cadcc3bc48a4a5fcb9da2951ef2e2a63cd490e3f1d7753",
}  # PATCHED_SHA256（本修订的固定补丁结果摘要）。


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contained(root: Path, path: Path) -> Path:
    """contained（受管路径）：逐级拒绝链接及目录联接。"""
    root, path = Path(root).absolute(), Path(path).absolute()
    if ".." in root.parts or ".." in path.parts:
        raise ValueError("受管路径不能包含父目录跳转。")
    try:
        parts = path.relative_to(root).parts
    except ValueError as error:
        raise ValueError("路径越出受管目录。") from error
    for cursor in [root, *root.parents]:
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError("受管目录祖先不能包含链接或目录联接。")
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError("受管路径不能包含链接或目录联接。")
    return path


def nominal_vram_supported(total_bytes: int, *, windows: bool) -> bool:
    """仅兼容 Windows 标称 8 GiB（吉比字节）显卡的少量驱动保留。"""
    return type(total_bytes) is int and total_bytes >= 8 * 1024**3 - (16 * 1024**2 if windows else 0)


def patch_source(source: Path) -> dict:
    """固定归档在隔离候选内应用补丁；调用者负责最终启用。"""
    records = []

    def replace(relative: str, old: str, new: str):
        path = contained(source, source / relative)
        original = path.read_bytes()
        text = original.decode("utf-8")
        if text.count(old) != 1:
            raise ValueError(f"上游源码结构不匹配，不能应用兼容补丁：{relative}")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
        prior = next((row for row in records if row['path'] == relative), None)
        if prior is not None:
            prior['after_sha256'] = digest(path)
        else:
            records.append({"path": relative, "before_sha256": hashlib.sha256(original).hexdigest(),
                            "after_sha256": digest(path)})

    replace("minimal_inference/export_onnx.py", "ssl_model = ssl_model.to(device)",
            "ssl_model = ssl_model.float().to(device)  # HuBERT（语音编码器）使用单精度导出。")
    replace("minimal_inference/export_onnx.py", "torch.set_num_threads(4)",
            "from voice_terminal.resources import model_thread_budget\n"
            "    torch.set_num_threads(model_thread_budget())  # 使用项目资源策略，不固定测试线程数。")
    replace("minimal_inference/GPT_SoVITS/text/korean.py",
            'dictionary_path = mecab_ko_dic.dictionary_path.as_posix()',
            'dictionary_path = getattr(mecab_ko_dic, "dictionary_path", None)\n'
            '            if dictionary_path is None:\n'
            '                dictionary_path = getattr(mecab_ko_dic, "DICDIR", None)\n'
            '            if dictionary_path is None:\n'
            '                raise RuntimeError("韩语词典没有有效目录。")\n'
            '            dictionary_path = str(dictionary_path).replace("\\\\", "/")')
    replace("src/aniflive_tts/service.py",
            "if int(properties.total_memory) < 8 * 1024 * 1024 * 1024:",
            "if int(properties.total_memory) < 8 * 1024**3 - (16 * 1024**2 if os.name == 'nt' else 0):")
    replace("src/aniflive_tts/backend/trt_builder.py",
            'if not parser.parse_from_file(str(source)):',
            'from voice_terminal.tts.aniflive_native import parser_path\n'
            '        with parser_path(source) as parser_source:\n'
            '            parsed = parser.parse_from_file(str(parser_source))\n'
            '        if not parsed:')
    replace("scripts/setup_shared_assets.py",
            'sorted(item for item in path.rglob("*") if item.is_file())',
            'sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.relative_to(path).as_posix())')
    return {"schema": 1, "upstream_commit": UPSTREAM_COMMIT, "patch_revision": PATCH_REVISION,
            "archive_sha256": ARCHIVE_SHA256, "files": records,
            "fields": {"schema": "源码收据格式", "upstream_commit": "官方源码提交",
                       "patch_revision": "兼容补丁修订", "archive_sha256": "官方归档摘要",
                       "files": "已修改源码", "path": "源码相对路径",
                       "before_sha256": "修改前摘要", "after_sha256": "修改后摘要"}}


def prepare_source(archive: Path, local: Path) -> dict:
    """校验固定归档并准备首次源码；不覆盖现用运行资源。"""
    local = contained(local, local)
    archive = Path(archive)
    with archive.open("rb") as stream:
        archive_bytes = stream.read(64 * 1024**2 + 1)
    if len(archive_bytes) > 64 * 1024**2 or hashlib.sha256(archive_bytes).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("AnifLive 官方归档摘要不匹配。")
    target = contained(local, local / "runtimes/aniflive-tts/source")
    if target.exists():
        receipt = json.loads((target / "windows-source-receipt.json").read_text(encoding="utf-8"))
        if receipt.get("upstream_commit") != UPSTREAM_COMMIT or receipt.get("patch_revision") != PATCH_REVISION:
            raise ValueError("现用源码需要通过组件更新事务升级。")
        if receipt.get("archive_sha256") != ARCHIVE_SHA256 or not isinstance(receipt.get("inventory"), dict):
            raise ValueError("现用源码缺少可信归档与完整文件登记。")
        inventory = receipt["inventory"]
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
            expected_inventory = {row.filename.split("/", 1)[1]: hashlib.sha256(bundle.read(row)).hexdigest()
                                  for row in bundle.infolist() if not row.is_dir()}
        expected_inventory.update(PATCHED_SHA256)
        if inventory != expected_inventory:
            raise ValueError("现用源码登记与固定官方归档或补丁身份不匹配。")
        actual = {p.relative_to(target).as_posix() for p in target.rglob("*")
                  if p.is_file() and p.name != "windows-source-receipt.json"
                  and "__pycache__" not in p.parts}
        if actual != set(inventory):
            raise ValueError("已准备源码文件集合损坏。")
        for relative, expected in inventory.items():
            if digest(contained(target, target / relative)) != expected:
                raise ValueError("已准备源码内容损坏。")
        for row in receipt["files"]:
            if digest(contained(target, target / row["path"])) != row["after_sha256"]:
                raise ValueError("已准备源码的兼容补丁内容损坏。")
        return receipt
    task_root = contained(local, local / "tmp/aniflive-source")
    task_root.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix="s-", dir=task_root))
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
            names = bundle.infolist()
            prefix = f"AnifLive-TTS-{UPSTREAM_COMMIT}"
            if len(names) > 20000 or sum(row.file_size for row in names) > 256 * 1024**2:
                raise ValueError("源码归档超出固定资源限制。")
            for row in names:
                parts = PurePosixPath(row.filename).parts
                if not parts or parts[0] != prefix or any(part in {".", ".."} or ":" in part or "\\" in part for part in parts):
                    raise ValueError("源码归档包含非法路径。")
                if (row.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("源码归档不能包含符号链接。")
                if row.is_dir():
                    continue
                destination = contained(candidate, candidate.joinpath(*parts[1:]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(row) as stream, destination.open("xb") as output:
                    shutil.copyfileobj(stream, output)
        receipt = patch_source(candidate)
        receipt["inventory"] = {p.relative_to(candidate).as_posix(): digest(p)
                                for p in sorted(candidate.rglob("*")) if p.is_file()}
        receipt["fields"]["inventory"] = "准备后的完整源码文件摘要；不包含派生字节码"
        (candidate / "windows-source-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(candidate, target)
        return receipt
    finally:
        if candidate.exists():
            shutil.rmtree(contained(task_root, candidate))


def verify_external_data(source: Path) -> None:
    """核对 ONNX（模型交换格式）外部张量引用，不加载张量数据。"""
    import onnx
    model = onnx.load(str(source), load_external_data=False)
    from onnx.external_data_helper import _get_all_tensors
    for tensor in _get_all_tensors(model):
        if tensor.data_location != onnx.TensorProto.EXTERNAL:
            continue
        metadata = {item.key: item.value for item in tensor.external_data}
        value = metadata.get("location", "")
        if not value or "\\" in value or ":" in value or PurePosixPath(value).is_absolute() or any(p in {".", ".."} for p in value.split("/")):
            raise ValueError("外部张量引用必须位于计算图目录内。")
        target = contained(source.parent, source.parent / value)
        if not target.is_file():
            raise ValueError("计算图缺少外部张量文件。")
        offset = int(metadata.get("offset", "0"))
        length = int(metadata["length"]) if "length" in metadata else target.stat().st_size - offset
        if offset < 0 or length < 0 or offset + length > target.stat().st_size:
            raise ValueError("外部张量范围超出实际文件。")


@contextmanager
def parser_path(source: Path):
    """TensorRT（推理引擎）解析期间持有自动盘符别名。"""
    source = Path(source).absolute()
    contained(source.parent, source)
    verify_external_data(source)
    if os.name != "nt" or str(source).isascii():
        yield source
        return
    with _alias_mutex():
        with _mapped_parser_path(source) as alias:
            yield alias


@contextmanager
def _alias_mutex():
    """串行化本应用不同构建进程的盘符分配与撤销。"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    create.restype = ctypes.c_void_p
    wait = kernel.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    wait.restype = ctypes.c_uint
    release = kernel.ReleaseMutex
    release.argtypes = [ctypes.c_void_p]
    release.restype = ctypes.c_int
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    handle = create(None, False, "Local\\VoiceTerminal.AnifLive.ParserAlias")
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法建立计算图盘符分配锁。")
    owned = False
    try:
        owned = wait(handle, 30000) in {0, 0x80}
        if not owned:
            raise RuntimeError("等待计算图盘符分配锁超时。")
        yield
    finally:
        if owned:
            release(handle)
        close(handle)


@contextmanager
def _mapped_parser_path(source: Path):
    if not source.name.isascii():
        raise ValueError("计算图文件名必须使用英文；自动转换任务须生成固定英文名称。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    define = kernel.DefineDosDeviceW
    define.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p]
    define.restype = ctypes.c_int
    query = kernel.QueryDosDeviceW
    query.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    query.restype = ctypes.c_uint
    raw_target = "\\??\\" + str(source.parent)
    for letter in "ZYXWVUTSRQPONMLKJIHGFED":
        drive = letter + ":"
        buffer = ctypes.create_unicode_buffer(32768)
        if query(drive, buffer, len(buffer)):
            continue
        if ctypes.get_last_error() != 2:
            continue
        from .aniflive_aliases import record_alias
        install = os.environ.get('VOICE_INSTALL_ROOT')
        if not install:
            raise RuntimeError('计算图盘符必须由受管任务提供安装目录。')
        receipt = record_alias(Path(install).absolute(), drive, raw_target)
        if not define(1, drive, raw_target):
            receipt.unlink()
            continue
        try:
            alias = Path(drive + "\\") / source.name
            if not os.path.samefile(alias, source):
                raise ValueError("计算图盘符别名与实际文件不一致。")
            yield alias
        finally:
            if not define(1 | 2 | 4, drive, raw_target):
                raise OSError(ctypes.get_last_error(), "无法撤销本任务的盘符别名。")
            receipt.unlink()
        return
    raise RuntimeError("没有可供本次计算图解析使用的空闲盘符。")

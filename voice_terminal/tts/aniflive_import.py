"""AnifLive（本地语音引擎）兼容模型导入的扫描、暂存与状态管理。"""
from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


WEIGHT_EXTENSIONS = {".ckpt", ".pth", ".pt", ".bin", ".safetensors"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg"}
TEXT_EXTENSIONS = {".txt", ".text", ".lab", ".list"}
REFERENCE_CONFIG_EXTENSIONS = {".json", ".yaml", ".yml"}  # 情绪及场景配置候选。
MAX_FILE_BYTES = 4 * 1024**3
MAX_FILES = 50000
MAX_TEXT_BYTES = 256 * 1024
MAX_TEXT_CHARS = 2000
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
TASK_PATTERN = re.compile(r"[a-f0-9]{32}\Z")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    check = getattr(path, "is_junction", None)
    return bool(check and check())


def _safe_relative(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.replace("\\", "/").split("/")):
        raise ValueError("仓库文件路径无效。")
    if "\\" in value or ":" in value:
        raise ValueError("仓库文件路径包含不支持的字符。")
    return value.replace("\\", "/")


def _file_id(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]


def _role(path: str) -> str:
    # 通用 GPT-SoVITS 项目目录不能被当作模型类型；优先判断文件名。
    parts = path.casefold().replace("-", "_").replace(" ", "_").split("/")
    name = parts[-1]
    if name in {"pytorch_model.bin", "model.safetensors"} or any(
        token in path.casefold() for token in ("pretrained_models/", "chinese-hubert", "chinese-roberta")
    ):
        return "unknown"
    if "sovits" not in name and "gpt" not in name:
        name = "/".join(part for part in parts if not ("gpt" in part and "sovits" in part))
    if Path(path).suffix.casefold() == ".ckpt":
        return "gpt"
    if "sovits" in name or "so_vits" in name or re.search(r"(^|[/_.])sov([/_.]|$)", name):
        return "sovits"
    if "gpt" in name or "text2semantic" in name or "text_2_semantic" in name:
        return "gpt"
    if Path(path).suffix.casefold() == ".pth":
        return "sovits"
    return "unknown"


def _classify(files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    weights: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    transcripts: list[dict[str, Any]] = []
    configs: list[dict[str, Any]] = []
    for source in files:
        path = source["path"]
        extension = Path(path).suffix.casefold()
        entry = {
            "file_id": _file_id(path),  # file_id（文件候选标识）：绑定扫描清单内的单个相对路径。
            "path": path,  # path（仓库内相对路径）：仅用于显示和后续受控读取。
            "size": source.get("size"),  # size（文件字节数）：仓库提供或本地扫描得到的大小。
            "role": _role(path),  # role（推测权重类型）：只用于排序，不作为兼容性判据。
        }
        if source.get("revision") is not None:
            entry["revision"] = source["revision"]  # revision（候选文件修订）：保留仓库返回的不可变文件修订。
        if extension in WEIGHT_EXTENSIONS:
            weights.append(entry)
        if extension in AUDIO_EXTENSIONS:
            audio.append(entry)
        if extension in TEXT_EXTENSIONS:
            transcripts.append(entry)
        if extension in REFERENCE_CONFIG_EXTENSIONS:
            configs.append(entry)
    weights.sort(key=lambda row: (row["role"] == "unknown", row["path"].casefold()))
    audio.sort(key=lambda row: row["path"].casefold())
    transcripts.sort(key=lambda row: row["path"].casefold())
    configs.sort(key=lambda row: row["path"].casefold())
    return {"weights": weights, "audio": audio, "transcripts": transcripts, "configs": configs}


def _download_json(url: str) -> tuple[Any, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "VoiceTerminal/AnifLive-Importer", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(32 * 1024 * 1024)), {key.casefold(): value for key, value in response.headers.items()}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取公开模型仓库文件清单，请检查网络、仓库地址或仓库是否公开。") from exc


def _normalize_reference_audio(source: Path, destination: Path) -> Path:
    """Normalize reference audio（参考音频）to the WAV（波形音频）format used by the existing importer（现有导入入口）."""
    try:
        import numpy as np
        import soundfile as sf

        with sf.SoundFile(source) as reader:
            if reader.samplerate <= 0 or reader.channels <= 0:
                raise ValueError("参考音频采样参数无效。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with sf.SoundFile(destination, mode="w", samplerate=reader.samplerate, channels=1,
                              format="WAV", subtype="PCM_16") as writer:
                for samples in reader.blocks(blocksize=65536, dtype="float32", always_2d=True):
                    if samples.size == 0 or not np.isfinite(samples).all():
                        raise ValueError("参考音频包含空块或无效采样值。")
                    mono = samples.mean(axis=1, dtype=np.float32)
                    writer.write(np.clip(mono, -1.0, 1.0))
                    if writer.tell() * 2 > MAX_FILE_BYTES - 128:
                        raise ValueError("标准 WAV 参考音频超过当前导入上限 4 GiB（吉二进制字节）。")
            if destination.stat().st_size <= 44:
                raise ValueError("参考音频没有可用音频帧。")
        return destination
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("参考音频无法识别或解码；请选择 WAV、MP3、FLAC 或 OGG 文件。") from exc


def _persist_reference_candidates(stage: Path, data_root: Path, task_id: str) -> Path:
    """先持久保存参考候选，再允许模型导入任务清理暂存。"""
    if not TASK_PATTERN.fullmatch(task_id):
        raise ValueError('参考资料任务编号无效。')
    stage = stage.absolute()
    if any(_is_reparse(part) for part in (stage, *stage.parents)):
        raise ValueError('参考候选暂存路径不能经过链接。')
    manifest_path = stage / 'reference-candidates.json'
    if _is_reparse(manifest_path) or not manifest_path.is_file():
        raise ValueError('参考候选清单缺失或不可用。')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('task_id') != task_id or not isinstance(manifest.get('files'), dict):
        raise ValueError('参考候选清单不属于本次任务。')
    root = Path(data_root).absolute()
    if any(_is_reparse(part) for part in (root, *root.parents)):
        raise ValueError('参考资料数据目录不能经过链接。')
    parent = root
    for part in ('tts', 'aniflive', 'reference-imports'):
        parent = parent / part
        if _is_reparse(parent):
            raise ValueError('参考资料目录不能经过链接。')
        parent.mkdir(exist_ok=True)
    destination = parent / task_id
    if destination.exists() or _is_reparse(destination):
        raise ValueError('该任务的参考资料已存在，保留原件等待核对。')
    candidate = parent / ('.' + task_id + '-' + uuid4().hex + '.pending')
    candidate.mkdir()
    rows = []
    for original, relative in sorted(manifest['files'].items()):
        _safe_relative(original)
        relative = _safe_relative(relative)
        source = stage / relative
        if any(_is_reparse(part) for part in (source, *source.parents) if part == stage or part.is_relative_to(stage)):
            raise ValueError('参考候选不能经过链接。')
        if not source.is_file():
            raise ValueError('参考候选文件缺失，原暂存保留。')
        output = candidate / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as reader, output.open('xb') as writer:
            digest = hashlib.sha256()
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        with output.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest.hexdigest():
                raise ValueError('持久参考文件内容校验失败。')
        rows.append({'source_path': original, 'path': relative, 'sha256': digest.hexdigest(),
                     'size': output.stat().st_size})
    manifest['persisted_files'] = rows
    manifest['fields']['persisted_files'] = '已持久化文件的来源路径、相对路径、内容摘要及字节数'
    _atomic_json(candidate / 'reference-candidates.json', manifest)
    os.replace(candidate, destination)
    return destination


def _repo_url(source: str) -> tuple[str, str, str | None]:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("请输入公开模型仓库页面地址。")
    parsed = urllib.parse.urlsplit(source.strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("模型仓库地址必须是 HTTPS（加密网页）公开页面。")
    parts = [urllib.parse.unquote(piece) for piece in parsed.path.split("/") if piece]
    if host in {"huggingface.co", "www.huggingface.co", "hf.co"}:
        if len(parts) < 2:
            raise ValueError("请输入 Hugging Face（模型托管平台）的公开模型仓库页面。")
        repository = "/".join(parts[:2])
        if any(part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:2]):
            raise ValueError("Hugging Face 仓库编号格式无效。")
        revision = parts[3] if len(parts) >= 4 and parts[2] in {"tree", "blob"} else None
        return "huggingface", repository, revision
    if host in {"modelscope.cn", "www.modelscope.cn", "modelscope.ai", "www.modelscope.ai"}:
        if parts and parts[0] == "models":
            parts = parts[1:]
        if len(parts) < 2:
            raise ValueError("请输入 ModelScope（魔搭）的公开模型仓库页面。")
        repository = "/".join(parts[:2])
        if any(part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:2]):
            raise ValueError("ModelScope 仓库编号格式无效。")
        query = urllib.parse.parse_qs(parsed.query)
        revision = parts[3] if len(parts) >= 4 and parts[2] in {"tree", "blob"} else None
        revision = revision or query.get("Revision", query.get("revision", [None]))[0]
        return "modelscope", repository, revision
    raise ValueError("网页导入仅支持 Hugging Face（模型托管平台）和 ModelScope（魔搭）公开模型仓库。")


def _huggingface_files(repository: str, requested_revision: str | None) -> tuple[str, list[dict[str, Any]]]:
    encoded_repo = urllib.parse.quote(repository, safe="/")
    api = "https://huggingface.co/api/models/" + encoded_repo
    if requested_revision:
        api += "/revision/" + urllib.parse.quote(requested_revision, safe="")
    metadata, _ = _download_json(api)
    revision = metadata.get("sha") if isinstance(metadata, dict) else None
    if not isinstance(revision, str) or not revision:
        raise ValueError("Hugging Face 未返回可固定的仓库修订版本。")
    files_url = f"https://huggingface.co/api/models/{encoded_repo}/tree/{urllib.parse.quote(revision, safe='')}?recursive=true&expand=true"
    files: list[dict[str, Any]] = []
    next_url: str | None = files_url
    seen: set[str] = set()
    while next_url and len(files) < MAX_FILES:
        if next_url in seen:
            break
        seen.add(next_url)
        payload, headers = _download_json(next_url)
        if not isinstance(payload, list):
            raise ValueError("Hugging Face 返回的文件清单格式无效。")
        for row in payload:
            if isinstance(row, dict) and row.get("type") in {"file", "blob"} and isinstance(row.get("path"), str):
                entry = {"path": _safe_relative(row["path"]), "size": row.get("size"), "revision": revision}
                lfs = row.get("lfs") or {}
                if isinstance(lfs, dict) and re.fullmatch(r"[a-f0-9]{64}", str(lfs.get("oid", ""))):
                    entry["sha256"] = lfs["oid"]  # sha256（仓库声明的大文件内容摘要）。
                files.append(entry)
        link = headers.get("link", "")
        next_url = None
        for item in link.split(","):
            if 'rel="next"' in item:
                match = re.search(r"<([^>]+)>", item)
                if match:
                    candidate = urllib.parse.urlsplit(match.group(1))
                    if candidate.scheme != "https" or (candidate.hostname or "").casefold() not in {"huggingface.co", "www.huggingface.co"}:
                        raise ValueError("Hugging Face 返回了不受支持的分页地址。")
                    next_url = match.group(1)
                    break
    if not files:
        raise ValueError("Hugging Face 仓库没有可读取的模型文件，或仓库不可公开访问。")
    return revision, files[:MAX_FILES]


def _modelscope_files(repository: str, requested_revision: str | None) -> tuple[str, list[dict[str, Any]]]:
    base = "https://www.modelscope.cn/api/v1/models/" + urllib.parse.quote(repository, safe="/")
    details, _ = _download_json(base)
    data = details.get("Data") if isinstance(details, dict) else None
    if not isinstance(data, dict):
        raise ValueError("ModelScope 未返回公开模型仓库信息。")
    revision = requested_revision or data.get("Revision") or "master"
    query = urllib.parse.urlencode({"Revision": revision, "Recursive": "true"})
    payload, _ = _download_json(base + "/repo/files?" + query)
    data = payload.get("Data") if isinstance(payload, dict) else None
    rows = data.get("Files") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("ModelScope 返回的文件清单格式无效。")
    files = []
    for row in rows:
        if not isinstance(row, dict) or row.get("Type", "blob") != "blob" or not isinstance(row.get("Path"), str):
            continue
        path = _safe_relative(row["Path"])
        # ModelScope（魔搭）清单为每个文件返回其最后提交修订；下载使用该不可变修订。
        file_revision = row.get("Revision") or revision
        entry = {"path": path, "size": row.get("Size"), "revision": str(file_revision)}
        checksum = row.get("Sha256") or row.get("SHA256")
        if re.fullmatch(r"[a-f0-9]{64}", str(checksum or "")):
            entry["sha256"] = checksum
        files.append(entry)
    if not files:
        raise ValueError("ModelScope 仓库没有可读取的模型文件，或仓库不可公开访问。")
    return str(revision), files[:MAX_FILES]


class AnifLiveImportService:
    """Import service（导入服务）：只准备输入；正式转换仍由受管桌面任务执行。"""

    def __init__(self, local_root: Path, data_root: Path, *, voice_libraries=None):
        local_candidate = Path(local_root).absolute()
        if _is_reparse(local_candidate):
            raise ValueError("AnifLive 本机缓存根目录不能是目录联接或符号链接。")
        self.local_root = local_candidate.resolve()
        data_candidate = Path(data_root).absolute()
        if _is_reparse(data_candidate):
            raise ValueError("用户数据根目录不能是目录联接或符号链接。")
        self.data_root = data_candidate.resolve()
        self.voice_libraries = voice_libraries  # voice_libraries（运行时唯一音色库实例）。
        self.cache_root = self.local_root / "cache/aniflive-tts"
        self.scans_root = self.cache_root / "import-scans"
        self.staging_root = self.cache_root / "import-staging"
        self.jobs_root = self.cache_root / "import-jobs"
        for directory in (self.scans_root, self.staging_root, self.jobs_root):
            self._ensure_managed_directory(directory)
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._monitors: dict[str, threading.Thread] = {}  # 后端持续交接任务，不依赖设置页保持打开。

    def _ensure_monitor(self, task_id: str) -> None:
        if self.voice_libraries is None:
            return
        with self._lock:
            if task_id in self._monitors and self._monitors[task_id].is_alive():
                return
            def monitor():
                deadline = time.monotonic() + 24 * 60 * 60
                observed = False
                while time.monotonic() < deadline:
                    try:
                        task = json.loads(self._job_path(task_id).read_text(encoding='utf-8'))
                        merged = self._merge_desktop_status(task)
                        status = merged.get('status')
                        if status in {'queued', 'running', 'converting', 'registering_references', 'reference_failed'}:
                            observed = True
                        if observed and status in {'complete', 'failed', 'cancelled'}:
                            return
                    except (OSError, ValueError, KeyError):
                        pass
                    time.sleep(1)
            thread = threading.Thread(target=monitor, name='aniflive-reference-' + task_id[:8], daemon=True)
            self._monitors[task_id] = thread
            thread.start()

    def _ensure_managed_directory(self, path: Path) -> None:
        relative = path.relative_to(self.local_root)
        current = self.local_root
        for part in relative.parts:
            current = current / part
            if _is_reparse(current):
                raise ValueError("AnifLive 导入缓存路径不能经过目录联接或符号链接。")
            current.mkdir(exist_ok=True)
            if not current.resolve().is_relative_to(self.local_root):
                raise ValueError("AnifLive 导入缓存路径越出本机资源目录。")

    def _scan_path(self, scan_id: str) -> Path:
        if not TASK_PATTERN.fullmatch(scan_id):
            raise ValueError("扫描编号无效或已过期。")
        path = self.scans_root / (scan_id + ".json")
        if _is_reparse(self.scans_root) or _is_reparse(path):
            raise ValueError("扫描记录不能是目录联接或符号链接。")
        return path

    def _job_path(self, task_id: str) -> Path:
        if not TASK_PATTERN.fullmatch(task_id):
            raise ValueError("导入任务编号无效或已过期。")
        path = self.jobs_root / (task_id + ".json")
        if _is_reparse(self.jobs_root) or _is_reparse(path):
            raise ValueError("任务记录不能是目录联接或符号链接。")
        return path

    def scan(self, source_type: str, source: str) -> dict[str, Any]:
        if source_type not in {"local", "huggingface", "modelscope"}:
            raise ValueError("来源类型需为本地文件夹、Hugging Face 或 ModelScope。")
        if source_type == "local":
            selected_root = Path(source).expanduser()
            if _is_reparse(selected_root):
                raise ValueError("所选模型文件夹不能是符号链接或目录联接。")
            root = selected_root.resolve(strict=True)
            if not root.is_dir():
                raise ValueError("请选择模型包所在文件夹。")
            files: list[dict[str, Any]] = []
            for current, dirs, names in os.walk(root, followlinks=False):
                current_path = Path(current)
                dirs[:] = [name for name in dirs if not _is_reparse(current_path / name)]
                for name in names:
                    path = current_path / name
                    if _is_reparse(path) or not path.is_file():
                        continue
                    try:
                        relative = path.relative_to(root).as_posix()
                        size = path.stat().st_size
                    except OSError:
                        continue
                    files.append({"path": _safe_relative(relative), "size": size})
                    if len(files) > MAX_FILES:
                        raise ValueError("文件夹包含超过 50000 个文件，请选择模型文件所在的较小子目录。")
            source_type_saved, source_saved, revision = "local", str(root), None
        else:
            detected, repository, requested_revision = _repo_url(source)
            if source_type not in {"huggingface", "modelscope"} or detected != source_type:
                raise ValueError("来源类型与仓库地址不一致。")
            revision, files = (_huggingface_files(repository, requested_revision) if detected == "huggingface"
                               else _modelscope_files(repository, requested_revision))
            source_type_saved, source_saved = detected, repository
        classified = _classify(files)
        if not classified["weights"]:
            raise ValueError("没有发现常见模型权重文件；可尝试选择权重文件所在的较小文件夹。")
        scan_id = uuid4().hex
        record = {
            "scan_id": scan_id, "source_type": source_type_saved, "source": source_saved,
            "revision": revision, "files": files, "created_at": int(time.time()),
        }
        _atomic_json(self._scan_path(scan_id), record)
        return {
            "scan_id": scan_id, "source_type": source_type_saved, "source": source_saved,
            "revision": revision, "files": [
                {"file_id": _file_id(row["path"]), "path": row["path"], "size": row.get("size")}
                for row in files
            ], "candidates": {
                "weights": classified["weights"], "audio": classified["audio"],
                "transcripts": classified["transcripts"], "configs": classified["configs"],
            }, "fields": {
                "scan_id": "本次只读扫描编号，后续导入会按此清单读取。",
                "source_type": "来源类型：本地文件夹、Hugging Face 或 ModelScope。",
                "source": "扫描的本机目录或公开模型仓库编号。",
                "revision": "扫描时固定的仓库修订；ModelScope 文件各自固定到清单返回的提交。",
                "files": "本次扫描找到的全部仓库文件。",
                "file_id": "候选文件标识，用于绑定本次清单中的文件。",
                "path": "来源中的相对文件路径。",
                "size": "文件大小字节数；来源未提供时为空。",
                "candidates": "按常见文件扩展名整理的候选分组。",
                "weights": "可能的 GPT 或 SoVITS 权重；名称只用于排序。",
                "audio": "WAV、MP3、FLAC 或 OGG 参考音频候选。",
                "transcripts": "TXT、LAB、TEXT 或 LIST 参考转写候选。",
                "role": "依据文件路径推测的权重角色；不决定最终兼容性。",
                "fields": "本响应字段说明。",
            },
        }

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        scan_id = body.get("scan_id")
        if not isinstance(scan_id, str):
            raise ValueError("请先扫描模型来源。")
        record = json.loads(self._scan_path(scan_id).read_text(encoding="utf-8"))
        if record.get("scan_id") != scan_id:
            raise ValueError("扫描记录与本次清单编号不匹配，请重新扫描。")
        model_id = body.get("model_id")
        name = body.get("name")
        if not isinstance(model_id, str) or len(model_id) > 50 or not ID_PATTERN.fullmatch(model_id):
            raise ValueError("模型编号需为 1–50 位英文小写字母、数字、点、下划线或短横线。")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("模型名称需为 1–80 个字符。")
        selected_gpt = body.get("selected_gpt")
        selected_sovits = body.get("selected_sovits")
        if not isinstance(selected_gpt, str) or not isinstance(selected_sovits, str) or selected_gpt == selected_sovits:
            raise ValueError("请选择一份 GPT 权重和一份 SoVITS 权重。")
        by_id = {_file_id(row["path"]): row for row in record["files"]}
        gpt = by_id.get(selected_gpt)
        sovits = by_id.get(selected_sovits)
        candidates = _classify(record["files"])
        weight_ids = {row["file_id"] for row in candidates["weights"]}
        audio_ids = {row["file_id"] for row in candidates["audio"]}
        transcript_ids = {row["file_id"] for row in candidates["transcripts"]}
        if not gpt or not sovits or selected_gpt not in weight_ids or selected_sovits not in weight_ids:
            raise ValueError("所选权重不在本次扫描的文件清单中，请重新扫描。")
        selected_audio = body.get("reference_audio")
        selected_text = body.get("reference_text_file")
        if selected_audio is not None and (not isinstance(selected_audio, str) or selected_audio not in audio_ids):
            raise ValueError("参考音频候选不在本次扫描清单中。")
        if selected_text is not None and (not isinstance(selected_text, str) or selected_text not in transcript_ids):
            raise ValueError("参考转写候选不在本次扫描清单中。")
        selected_references = body.get('selected_reference_files', list(audio_ids))
        if (not isinstance(selected_references, list) or len(selected_references) > MAX_FILES
                or any(not isinstance(item, str) or item not in audio_ids for item in selected_references)
                or len(set(selected_references)) != len(selected_references)):
            raise ValueError('所选参考音频必须属于本次扫描清单且不能重复。')
        if selected_audio and selected_audio not in selected_references:
            selected_references = [*selected_references, selected_audio]
        reference_text = body.get("reference_text", "")
        if not isinstance(reference_text, str) or len(reference_text.strip()) > MAX_TEXT_CHARS:
            raise ValueError("参考转写最多 2000 个字符。")
        language = body.get("reference_language", "zh")
        if language not in {"zh", "yue", "en", "ja", "ko"}:
            raise ValueError("参考语言需为 zh（中文）、yue（粤语）、en（英语）、ja（日语）或 ko（韩语）。")
        task_id = uuid4().hex
        task = {
            "task_id": task_id, "scan_id": scan_id, "source_type": record["source_type"],
            "source": record["source"], "revision": record["revision"], "model_id": model_id,
            "name": name.strip(), "selected_gpt": selected_gpt, "selected_sovits": selected_sovits,
            "reference_candidates": {"audio": candidates["audio"], "transcripts": candidates["transcripts"],
                                     "configs": candidates["configs"]},
            "reference_audio": selected_audio, "reference_text_file": selected_text,
            "selected_reference_files": selected_references,
            "reference_text": reference_text.strip(), "reference_language": language,
            "status": "staging", "phase": "正在整理模型与参考文件", "progress": 0,
            "created_at": int(time.time()), "error": None, "stage_ready": False,
        }
        with self._lock:
            registry_path = self.data_root / "config/aniflive-models.json"
            if _is_reparse(registry_path.parent) or _is_reparse(registry_path):
                raise ValueError("AnifLive 模型登记不能经过目录联接或符号链接。")
            if registry_path.is_file():
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
                if model_id in registry.get("models", {}):
                    raise ValueError("该模型编号已经登记；请为导入模型使用一个新的唯一编号。")
            for existing_path in self.jobs_root.glob("*.json"):
                if _is_reparse(existing_path):
                    raise ValueError("导入任务记录不能是目录联接或符号链接。")
                try:
                    existing = json.loads(existing_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if existing.get("model_id") == model_id:
                    raise ValueError("该模型编号已被另一导入任务使用；请为模型填写新的唯一编号。")
            _atomic_json(self._job_path(task_id), task)
            thread = threading.Thread(target=self._stage_worker, args=(task_id, record, task),
                                      name=f"aniflive-import-{task_id[:8]}", daemon=True)
            self._threads[task_id] = thread
            thread.start()
        return self._public_task(task)

    def start_existing_references(self, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """为已安装模型启动参考资料导入，不转换或重建模型。"""
        from .settings import valid_local_model
        if not valid_local_model(model_id) or not isinstance(body, dict) or set(body) - {'scan_id', 'selected_reference_files'}:
            raise ValueError('参考导入请求或模型编号无效。')
        if self.voice_libraries is None:
            raise ValueError('音色库尚未就绪。')
        task_id = uuid4().hex
        task = {'task_id': task_id, 'model_id': model_id, 'job_kind': 'reference_import',
                'name': model_id, 'status': 'staging', 'phase': '准备模型来源参考资料',
                'progress': 0, 'created_at': int(time.time()), 'error': None}
        scan_id = body.get('scan_id')
        record = None
        default_path = None
        if scan_id:
            record = json.loads(self._scan_path(scan_id).read_text(encoding='utf-8'))
            task['scan_id'] = scan_id
        else:
            presets_path = Path(__file__).resolve().parents[2] / 'config/aniflive-presets.json'
            presets = json.loads(presets_path.read_text(encoding='utf-8'))
            preset = next((row for row in presets.get('models', []) if row.get('id') == model_id), None)
            if not preset or not preset.get('repository') or not (preset.get('reference', {}).get('path') or preset.get('reference', {}).get('asset')):
                raise ValueError('该模型没有可用的预设参考来源，请先扫描原始模型目录或公开仓库。')
            task['preset_source'] = {'repository': preset['repository'], 'revision': preset['revision']}
            default_path = preset['reference'].get('path')
            task['bundled_reference_only'] = bool(preset['reference'].get('asset') and not default_path)
        task['selected_reference_files'] = body.get('selected_reference_files')
        _atomic_json(self._job_path(task_id), task)
        thread = threading.Thread(target=self._existing_reference_worker,
            args=(task_id, task, record, default_path), name='aniflive-existing-reference-' + task_id[:8], daemon=True)
        self._threads[task_id] = thread
        thread.start()
        return self._public_task(task)

    def _existing_reference_worker(self, task_id, task, record, default_path):
        try:
            from .aniflive_models import AnifLiveModelRegistry
            from .aniflive_native import contained
            from .aniflive_reference_registration import register_reference_bundle
            package, _, _ = AnifLiveModelRegistry(self.local_root, self.data_root).resolve(task['model_id'])
            if task.get('bundled_reference_only'):
                from .aniflive_reference_registration import register_model_default
                voice = register_model_default(self.voice_libraries.for_model(task['model_id']), package)
                self._update(task_id, status='complete', phase='模型默认参考已登记', progress=100, error=None,
                             reference_registration={'ready': 1, 'pending': 0, 'voice_id': voice['id'], 'revision': voice['revision']})
                return
            if record is None:
                source = task['preset_source']
                revision, files = _huggingface_files(source['repository'], source['revision'])
                if revision != source['revision']:
                    raise ValueError('预设参考来源修订与固定版本不匹配。')
                files = [row for row in files if row['path'].startswith(('references/', 'profiles/'))]
                record = {'source_type': 'huggingface', 'source': source['repository'], 'revision': revision, 'files': files}
            candidates = _classify(record['files'])
            valid = {row['file_id'] for row in candidates['audio']}
            selected = task.get('selected_reference_files')
            if selected is None:
                selected = sorted(valid)
            if not isinstance(selected, list) or len(set(selected)) != len(selected) or any(item not in valid for item in selected):
                raise ValueError('所选参考录音不属于来源清单。')
            task['selected_reference_files'] = selected
            task['reference_audio'] = _file_id(default_path) if default_path and _file_id(default_path) in valid else None
            stage = contained(self.local_root, self.staging_root / task_id)
            stage.mkdir(parents=True, exist_ok=False)
            bundle = self._stage_candidate_bundle(task_id, record, task, stage)
            self._update(task_id, status='registering_references', phase='正在登记模型参考资料', progress=95)
            registration = register_reference_bundle(self.voice_libraries.for_model(task['model_id']), task['model_id'], package, bundle)
            _atomic_json(bundle / 'reference-registration.json', {'schema': 1, 'task_id': task_id,
                'model_id': task['model_id'], 'registration': registration,
                'fields': {'schema': '参考登记收据格式', 'task_id': '任务编号', 'model_id': '模型编号', 'registration': '参考登记结果'}})
            for current, directories, names in os.walk(stage, followlinks=False):
                for name in [*directories, *names]:
                    contained(self.local_root, Path(current) / name)
            shutil.rmtree(stage)
            self._update(task_id, status='complete', phase='模型参考资料已登记', progress=100, error=None,
                         reference_registration=registration)
        except Exception as exc:
            from .voices import safe_voice_diagnostic
            self._update(task_id, status='reference_failed', phase='参考资料导入失败；已保存的资料保留',
                         progress=0, error=safe_voice_diagnostic(exc))

    def _update(self, task_id: str, **values: Any) -> dict[str, Any]:
        with self._lock:
            path = self._job_path(task_id)
            task = json.loads(path.read_text(encoding="utf-8"))
            task.update(values, updated_at=int(time.time()))
            _atomic_json(path, task)
            return task

    def _source_file(self, record: dict[str, Any], row: dict[str, Any], destination: Path,
                     task_id: str, total: int, completed_before: int) -> tuple[int, Path]:
        path = _safe_relative(row["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = row.get("size")
        if isinstance(size, int) and size > MAX_FILE_BYTES:
            raise ValueError(f"文件 {path} 超过当前导入上限 4 GiB（吉二进制字节）。")
        if record["source_type"] == "local":
            source = Path(record["source"]) / Path(path)
            if any(_is_reparse(parent) for parent in (source, *source.parents) if parent == Path(record["source"]) or parent.is_relative_to(Path(record["source"]))):
                raise ValueError(f"本地文件路径经过符号链接或目录联接：{path}")
            if not source.is_file():
                raise ValueError(f"本地文件已移动、缺失或使用目录联接：{path}")
            actual_size = source.stat().st_size
            if actual_size > MAX_FILE_BYTES:
                raise ValueError(f"文件 {path} 超过当前导入上限 4 GiB（吉二进制字节）。")
            copied = 0
            last_report = 0
            with source.open("rb") as source_stream, destination.open("wb") as output:
                while True:
                    chunk = source_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    copied += len(chunk)
                    if copied - last_report >= 8 * 1024 * 1024:
                        self._update(task_id, progress=min(99, int((completed_before + copied) * 99 / max(total, 1))),
                                     phase=f"正在整理 {Path(path).name}", downloaded_bytes=completed_before + copied)
                        last_report = copied
            if copied != actual_size:
                destination.unlink(missing_ok=True)
                raise ValueError(f"本地文件 {path} 在复制期间发生变化，请重新扫描后再导入。")
            return actual_size, destination
        if record["source_type"] == "huggingface":
            url = f"https://huggingface.co/{urllib.parse.quote(record['source'], safe='/')}/resolve/{urllib.parse.quote(row['revision'], safe='')}/{urllib.parse.quote(path, safe='/')}"
        else:
            params = urllib.parse.urlencode({"Revision": row.get("revision") or record["revision"], "FilePath": path})
            url = f"https://www.modelscope.cn/api/v1/models/{urllib.parse.quote(record['source'], safe='/')}/repo?{params}"
        partial = destination.with_name(destination.name + '.part')
        for attempt in range(3):
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"User-Agent": "VoiceTerminal/AnifLive-Importer", "Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=60) as response:
                    resumed = offset > 0 and response.status == 206
                    if resumed and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                        partial.unlink(missing_ok=True)
                        raise ValueError('下载续传位置不匹配。')
                    downloaded = offset if resumed else 0
                    content_length = response.headers.get('Content-Length')
                    expected = size if isinstance(size, int) else (
                        downloaded + int(content_length) if content_length is not None else None)
                    with partial.open('ab' if resumed else 'wb') as output:
                        last_report = downloaded
                        while chunk := response.read(1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > MAX_FILE_BYTES or (expected is not None and downloaded > expected):
                                raise ValueError('下载文件超出仓库声明大小或导入上限。')
                            output.write(chunk)
                            if downloaded - last_report >= 8 * 1024 * 1024:
                                self._update(task_id, progress=min(99, int((completed_before + downloaded) * 99 / max(total, 1))),
                                             phase=f"正在下载 {Path(path).name}", downloaded_bytes=completed_before + downloaded)
                                last_report = downloaded
                if expected is not None and downloaded != expected:
                    raise ValueError('下载文件未达到仓库声明大小。')
                if row.get('sha256'):
                    with partial.open('rb') as stream:
                        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
                    if checksum != row['sha256']:
                        partial.unlink(missing_ok=True)
                        raise ValueError('下载文件摘要与仓库不一致。')
                os.replace(partial, destination)
                break
            except (OSError, HTTPException, ValueError) as exc:
                # 已完整接收但摘要不符时从头重试；网络中断保留片段供本轮续传。
                if partial.exists() and partial.stat().st_size >= (size if isinstance(size, int) else MAX_FILE_BYTES):
                    partial.unlink()
                if attempt == 2:
                    raise ValueError(f"下载文件 {path} 失败，已自动重试三次；请检查网络或仓库版本。") from exc
                time.sleep(.5 * (attempt + 1))
        return downloaded, destination

    def _stage_candidate_bundle(self, task_id, record, task, stage, total=1):
        by_id = {_file_id(row["path"]): row for row in record["files"]}
        # 参考候选在转换前按来源相对路径暂存；不能只保存文件名后丢失配对关系。
        from .aniflive_reference_catalog import discover_references
        candidates = _classify(record['files'])
        documents: dict[str, bytes] = {}
        staged_candidates: dict[str, str] = {}
        candidate_errors: dict[str, str] = {}
        selected_ids = set(task.get('selected_reference_files', [item['file_id'] for item in candidates['audio']]))
        extra_rows = [*[item for item in candidates['audio'] if item['file_id'] in selected_ids],
                      *candidates['transcripts'], *candidates['configs']]
        for item in extra_rows:
            relative = item['path']
            row = by_id[item['file_id']]
            size_limit = 20 * 1024 * 1024 if Path(relative).suffix.casefold() in AUDIO_EXTENSIONS else MAX_TEXT_BYTES
            if isinstance(row.get('size'), int) and row['size'] > size_limit:
                candidate_errors[relative] = '参考候选文件超出自动导入大小限制。'
                continue
            destination = stage / 'candidates' / item['file_id'] / Path(relative).name
            _, candidate = self._source_file(record, row, destination, task_id, max(total, 1), 0)
            if candidate.stat().st_size > size_limit:
                candidate.unlink(missing_ok=True)
                candidate_errors[relative] = '参考候选文件超出自动导入大小限制。'
                continue
            staged_candidates[relative] = candidate.relative_to(stage).as_posix()
            if Path(relative).suffix.casefold() not in AUDIO_EXTENSIONS:
                documents[relative] = candidate.read_bytes()
        catalog = discover_references([row['path'] for row in record['files']
            if Path(row['path']).suffix.casefold() not in AUDIO_EXTENSIONS or _file_id(row['path']) in selected_ids], documents)
        for item in catalog:
            relative = item['path']
            item['staged_path'] = staged_candidates.get(relative)
            if relative in candidate_errors:
                item.update(status='pending', error=candidate_errors[relative])
            elif item['status'] == 'ready' and item['staged_path']:
                try:
                    from .voices import VoiceLibrary
                    source = stage / item['staged_path']
                    VoiceLibrary._normalize_audio(source.read_bytes(), source.name, minimum=3.0, maximum=10.0)
                except (ValueError, OSError, OverflowError) as exc:
                    item.update(status='pending', error=str(exc))
        _atomic_json(stage / 'reference-candidates.json', {
            'schema': 1, 'task_id': task_id, 'model_id': task['model_id'],
            'source': record['source'], 'source_type': record['source_type'],
            'revision': record['revision'], 'references': catalog,
            'default_audio': by_id[task['reference_audio']]['path'] if task.get('reference_audio') else None,
            'files': staged_candidates, 'errors': candidate_errors,
            'fields': {'schema': '参考候选清单格式', 'task_id': '导入任务编号',
                       'model_id': '模型编号', 'source': '来源目录或仓库',
                       'source_type': '来源类型', 'revision': '来源修订',
                       'default_audio': '转换时选择的默认参考来源路径',
                       'references': '已配对及待完善参考', 'files': '来源路径与暂存文件对应',
                       'errors': '未能暂存候选及原因'}})
        persistent = _persist_reference_candidates(stage, self.data_root, task_id)
        self._update(task_id, reference_bundle=str(persistent))
        return persistent

    def _stage_worker(self, task_id: str, record: dict[str, Any], task: dict[str, Any]) -> None:
        try:
            from .aniflive_preflight import require_writable_workspace
            require_writable_workspace(self.local_root, self.data_root)
            stage = self.staging_root / task_id
            if _is_reparse(self.staging_root) or _is_reparse(stage):
                raise ValueError("任务暂存路径不能是目录联接或符号链接。")
            stage.mkdir(parents=True, exist_ok=False)
            by_id = {_file_id(row["path"]): row for row in record["files"]}
            rows = [by_id[task["selected_gpt"]], by_id[task["selected_sovits"]]]
            if task.get("reference_audio"):
                rows.append(by_id[task["reference_audio"]])
            if task.get("reference_text_file"):
                rows.append(by_id[task["reference_text_file"]])
            total = sum(max(1, int(row.get("size") or 1)) for row in rows)
            staged: dict[str, Path] = {}
            for index, row in enumerate(rows):
                path = row["path"]
                label = "gpt" if _file_id(path) == task["selected_gpt"] else "sovits" if _file_id(path) == task["selected_sovits"] else "reference" if _file_id(path) == task.get("reference_audio") else "transcript"
                extension = Path(path).suffix.lower()
                if label == "gpt":
                    destination = stage / "weights" / ("gpt" + (extension or ".ckpt"))
                elif label == "sovits":
                    destination = stage / "weights" / ("sovits" + (extension or ".pth"))
                elif label == "reference":
                    destination = stage / "reference" / ("source-audio" + (extension or ".wav"))
                else:
                    destination = stage / "reference" / "transcript.txt"
                completed_before = sum(max(1, int(item.get("size") or 1)) for item in rows[:index])
                size, staged_path = self._source_file(record, row, destination, task_id, total, completed_before)
                if label == "reference":
                    staged_path = _normalize_reference_audio(staged_path, stage / "reference" / "audio.wav")
                staged[label] = staged_path
                self._update(task_id, progress=min(99, int(sum(max(1, int(item.get("size") or 1)) for item in rows[:index + 1]) * 99 / max(total, 1))),
                             phase=f"已整理 {index + 1}/{len(rows)} 个所选文件", staged_bytes=size)
            bundle = self._stage_candidate_bundle(task_id, record, task, stage, total)
            transcript = task.get("reference_text", "")
            needs_review = False
            if not transcript and task.get('reference_audio'):
                manifest = json.loads((bundle / 'reference-candidates.json').read_text(encoding='utf-8'))
                source_path = by_id[task['reference_audio']]['path']
                matching = [row for row in manifest.get('references', [])
                            if row.get('path') == source_path and row.get('reference_text', '').strip()]
                if len(matching) == 1:
                    transcript = matching[0]['reference_text']
                    task['reference_language'] = matching[0].get('reference_language') or task['reference_language']
            if not transcript and "transcript" in staged:
                transcript_path = staged["transcript"]
                if transcript_path.stat().st_size > MAX_TEXT_BYTES:
                    raise ValueError("参考转写候选文件超过 256 KiB（千字节），请改为手动输入。")
                transcript = transcript_path.read_text(encoding="utf-8-sig", errors="replace").strip()
                if len(transcript) > MAX_TEXT_CHARS:
                    raise ValueError("参考转写候选超过 2000 个字符，请改为手动输入。")
                # 同名旁注经长度和配对校验后可自动使用，不伪造人工确认。
                needs_review = False
            stage_record = {
                "schema": 1, "task_id": task_id, "model_id": task["model_id"], "name": task["name"],
                "gpt": str(staged["gpt"]), "sovits": str(staged["sovits"]),
                "source": record["source"], "source_type": record["source_type"], "revision": record["revision"],
                "reference_language": task["reference_language"],
            }
            status = "pending_reference"
            phase = "模型已暂存，等待参考录音与准确转写"
            if "reference" in staged:
                stage_record["reference"] = str(staged["reference"])
                stage_record["reference_text"] = transcript
                import soundfile as sf
                duration = sf.info(str(staged['reference'])).duration
                if not 3 <= duration <= 10:
                    status, phase = 'pending_reference', '基础参考录音需为 3–10 秒；原录音已保留，请导入有效片段'
                    stage_record.pop('reference', None)
                elif transcript and not needs_review:
                    self._write_input_spec(stage, stage_record)
                    status, phase = "ready_to_convert", "模型与参考资料已准备，等待启动转换"
                elif needs_review:
                    status, phase = "review_reference", "请核对仓库附带的参考转写"
            elif transcript:
                stage_record["reference_text_draft"] = transcript
            _atomic_json(stage / "import-manifest.json", stage_record)
            self._update(task_id, status=status, phase=phase, progress=100,
                         reference_text_draft=transcript if needs_review or status == "pending_reference" else None,
                         reference_audio_staged=bool(stage_record.get('reference')),
                         stage_ready=status == "ready_to_convert")
            self._ensure_monitor(task_id)
        except Exception as exc:
            self._update(task_id, status="failed", phase="模型暂存失败，可保留现场后重试", error=str(exc), progress=0)

    def _write_input_spec(self, stage: Path, record: dict[str, Any]) -> Path:
        if not record.get("reference") or not record.get("reference_text", "").strip():
            raise ValueError("转换前需要参考录音和准确转写。")
        if _is_reparse(stage):
            raise ValueError("模型任务暂存目录不能是目录联接或符号链接。")
        stage_root = stage.resolve(strict=True)
        for key in ("gpt", "sovits", "reference"):
            source = Path(record[key]).absolute()
            if (_is_reparse(source) or any(_is_reparse(parent) for parent in source.parents)
                    or not source.is_file() or not source.resolve(strict=True).is_relative_to(stage_root)):
                raise ValueError("模型权重与参考录音必须是本次任务暂存目录中的普通文件。")
        spec = {
            "model_id": record["model_id"], "name": record["name"], "gpt": record["gpt"],
            "sovits": record["sovits"], "reference": record["reference"],
            "reference_text": record["reference_text"].strip(),
            "reference_language": record.get("reference_language", "zh"),
            "import_id": record["task_id"],
        }
        path = stage / "aniflive-input.json"
        _atomic_json(path, spec)
        return path

    def add_reference(self, task_id: str, *, reference_path: Path | None,
                      reference_text: str, reference_language: str) -> dict[str, Any]:
        task_path = self._job_path(task_id)
        task = self._merge_desktop_status(json.loads(task_path.read_text(encoding="utf-8")))
        if task.get("status") not in {"pending_reference", "review_reference", "ready_to_convert", "failed"}:
            raise ValueError("导入任务当前阶段不能补充参考资料。")
        if not isinstance(reference_text, str) or not 1 <= len(reference_text.strip()) <= MAX_TEXT_CHARS:
            raise ValueError("参考转写需为 1–2000 个字符。")
        if reference_language not in {"zh", "yue", "en", "ja", "ko"}:
            raise ValueError("参考语言需为 zh、yue、en、ja 或 ko。")
        stage = self.staging_root / task_id
        if _is_reparse(self.staging_root) or _is_reparse(stage) or _is_reparse(stage / "import-manifest.json"):
            raise ValueError("任务暂存路径不能经过目录联接或符号链接。")
        manifest_path = stage / "import-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if reference_path is not None:
            if (not reference_path.is_file() or any(_is_reparse(parent) for parent in reference_path.parents)
                    or _is_reparse(reference_path) or reference_path.stat().st_size > MAX_FILE_BYTES):
                raise ValueError("参考录音素材不可用。")
            reference_folder = stage / "reference"
            if _is_reparse(reference_folder):
                raise ValueError("任务参考录音目录不能是目录联接或符号链接。")
            reference_folder.mkdir(parents=True, exist_ok=True)
            target = reference_folder / "selected-reference.wav"
            if _is_reparse(target):
                raise ValueError("任务参考录音目标不能是目录联接或符号链接。")
            shutil.copyfile(reference_path, target)
            if target.stat().st_size != reference_path.stat().st_size:
                target.unlink(missing_ok=True)
                raise ValueError("参考录音复制期间源文件发生变化，请重新添加录音。")
            manifest["reference"] = str(target)
        manifest["reference_text"] = reference_text.strip()
        manifest["reference_language"] = reference_language
        self._write_input_spec(stage, manifest)
        _atomic_json(manifest_path, manifest)
        return self._update(task_id, status="ready_to_convert", phase="参考资料已就绪，等待启动转换",
                            progress=100, error=None, reference_text_draft=None,
                            reference_audio_staged=True, stage_ready=True)

    def task(self, task_id: str) -> dict[str, Any]:
        task = json.loads(self._job_path(task_id).read_text(encoding="utf-8"))
        if task.get("status") == "staging" and task_id not in self._threads:
            task = self._update(task_id, status="failed", phase="上次程序关闭时中断暂存，缓存原件已保留，请重新扫描重试。",
                                error="模型文件暂存任务未能完成；重新扫描不会修改来源文件。")
        task = self._merge_desktop_status(task)
        if task.get('status') in {'ready_to_convert', 'queued', 'running', 'converting', 'registering_references', 'reference_failed'}:
            self._ensure_monitor(task_id)
        return self._public_task(task)

    def pending(self) -> list[dict[str, Any]]:
        if _is_reparse(self.jobs_root):
            raise ValueError("导入任务目录不能是目录联接或符号链接。")
        results = []
        for path in self.jobs_root.glob("*.json"):
            try:
                if _is_reparse(path):
                    continue
                task = json.loads(path.read_text(encoding="utf-8"))
                if task.get("status") == "staging" and task["task_id"] not in self._threads:
                    task = self._update(task["task_id"], status="failed",
                                        phase="上次程序关闭时中断暂存，缓存原件已保留，请重新扫描重试。",
                                        error="模型文件暂存任务未能完成；重新扫描不会修改来源文件。")
                merged = self._merge_desktop_status(task)
                if merged.get('status') in {'ready_to_convert', 'queued', 'running', 'converting', 'registering_references', 'reference_failed'}:
                    self._ensure_monitor(task['task_id'])
                if merged.get("status") in {"pending_reference", "review_reference", "ready_to_convert", "failed",
                                             "staging", "queued", "running", "converting", "cancelled",
                                             "registering_references", "reference_failed"}:
                    results.append(self._public_task(merged))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(results, key=lambda row: row["created_at"], reverse=True)

    def _merge_desktop_status(self, task: dict[str, Any]) -> dict[str, Any]:
        # 页面轮询和后台监视共用任务状态，禁止两个读取者重复登记或回写旧状态。
        with self._lock:
            current = json.loads(self._job_path(task['task_id']).read_text(encoding='utf-8'))
            return self._merge_desktop_status_locked(current)

    def _merge_desktop_status_locked(self, task: dict[str, Any]) -> dict[str, Any]:
        job_id = "aniflive-import-" + task["task_id"]
        data_root = self.data_root
        jobs_file = data_root / "state/resource-jobs.json"
        if _is_reparse(jobs_file.parent) or _is_reparse(jobs_file):
            raise ValueError("桌面资源任务记录不能经过目录联接或符号链接。")
        if not jobs_file.is_file():
            return task
        previous_status = task.get("status")
        try:
            payload = json.loads(jobs_file.read_text(encoding="utf-8"))
            for job in payload.get("jobs", []):
                if job.get("job_id") == job_id:
                    state = job.get("state")
                    if state == "queued":
                        task.update(status="queued", phase=job.get("phase", "等待转换任务"), progress=0)
                    elif state in {"running", "waiting"} and job.get('phase') != '正在登记音色参考':
                        done = job.get("completed_bytes", 0) or 0
                        total = job.get("total_bytes") or 0
                        task.update(status="converting", phase=job.get("phase", "正在转换与验证"),
                                    progress=min(100, int(done * 100 / total)) if total else 0,
                                    conversion_progress=done, conversion_total=total or None)
                    elif state == "complete" or (state in {'running', 'waiting'} and job.get('phase') == '正在登记音色参考'):
                        if previous_status == 'reference_failed':
                            task.pop('reference_registration', None)
                        if not task.get('reference_registration'):
                            if self.voice_libraries is None:
                                task.update(status='registering_references', phase='等待音色库就绪后登记参考资料', progress=99)
                                break
                            try:
                                from .aniflive_models import AnifLiveModelRegistry
                                from .aniflive_native import contained
                                from .aniflive_reference_registration import register_reference_bundle
                                with self._lock:
                                    package, _, _ = AnifLiveModelRegistry(self.local_root, self.data_root).resolve(task['model_id'])
                                    bundle = contained(self.data_root, self.data_root / 'tts' / 'aniflive' / 'reference-imports' / task['task_id'])
                                    result = register_reference_bundle(self.voice_libraries.for_model(task['model_id']),
                                                                       task['model_id'], package, bundle)
                                _atomic_json(bundle / 'reference-registration.json', {
                                    'schema': 1, 'task_id': task['task_id'], 'model_id': task['model_id'],
                                    'registration': result, 'fields': {'schema': '参考登记收据格式',
                                        'task_id': '导入任务编号', 'model_id': '模型编号',
                                        'registration': '参考登记结果及音色版本'}})
                                task['reference_registration'] = result
                            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                                task.pop('reference_registration', None)
                                task.update(status='reference_failed', phase='模型已登记，参考资料登记失败；资料已保留，可重试',
                                            progress=99, error=str(exc))
                                break
                        if state == 'complete':
                            task.update(status="complete", phase="模型转换、验证与参考资料登记完成", progress=100, error=None)
                        else:
                            task.update(status='registering_references', phase='参考资料已登记，正在完成任务清理', progress=99, error=None)
                    elif state in {"failed", "cancelled"}:
                        task.update(status=state, phase=job.get("phase", "转换任务结束"), error=job.get("error"))
                    break
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if task.get("status") in {"complete", "failed", "cancelled", "reference_failed", "registering_references"}:
            task["updated_at"] = int(time.time())
            _atomic_json(self._job_path(task["task_id"]), task)
        return task

    @staticmethod
    def _public_task(task: dict[str, Any]) -> dict[str, Any]:
        allowed = {"task_id", "scan_id", "model_id", "name", "source_type", "source", "revision", "status", "phase",
                   "progress", "error", "created_at", "reference_text_draft", "reference_audio_staged",
                   "conversion_progress", "conversion_total", "stage_ready", "reference_candidates", "reference_registration", "job_kind"}
        result = {key: task[key] for key in allowed if key in task}
        result["fields"] = {
            "task_id": "导入任务编号。", "scan_id": "创建任务时使用的只读扫描清单编号。",
            "model_id": "用户为新模型填写的唯一英文编号。",
            "name": "用户为新模型填写的界面名称。", "source_type": "来源类型：本地文件夹、Hugging Face 或 ModelScope。",
            "source": "扫描的本机目录或公开模型仓库编号。", "revision": "远程模型文件使用的固定修订。",
            "status": "任务状态：暂存、待补参考、待校对、待转换、转换中、完成或失败。",
            "progress": "当前阶段的百分比进度。", "phase": "当前任务步骤说明。",
            "error": "本轮失败原因；成功或可继续的任务为空。", "created_at": "任务创建时间的 Unix 秒数。",
            "reference_text_draft": "从仓库旁注读取的待核对转写草稿。",
            "reference_audio_staged": "任务暂存目录中是否已有参考音频。",
            "reference_registration": "已就绪、待完善参考数量及登记后的音色版本。",
            "job_kind": "任务用途：模型导入或已有模型参考资料导入。",
            "reference_candidates": "完整保留的可选音频与旁注候选；成功清理模型暂存后仍可追溯来源路径及修订。",
            "audio": "本次扫描发现的所有音频候选，包含相对路径、大小及可用的文件修订。",
            "transcripts": "本次扫描发现的所有转写旁注候选，包含相对路径、大小及可用的文件修订。",
            "file_id": "扫描清单中单个候选文件的选择标识。",
            "path": "来源中的相对文件路径；本地来源保持原样，远程来源按固定修订追溯。",
            "size": "候选文件的字节数；来源未提供时为空。",
            "role": "依据路径名称推测的文件角色，仅供提示。",
            "stage_ready": "是否已有可交给正式转换流程的完整输入。",
            "conversion_progress": "转换任务当前阶段的已处理单位数。",
            "conversion_total": "转换任务当前阶段的单位总数；未知时为空。",
            "fields": "本响应字段说明。",
        }
        return result

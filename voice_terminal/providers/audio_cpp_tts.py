"""audio.cpp（原生音频推理框架）VoxCPM（本地语音合成模型）适配。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from uuid import uuid4
from typing import Any, AsyncIterator, Callable, Mapping

import httpx

from ..log_budget import (
    enforce_log_budget,
    protect_log_files,
    rotate_oversized_log,
    unprotect_log_files,
)
from ..tts.scheduling import TtsSegmentReceipt
from ..tts.capabilities import TtsCapabilities
from ..tts.voices import ResolvedLocalTtsVoice, VoiceLibrary
from ..gpu_performance import GpuPerformanceMode

logger = logging.getLogger(__name__)

_QUEUE_END = object()
_PREWARM_TEXT = "你好，准备开始语音播报。"
_NATIVE_LOG_LIMIT = 4 * 1024 * 1024  # _NATIVE_LOG_LIMIT（原生 stdout 单文件上限）。
_TEXT_PREVIEW_CHARS = 120  # 与在线后端保持一致的可选文本预览长度。
_CONTINUATION_REANCHOR_MIN_SEGMENTS = 3  # 声学续接至少累计三段后，才允许在句末重锚定。
_SENTENCE_END_PUNCTUATION = ("。", "！", "？", "!", "?")  # 句末重锚定的完整句终止符。


def _enabled(name: str, default: str = "") -> bool:
    raw = os.environ.get(name, default).strip().casefold()
    return raw not in {"", "0", "false", "no", "off"}


def _native_trace_enabled() -> bool:
    """原生分阶段计时日志：默认关闭，开启后逐行刷新会扰动绝对耗时。"""
    return _enabled("VOICE_TTS_NATIVE_TRACE")


def _native_log_pump_enabled() -> bool:
    """原生 stdout 时间戳转发：默认开启，置 0 回退为直接重定向。"""
    return _enabled("VOICE_TTS_NATIVE_LOG_PUMP", "1")


def _text_logging_enabled() -> bool:
    """合成文本预览：默认关闭，只有显式开启时才写入日志。"""
    return _enabled("VOICE_TTS_LOG_TEXT")


def text_digest(text: str) -> str:
    """text_digest（文本摘要）：不落内容也能把同一文本块与日志行对上。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def text_preview_suffix(text: str) -> str:
    """可选文本预览后缀；未开启时返回空串，保证默认日志不含会话内容。"""
    if not _text_logging_enabled():
        return ""
    return f" text={text[:_TEXT_PREVIEW_CHARS]!r}"


def _write_native_line(stream, line: bytes) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    text = line.decode("utf-8", "replace").rstrip("\r")
    with contextlib.suppress(OSError, ValueError):
        stream.write(f"{stamp} {text}\n".encode("utf-8"))


_CONTEXT_KEYS = (  # _CONTEXT_KEYS（原生元数据内存池）：不包含模型张量/权重精度设置。
    "weight_context_mb", "text_embedding_graph_context_mb", "lm_step_graph_context_mb",
    "projection_graph_context_mb", "local_encoder_graph_context_mb", "dit_graph_context_mb",
    "audiovae_weight_context_mb", "audiovae_graph_context_mb", "audiovae_encoder_graph_context_mb",
)
_MODEL_CATALOG = {
    # model_id（服务模型标识）、family（原生模型家族）、native_sample_rate（模型原生采样率）。
    "voxcpm1": {
        "id": "voxcpm1-stream",
        "family": "voxcpm1",
        "name": "VoxCPM1",
        "native_sample_rate": 16000,
        "relative_path": Path("VoxCPM1-GGUF") / "voxcpm-0.5b-q8_0-audiovae-f16.gguf",
    },
    "voxcpm2": {
        "id": "voxcpm2-stream",
        "family": "voxcpm2",
        "name": "VoxCPM2",
        "native_sample_rate": 48000,
        "relative_path": Path("VoxCPM2-GGUF") / "voxcpm2-q8_0.gguf",
    },
}


class AudioCppTtsError(RuntimeError):
    """本地 audio.cpp（原生音频推理框架）TTS（文本转语音）错误。"""


class AudioCppDegenerateError(AudioCppTtsError):
    """AudioCppDegenerateError（异常生成）：停止剩余播放，禁止整句自动重播。"""

    discard_audio = True  # discard_audio（丢弃本事务尚未播放的音频）。


def _generation_budget_ms(text: str) -> int:
    """生成时长预算：保留短句停顿余量，按实际正文限制异常长输出。"""
    units = sum(char.isalnum() for char in text)
    return max(4000, units * 500 + 1000)


class AudioCppTtsServer:
    """启动和管理本机 audio.cpp（原生音频推理框架）TTS 服务。"""

    @classmethod
    def runtime_directory(cls, runtime_root: Path) -> Path:
        """runtime_directory（原生运行目录）：优先使用组件安装目录，兼容此前迁入的资源。"""
        root = Path(runtime_root).resolve()
        managed = root / "models/audiocpp/runtime"
        return managed if (managed / "cuda/audiocpp_server.exe").is_file() else root / "runtimes/audiocpp"

    @staticmethod
    def _catalog_program(program_root: Path | None = None) -> Path:
        """_catalog_program（随包清单目录）：正式压缩模块使用宿主传入的程序位置。"""
        return Path(program_root or os.environ.get("VOICE_PROGRAM_ROOT") or Path(__file__).resolve().parents[2])

    @classmethod
    def _runtime_update_error(cls, runtime_root: Path, *, program_root: Path | None = None,
                              verify_hashes: bool = True) -> str | None:
        """_runtime_update_error（原生版本门禁）：自有收据有效也必须达到当前随包目标。"""
        from ..model_installation import inspect_installed_model, model_update_available
        from ..resource_distribution import ResourceCatalog, ResourceError
        try:
            target = ResourceCatalog(cls._catalog_program(program_root)).specs.get("tts-audiocpp-runtime")
            root = Path(runtime_root).resolve()
            runtime = cls.runtime_directory(root)
            if (runtime / "resource-install.json").is_file() and runtime == root / "models/audiocpp/runtime":
                installed = inspect_installed_model(root / "models", "audiocpp/runtime", "tts-audiocpp-runtime",
                                                    verify_hashes=verify_hashes and target is None)
                if installed.error:
                    return f"本地语音运行组件校验失败：{installed.error} 请在组件管理中更新本地语音共用运行程序。"
                if target is not None and model_update_available(target, installed):
                    return (f"本地语音运行组件需要更新：installed_version（已装版本）={installed.version}，"
                            f"target_version（目标版本）={target.version}。请在组件管理中更新本地语音共用运行程序。")
            if target is None:  # 明确不声明原生组件的独立工具清单，不施加产品版本约束。
                return None
            # 无收据的旧迁入目录也核验目标内容；启动前至少复核原生程序和模型规格的实际摘要。
            for item in target.required_files:
                if not verify_hashes and item.path != "cuda/audiocpp_server.exe" and not item.path.startswith("model_specs/"):
                    continue
                path = runtime / item.path
                if not path.is_file() or path.stat().st_size != item.size_bytes:
                    return f"本地语音运行组件不符合当前版本：{item.path}。请在组件管理中更新本地语音共用运行程序。"
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != item.sha256:
                        return f"本地语音运行组件摘要不符合当前版本：{item.path}。请在组件管理中更新本地语音共用运行程序。"
        except (OSError, ResourceError) as exc:
            return f"无法核验本地语音运行组件：{exc}。请修复程序或在组件管理中更新语音运行程序。"
        return None

    @classmethod
    def available_models(cls, runtime_root: Path, *, program_root: Path | None = None,
                         verify_hashes: bool = True, local_model: str | None = None) -> list[str]:
        """available_models（完整本地候选）：权重、原生程序和模型规格均存在才算可启动。"""
        root = Path(runtime_root).resolve()
        runtime = cls.runtime_directory(root)
        specs = runtime / "model_specs"
        if not specs.is_dir():
            specs = root / "sources/audio.cpp/model_specs"
        if not (runtime / "cuda/audiocpp_server.exe").is_file():
            return []
        if error := cls._runtime_update_error(root, program_root=program_root, verify_hashes=verify_hashes):
            logger.warning("audio_cpp_tts.runtime_update_required（原生组件需要更新） %s", error)
            return []
        # 原生组件已同时按安装收据和当前目标核验；权重继续独立复核。
        from ..model_installation import inspect_installed_model
        result: list[str] = []
        catalog = None
        for key in ("voxcpm2", "voxcpm1"):
            if local_model is not None and key != local_model:
                continue
            relative = Path("audiocpp/tts") / _MODEL_CATALOG[key]["relative_path"].parent
            folder = root / "models" / relative
            if not (folder / _MODEL_CATALOG[key]["relative_path"].name).is_file() or not (specs / f"{key}.json").is_file():
                continue
            if (folder / "resource-install.json").is_file():
                installed = inspect_installed_model(root / "models", relative.as_posix(), f"tts-{key}", verify_hashes=verify_hashes)
                if installed.error:
                    continue
                from ..resource_distribution import ResourceCatalog, ResourceError
                from ..model_installation import model_update_available
                try:
                    if catalog is None:
                        catalog = ResourceCatalog(cls._catalog_program(program_root))
                    if model_update_available(catalog.get(f"tts-{key}"), installed):
                        continue
                except ResourceError:
                    continue
            else:
                # 旧迁入目录没有收据时，同样按随包清单验证，不能让损坏但仍存在的权重抢占可用后端。
                from ..resource_distribution import ResourceCatalog, ResourceError
                try:
                    if catalog is None:
                        catalog = ResourceCatalog(cls._catalog_program(program_root))
                    resource = catalog.get(f"tts-{key}")
                    valid = True
                    for item in resource.required_files:
                        path = folder / item.path
                        if not path.is_file() or path.stat().st_size != item.size_bytes:
                            valid = False
                            break
                        if not verify_hashes:
                            continue
                        with path.open("rb") as stream:
                            if hashlib.file_digest(stream, "sha256").hexdigest() != item.sha256:
                                valid = False
                                break
                    if not valid:
                        continue
                except (OSError, ResourceError):
                    continue
            result.append(key)
        return result

    def __init__(self, runtime_root: Path, config_path: Path, *,
                 local_model: str = "voxcpm1",
                 http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.config_path = Path(config_path).resolve()
        if local_model not in _MODEL_CATALOG:
            raise AudioCppTtsError("local_model（本地语音合成模型）必须为 voxcpm1 或 voxcpm2")
        self._process: subprocess.Popen | None = None
        self._log_file = None
        self._log_pump_thread: threading.Thread | None = None
        self._log_pump_done: threading.Event | None = None
        self._log_path: Path | None = None
        self._owns_process = False
        self._ready = False
        self._http_transport = http_transport
        self._management_http: httpx.AsyncClient | None = None
        self._management_transport: httpx.AsyncBaseTransport | None = http_transport
        self._lock = asyncio.Lock()
        self.local_model = local_model
        self._loaded_model: str | None = None
        self._owned_loaded_model: str | None = None  # owned_loaded_model（由此客户端加载的模型，借用模型不卸载）。
        self._models = self._read_config()
        config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        host = str(config.get("host") or "127.0.0.1")
        port = int(config.get("port") or 0)
        self.base_url = f"http://{host}:{port}"

    @property
    def model_id(self) -> str:
        """model_id（当前选中模型在 audio.cpp 服务中的标识）。"""
        return str(_MODEL_CATALOG[self.local_model]["id"])

    @property
    def native_sample_rate(self) -> int:
        """native_sample_rate（所选模型输出的原生采样率）。"""
        return int(_MODEL_CATALOG[self.local_model]["native_sample_rate"])

    @property
    def supports_reference_prompt(self) -> bool:
        """supports_reference_prompt（参考音频与转写克隆能力）：由实际运行程序声明。"""
        return bool(getattr(self, "capabilities", {}).get("reference_prompt", False))

    @property
    def supports_full_decode(self) -> bool:
        """supports_full_decode（完整解码能力）：防止旧运行程序静默忽略样音配方。"""
        return bool(getattr(self, "capabilities", {}).get("full_decode", False))

    @classmethod
    def write_runtime_config(cls, runtime_root: Path, config_path: Path) -> None:
        """写入本工作区使用的双模型惰性加载配置。"""
        root = Path(runtime_root).resolve()
        models_root = root / "models" / "audiocpp" / "tts"
        model_entries: list[dict[str, Any]] = []
        for key, info in _MODEL_CATALOG.items():
            model_path = models_root / Path(info["relative_path"])
            model_entries.append({
                "id": info["id"],
                "family": info["family"],
                "path": str(model_path.resolve()),
                "task": "tts",
                "mode": "streaming",
                "lazy": True,
                "default_request_options": {"retry_badcase": False, "num_inference_steps": 10},  # num_inference_steps（正式推理步数）。
                # mem_saver（请求后释放计算图）会增加下一句首包的重建耗时。
                # 两个 VoxCPM 模型保留单音色参考编码缓存槽，音色由每笔请求指定。
                "session_options": {
                    f"{key}.mem_saver": False,
                    f"{key}.prompt_cache_slots": 1,
                    # 默认容量按GiB预留主机提交，ggml no_alloc（不分配张量数据）上下文只需元数据。
                    # 两模型15步真实逐句对照验证128MiB容量，完整PCM逐字节一致。
                    **{f"{key}.{name}": 128 for name in _CONTEXT_KEYS},
                },
            })
        payload = {
            "host": "127.0.0.1",
            "port": 18881,
            "backend": "cuda",
            "device": 0,
            "threads": 8,
            "lazy_load": True,
            "max_loaded_models": 1,
            "ui": False,  # 原生服务实际读取的界面开关；无界面管理仍支持本地模型切换。
            "ui_management": True,
            "log_request_body": False,
            "model_spec_override": str((cls.runtime_directory(root) / "model_specs"
                                        if (cls.runtime_directory(root) / "model_specs").is_dir()
                                        else root / "sources/audio.cpp/model_specs").resolve()),
            "models": model_entries,
        }
        destination = Path(config_path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}-", suffix=".json", dir=str(destination.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def _read_config(self) -> dict[str, dict[str, Any]]:
        if not self.config_path.is_file():
            raise AudioCppTtsError(f"找不到 audio.cpp TTS 服务配置：{self.config_path}")
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AudioCppTtsError(f"audio.cpp TTS 服务配置无法读取：{exc}") from exc
        host = str(config.get("host") or "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise AudioCppTtsError("audio.cpp TTS 服务只允许绑定本机回环地址")
        port = int(config.get("port") or 0)
        if not 1 <= port <= 65535:
            raise AudioCppTtsError("audio.cpp TTS 服务端口无效")
        models = config.get("models")
        if not isinstance(models, list):
            raise AudioCppTtsError("audio.cpp TTS 配置没有模型条目")
        entries: dict[str, dict[str, Any]] = {}
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or "")
            key = next((name for name, info in _MODEL_CATALOG.items() if info["id"] == model_id), None)
            if key is None:
                continue
            model_path = Path(str(model.get("path") or "")).resolve()
            info = _MODEL_CATALOG[key]
            entries[key] = {
                "id": model_id,
                "family": str(model.get("family") or info["family"]),
                "path": model_path,
                # load_payload（完整加载参数）：动态加载会替换注册项，不能只传路径。
                # 即使旧用户配置仍留有 default_voice_preset（原生默认音色），也不让模型加载链路重新依赖它。
                "load_payload": {**{key: value for key, value in model.items()
                                     if key != "default_voice_preset"}, "path": str(model_path)},
            }
        return entries

    def model_available(self, local_model: str) -> bool:
        """只检查模型权重；音色可用性由音色库单独校验。"""
        info = _MODEL_CATALOG.get(local_model)
        if info is None:
            return False
        model_path = (self.runtime_root / "models" / "audiocpp" / "tts" / Path(info["relative_path"])).resolve()
        configured = self._models.get(local_model)
        if configured is not None:
            model_path = configured["path"]
        return model_path.is_file()

    @classmethod
    def catalog_snapshot(cls, runtime_root: Path, *, local_model: str = "voxcpm1",
                         loaded_model: str | None = None) -> dict[str, Any]:
        """返回本地目录中的模型可用清单，供服务启动前的设置页显示。"""
        root = Path(runtime_root).resolve()
        ready = set(cls.available_models(root, verify_hashes=False))
        return {
            "local_model": local_model,
            "loaded_model": loaded_model,
            "models": [
                {
                    "id": key,
                    "name": str(info["name"]),
                    "native_sample_rate": int(info["native_sample_rate"]),
                    "available": key in ready,  # available（模型与框架达到当前版本，可启动）。
                    "loaded": loaded_model == key,
                }
                for key, info in _MODEL_CATALOG.items()
            ],
        }

    def _validate_model_assets(self, local_model: str) -> dict[str, Any]:
        info = _MODEL_CATALOG.get(local_model)
        if info is None:
            raise AudioCppTtsError("local_model（本地语音合成模型）必须为 voxcpm1 或 voxcpm2")
        model_path = (self.runtime_root / "models" / "audiocpp" / "tts" / Path(info["relative_path"])).resolve()
        configured = self._models.get(local_model)
        if configured is not None:
            model_path = configured["path"]
        name = str(info["name"])
        if not model_path.is_file():
            raise AudioCppTtsError(f"{name} 本地模型文件缺失：{model_path}")
        if local_model not in self._models:
            raise AudioCppTtsError(f"{name} 未注册到 audio.cpp 服务配置，请重启应用后再切换。")
        return self._models[local_model]

    def status_snapshot(self) -> dict[str, Any]:
        ready = set(self.available_models(self.runtime_root, verify_hashes=False))
        return {
            "local_model": self.local_model,  # local_model（所选本地模型）。
            "loaded_model": self._loaded_model,  # loaded_model（当前驻留内存的模型）。
            "models": [
                {
                    "id": key,  # id（稳定的本地模型选择值）。
                    "name": str(info["name"]),  # name（界面显示名称）。
                    "native_sample_rate": int(info["native_sample_rate"]),  # native_sample_rate（原生采样率）。
                    "available": key in ready,  # available（当前模型及框架是否可启动）。
                    "loaded": self._loaded_model == key,  # loaded（当前是否已加载）。
                }
                for key, info in _MODEL_CATALOG.items()
            ],
        }

    async def refresh_status(self) -> dict[str, Any]:
        # 与 ensure_ready（确保服务就绪）和 switch_model（切换模型）共用同一互斥锁，
        # 避免并发状态请求用切换前的模型清单覆盖刚完成的加载结果。
        async with self._lock:
            if self._ready:
                with contextlib.suppress(httpx.HTTPError, OSError, ValueError):
                    entries = await self._read_model_entries()
                    # entries（服务端模型清单）以 voxcpm2-stream（服务模型 ID）为键；
                    # 状态快照保存的则是 voxcpm2（本地模型选择 ID），需从注册表反查。
                    loaded = next((
                        key for key, configured in self._models.items()
                        if entries.get(str(configured.get("id")), {}).get("loaded") is True
                    ), None)
                    self._loaded_model = loaded
            return self.status_snapshot()

    async def _management_client(self) -> httpx.AsyncClient:
        """复用管理接口连接，避免每次会话检查重新创建 HTTP（网页传输）客户端。"""
        client = self._management_http
        if client is not None and self._management_transport is not self._http_transport:
            await client.aclose()
            client = None
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=2.0, transport=self._http_transport, trust_env=False)
            self._management_http = client
            self._management_transport = self._http_transport
        return client

    async def _read_model_entries(self) -> dict[str, dict[str, Any]]:
        client = await self._management_client()
        response = await client.get(f"{self.base_url}/v1/models")
        response.raise_for_status()
        payload = response.json()
        entries = payload.get("data", []) if isinstance(payload, dict) else []
        return {
            str(entry.get("id")): entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }

    async def _service_ready(self) -> bool:
        try:
            client = await self._management_client()
            health = await client.get(f"{self.base_url}/health")
            if health.status_code != 200:
                return False
            models = await client.get(f"{self.base_url}/v1/models")
            if models.status_code != 200:
                return False
            payload = models.json()
            entries = payload.get("data", []) if isinstance(payload, dict) else []
            self.capabilities = next((entry.get("capabilities", {}) for entry in entries
                                      if isinstance(entry, dict) and entry.get("id") == self.model_id), {})
            ids = {
                str(entry.get("id"))
                for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            }
            if not set(item["id"] for item in self._models.values()).issubset(ids):
                return False
            self._loaded_model = next((
                key for key, item in self._models.items()
                if any(entry.get("id") == item["id"] and entry.get("loaded") is True for entry in entries if isinstance(entry, dict))
            ), None)
            return bool(self._models)
        except (httpx.HTTPError, OSError, ValueError):
            return False

    def _server_binary(self) -> Path:
        root = self.runtime_directory(self.runtime_root) / "cuda"
        candidates = [path for path in root.rglob("audiocpp_server.exe") if path.is_file()]
        if len(candidates) != 1:
            raise AudioCppTtsError(f"需要唯一的 CUDA audio.cpp 服务程序，找到 {len(candidates)} 个：{root}")
        return candidates[0].resolve()

    def _start_process(self, *, log_path: Path | None = None, trace_path: Path | None = None) -> None:
        if error := self._runtime_update_error(self.runtime_root, verify_hashes=False):
            logger.error("audio_cpp_tts.runtime_update_required（原生组件需要更新） %s", error)
            raise AudioCppTtsError(error)
        binary = self._server_binary()
        source_root = self.runtime_directory(self.runtime_root)
        if not (source_root / "model_specs").is_dir():
            source_root = self.runtime_root / "sources" / "audio.cpp"
        if not source_root.is_dir():
            raise AudioCppTtsError(f"找不到 audio.cpp 源码运行目录：{source_root}")
        self._validate_model_assets(self.local_model)

        # log_path（日志路径）允许隔离性能测量日志，正式服务仍采用默认位置。
        log_path = log_path or self.runtime_root / "cache" / "logs" / "audio-cpp-tts.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if trace_path is None and _native_trace_enabled():
            # 原生分阶段计时：仅在显式开启时写入独立文件，避免污染正式 stdout 日志。
            trace_path = log_path.with_name("audiocpp-trace.log")
        rotate_oversized_log(log_path, max_bytes=_NATIVE_LOG_LIMIT, backups=1)
        if trace_path is not None:
            rotate_oversized_log(trace_path, max_bytes=_NATIVE_LOG_LIMIT, backups=1)
        if log_path.parent == self.runtime_root / "cache" / "logs":
            # 隔离测量目录由调用方管理，不纳入产品日志预算。
            enforce_log_budget(log_path.parent)
        environment = os.environ.copy()
        torch_lib = self.runtime_root / "runtimes" / "python" / "Lib" / "site-packages" / "torch" / "lib"
        library_paths = [str(binary.parent)]
        if torch_lib.is_dir():
            library_paths.append(str(torch_lib))
        if environment.get("PATH"):
            library_paths.append(environment["PATH"])
        environment["PATH"] = os.pathsep.join(library_paths)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        command = [str(binary), "--config", str(self.config_path)]
        if trace_path is not None:
            command += ["--log", "--log-file", str(trace_path)]  # 原生阶段计时诊断，不默认启用。
        pump_stdout = _native_log_pump_enabled()
        self._log_file = log_path.open("ab", buffering=0)
        self._log_path = log_path
        # 本进程持有该句柄；显式登记后，预算剔除不会再尝试删除它（Windows 上必然失败）。
        protect_log_files(log_path)
        self._process = subprocess.Popen(
            command,
            cwd=source_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if pump_stdout else self._log_file,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        self._owns_process = True
        if pump_stdout and self._process.stdout is not None:
            self._start_log_pump(self._process.stdout)
        logger.info(
            "audio_cpp_tts.server_started pid=%s local_model=%s config=%s log=%s trace=%s",
            self._process.pid, self.local_model, self.config_path, log_path, trace_path,
        )
        try:
            with binary.open("rb") as handle:
                binary_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
            receipt_path = binary.parent.parent / "resource-install.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig")) if receipt_path.is_file() else {}
            receipt_digest = next((row.get("sha256") for row in receipt.get("files", [])
                                   if row.get("path", "").replace("\\", "/") == "cuda/audiocpp_server.exe"), None)
            release_path = self._catalog_program() / "release.json"
            release = json.loads(release_path.read_text(encoding="utf-8-sig")) if release_path.is_file() else {}
            from ..resource_distribution import ResourceCatalog
            target = ResourceCatalog(self._catalog_program()).specs.get("tts-audiocpp-runtime")
            target_digest = next((item.sha256 for item in target.files if item.path == "cuda/audiocpp_server.exe"), None) if target else None
            logger.info(
                "audio_cpp_tts.runtime_identity pid=%s binary_path（实际程序路径）=%s "
                "binary_sha256（程序完整摘要）=%s installed_version（安装版本）=%s "
                "receipt_path（安装收据）=%s binary_matches_receipt（程序与收据一致）=%s "
                "app_build（程序构建）=%s target_version（目标原生版本）=%s "
                "binary_matches_target（程序与当前目标一致）=%s native_trace（原生追踪）=%s",
                self._process.pid, binary, binary_sha256, receipt.get("version"), receipt_path,
                binary_sha256 == receipt_digest if receipt_digest else None, release.get("build"),
                target.version if target else None, binary_sha256 == target_digest if target_digest else None, trace_path,
            )
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            selected = next((item for item in config.get("models", []) if item.get("id") == self.model_id), {})
            options = selected.get("session_options", {})
            logger.info(
                "audio_cpp_tts.runtime_config pid=%s model=%s backend（计算后端）=%s device（设备编号）=%s "
                "threads（推理线程数）=%s session_options（会话计算图及缓存参数）=%s",
                self._process.pid, self.model_id, config.get("backend"), config.get("device"), config.get("threads"),
                {key: value for key, value in options.items()
                 if key.endswith((".mem_saver", ".prompt_cache_slots", *tuple("." + name for name in _CONTEXT_KEYS)))},
            )
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
            logger.warning("audio_cpp_tts.runtime_identity_unavailable pid=%s error_type（错误类型）=%s",
                           self._process.pid, type(exc).__name__)

    def _start_log_pump(self, stream) -> None:
        """把原生 stdout 逐行加时间戳写入日志；读取永不中断，避免子进程管道写满阻塞。"""

        target = self._log_file
        finished = threading.Event()

        def pump() -> None:
            pending = b""
            read = getattr(stream, "read1", stream.read)  # read1（读取可用数据）不等满4096字节。
            try:
                while True:
                    chunk = read(4096)
                    if not chunk:
                        break
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        _write_native_line(target, line)
                if pending:
                    _write_native_line(target, pending)
            except Exception:
                logger.warning("audio_cpp_tts.native_log_pump_failed action=drain_only", exc_info=True)
                # 降级后继续排空管道：绝不因为日志线程故障让原生进程写阻塞。
                with contextlib.suppress(Exception):
                    while read(65536):
                        pass
            finally:
                with contextlib.suppress(Exception):
                    stream.close()
                finished.set()

        thread = threading.Thread(target=pump, name="audio-cpp-tts-log", daemon=True)
        self._log_pump_thread = thread
        self._log_pump_done = finished
        thread.start()

    async def _ensure_ready_locked(self, *, timeout_seconds: float) -> None:
        self._validate_model_assets(self.local_model)
        if self._ready and await self._service_ready():
            entries = await self._read_model_entries()
            if entries.get(self.model_id, {}).get("loaded") is True:
                return
            await self._load_model_locked(self.local_model)
            return
        if await self._service_ready():
            self._ready = True
            logger.info("audio_cpp_tts.server_reused model=%s", self.model_id)
            logger.info("audio_cpp_tts.runtime_identity_unavailable model=%s reason（原因）=existing_service_not_spawned（复用服务非本次启动）",
                        self.model_id)
            await self._load_model_locked(self.local_model)
            return
        if self._process is None:
            self._start_process()

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            process = self._process
            if process is not None and process.poll() is not None:
                raise AudioCppTtsError(
                    f"audio.cpp 服务提前退出，exit_code={process.returncode}；日志："
                    f"{self.runtime_root / 'cache' / 'logs' / 'audio-cpp-tts.log'}"
                )
            if await self._service_ready():
                self._ready = True
                await self._load_model_locked(self.local_model)
                logger.info("audio_cpp_tts.server_ready model=%s", self.model_id)
                return
            await asyncio.sleep(0.25)
        raise TimeoutError(f"audio.cpp 服务未在 {timeout_seconds:.0f} 秒内就绪：{self.base_url}")

    async def ensure_ready(self, *, timeout_seconds: float = 180.0) -> None:
        async with self._lock:
            await self._ensure_ready_locked(timeout_seconds=timeout_seconds)

    async def _load_model_locked(self, local_model: str) -> None:
        configured = self._validate_model_assets(local_model)
        entries = await self._read_model_entries()
        model_id = str(configured["id"])
        if model_id not in entries:
            raise AudioCppTtsError(f"{_MODEL_CATALOG[local_model]['name']} 未注册在正在运行的 audio.cpp 服务中。")
        if entries[model_id].get("loaded") is True:
            self._loaded_model = local_model
            return
        prior = next((
            (model_id, entry) for model_id, entry in entries.items()
            if entry.get("loaded") is True
            and any(configured_model["id"] == model_id for configured_model in self._models.values())
        ), None)
        if prior is not None:
            owned = getattr(self, "_owned_loaded_model", None)
            if not self._owns_process and (owned is None or _MODEL_CATALOG[owned]["id"] != prior[0]):
                raise AudioCppTtsError("外部语音服务已有其他客户端加载的模型，不能替换；请先释放该服务。")
            await self._unload_model_locked(prior[0])
        body = {
            **configured["load_payload"],
            "id": configured["id"],
            "path": str(configured["path"]),
            "family": configured["family"],
            "task": "tts",
            "mode": "streaming",
        }
        started = time.perf_counter()
        try:
            # load intent（加载所有权意图）：请求已送出但确认失败时，回滚仍需释放本次模型。
            self._owned_loaded_model = local_model
            await self._post_model_action("/v1/models/load", body, timeout_seconds=300.0)
            loaded_entries = await self._read_model_entries()
            if loaded_entries.get(model_id, {}).get("loaded") is not True:
                raise AudioCppTtsError(f"{_MODEL_CATALOG[local_model]['name']} 加载请求完成，但服务未确认模型已驻留。")
        except BaseException as exc:
            logger.warning(
                "audio_cpp_tts.model_load_failed model_id=%s local_model=%s error_type=%s error=%s elapsed_ms=%.1f",
                model_id, local_model, type(exc).__name__, str(exc)[:300],
                (time.perf_counter() - started) * 1000,
            )
            raise
        self._loaded_model = local_model
        self._owned_loaded_model = local_model
        logger.info("audio_cpp_tts.model_loaded model_id=%s local_model=%s elapsed_ms=%.1f",
                    model_id, local_model, (time.perf_counter() - started) * 1000)

    async def _unload_model_locked(self, model_id: str) -> None:
        started = time.perf_counter()
        try:
            await self._post_model_action("/v1/models/unload", {"id": model_id})
            entries = await self._read_model_entries()
            if entries.get(model_id, {}).get("loaded") is True:
                raise AudioCppTtsError(f"audio.cpp 服务未确认旧模型已卸载：{model_id}")
        except BaseException as exc:
            logger.warning(
                "audio_cpp_tts.model_unload_failed model_id=%s error_type=%s error=%s elapsed_ms=%.1f",
                model_id, type(exc).__name__, str(exc)[:300], (time.perf_counter() - started) * 1000,
            )
            raise
        self._loaded_model = None
        self._owned_loaded_model = None
        logger.info("audio_cpp_tts.model_unloaded model_id=%s elapsed_ms=%.1f",
                    model_id, (time.perf_counter() - started) * 1000)

    async def _post_model_action(self, path: str, payload: dict[str, Any], *,
                                 timeout_seconds: float = 10.0) -> None:
        client = await self._management_client()
        response = await client.post(
            f"{self.base_url}{path}", json=payload,
            timeout=httpx.Timeout(timeout_seconds, connect=2.0),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:400]
            raise AudioCppTtsError(f"audio.cpp 模型操作失败（HTTP {response.status_code}）：{detail}") from exc

    async def switch_model(self, local_model: str) -> None:
        """卸载旧模型、加载并验证新模型，失败时恢复原模型。"""
        if local_model not in _MODEL_CATALOG:
            raise AudioCppTtsError("local_model（本地语音合成模型）必须为 voxcpm1 或 voxcpm2")
        async with self._lock:
            if local_model == self.local_model and self._loaded_model == local_model:
                return
            self._validate_model_assets(local_model)
            service_ready = self._ready and await self._service_ready()
            if not service_ready:
                previous = self.local_model
                self.local_model = local_model
                try:
                    await self._ensure_ready_locked(timeout_seconds=180.0)
                except Exception:
                    self.local_model = previous
                    raise
                self.local_model = local_model
                self._loaded_model = local_model
                self._ready = True
                return
            previous = self.local_model
            previous_id = str(_MODEL_CATALOG[previous]["id"])
            entries = await self._read_model_entries()
            if (entries.get(previous_id, {}).get("loaded") is True
                    and not self._owns_process
                    and getattr(self, "_owned_loaded_model", None) != previous):
                raise AudioCppTtsError("当前模型由外部客户端加载，不能替换；请先释放该服务。")
            try:
                if entries.get(previous_id, {}).get("loaded") is True:
                    await self._unload_model_locked(previous_id)
                await self._load_model_locked(local_model)
            except Exception as exc:
                try:
                    entries = await self._read_model_entries()
                    target_id = str(_MODEL_CATALOG[local_model]["id"])
                    if (entries.get(target_id, {}).get("loaded") is True
                            and (self._owns_process or getattr(self, "_owned_loaded_model", None) == local_model)):
                        await self._unload_model_locked(target_id)
                    await self._load_model_locked(previous)
                except Exception as restore_exc:
                    self._loaded_model = None
                    self._ready = False
                    raise AudioCppTtsError(
                        f"切换到 {_MODEL_CATALOG[local_model]['name']} 失败，恢复原模型也失败：{restore_exc}"
                    ) from exc
                raise AudioCppTtsError(
                    f"切换到 {_MODEL_CATALOG[local_model]['name']} 失败，已恢复 {_MODEL_CATALOG[previous]['name']}：{exc}"
                ) from exc
            self.local_model = local_model
            self._loaded_model = local_model
            self._ready = True
            logger.info("audio_cpp_tts.model_switched local_model=%s", local_model)

    async def close(self) -> None:
        async with self._lock:
            process = self._process
            self._ready = False
            # 外部服务只释放本客户端加载的模型，不终止不属于本程序的进程。
            if not self._owns_process and getattr(self, "_owned_loaded_model", None) is not None:
                with contextlib.suppress(Exception):
                    await self._unload_model_locked(str(_MODEL_CATALOG[self._owned_loaded_model]["id"]))
            self._loaded_model = None
            self._owned_loaded_model = None
            management, self._management_http = self._management_http, None
            if management is not None:
                await management.aclose()
            if process is not None and self._owns_process and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
                logger.info("audio_cpp_tts.server_stopped pid=%s", process.pid)
            self._process = None
            self._owns_process = False
            thread, self._log_pump_thread = self._log_pump_thread, None
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)
                if thread.is_alive():
                    logger.warning("audio_cpp_tts.native_log_pump_timeout pid=%s",
                                   process.pid if process is not None else None)
            self._log_pump_done = None
            # 关闭路径必须容错：对象可能未完整初始化就已进入收尾。
            log_path, self._log_path = getattr(self, "_log_path", None), None
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None
            if log_path is not None:
                unprotect_log_files(log_path)


class AudioCppTtsClient:
    """把 VoxCPM SSE（服务器推送事件流）映射为现有 TTS 会话接口。"""

    transport = "http_sse"
    scheduling = "sentence_window"
    effective_scheduling = "sentence_window"
    sentence_min_effective_chars = 10
    sentence_prefetch_ms = 3000
    sentence_tick_ms = 50
    first_clause_timeout_ms = 400
    supports_prewarm = True
    playback_startup_buffer_ms = 0  # playback_startup_buffer_ms（起播储备）：首块音频立即交给设备回调。
    playback_resume_buffer_ms = 400  # playback_resume_buffer_ms（欠载后连续音频储备）。

    def __init__(self, server: AudioCppTtsServer, *, timeout_seconds: float = 300.0,
                 sample_rate: int | None = None, sentence_min_effective_chars: int = 10,
                 sentence_prefetch_ms: int = 3000, sentence_tick_ms: int = 50,
                 first_clause_timeout_ms: int = 400,
                http_transport: httpx.AsyncBaseTransport | None = None,
                local_optimization: bool = True, acoustic_continuation: bool = True,
                gpu_max_performance: bool = True,
                voice_library: VoiceLibrary | None = None,
                active_voice_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
                emotion_mode: str = "off",
                inference_steps_by_model: Mapping[str, int] | None = None) -> None:
        self.server = server
        self.base_url = server.base_url
        self.timeout_seconds = timeout_seconds
        # sample_rate（播放器采样率）沿用当前输出设备时钟，模式热切换不重载输入模型。
        # 未显式指定时取所选模型的原生率：VoxCPM2 原生 48 kHz 降到 16 kHz 会丢掉
        # 8 kHz 以上全部带宽（实测占 1.1%–14.4%），默认值不能是 16 kHz。
        self._sample_rate_locked = sample_rate is not None
        self.sample_rate = int(sample_rate) if sample_rate else int(
            getattr(server, "native_sample_rate", 16000))
        self.sentence_min_effective_chars = int(sentence_min_effective_chars)
        self.sentence_prefetch_ms = int(sentence_prefetch_ms)
        self.sentence_tick_ms = int(sentence_tick_ms)
        self.first_clause_timeout_ms = int(first_clause_timeout_ms)
        self._http_transport = http_transport
        self._http_client: httpx.AsyncClient | None = None
        self._request_lock = asyncio.Lock()
        self._prewarm_lock = asyncio.Lock()
        self._warmed = False
        self.local_optimization = local_optimization
        # acoustic_continuation（跨段声学续接）经长文本实测产生不可接受的时延和 RTF；
        # 保留构造参数兼容旧配置，但当前本地 VoxCPM 请求固定不使用该能力。
        self.acoustic_continuation = False
        if acoustic_continuation:
            logger.info(
                "audio_cpp_tts.continuation_disabled（声学续接已固定关闭） configured=%s effective=%s",
                acoustic_continuation, self.acoustic_continuation,
            )
        # gpu_max_performance（本地合成期间请求显卡优先最大性能）：免管理员权限、按进程生效，
        # 进程退出即自动还原；不可用时全部降级为无操作，不影响合成。
        self.gpu_performance = GpuPerformanceMode(enabled=gpu_max_performance)
        self.voice_library = voice_library
        self.active_voice_provider = active_voice_provider
        self.emotion_mode = "off"  # LLM（大语言模型）不再控制播报情绪；旧构造参数保留兼容。
        from ..config import validate_tts_inference_steps
        self.inference_steps_by_model = validate_tts_inference_steps(inference_steps_by_model or {})
        self._active_emotion = "calm"  # _active_emotion（实际提交的情绪请求）。
        self._native_continuation = False
        self._last_success_at = 0.0  # _last_success_at（最近推理成功时刻）。
        self._warm_task = None  # _warm_task（可被真实生成抢占的预热任务）。
        self._real_waiters = 0  # _real_waiters（等待或正在生成的真实请求数）。
        self.last_prewarm_metrics: list[dict[str, float | int | None]] = []

    @property
    def capabilities(self) -> TtsCapabilities:
        return TtsCapabilities(
            acoustic_continuation=self.acoustic_continuation and self._native_continuation,
            idle_prewarm=self.local_optimization, cancellation=True,
        )

    @property
    def model_id(self) -> str:
        """model_id（当前模型在 audio.cpp 服务中的稳定标识）。"""
        return self.server.model_id

    @property
    def local_model(self) -> str:
        """local_model（当前选中的 VoxCPM 本地模型）。"""
        return self.server.local_model

    @property
    def native_sample_rate(self) -> int:
        """native_sample_rate（本地模型的原生输出采样率）。"""
        return int(getattr(self.server, "native_sample_rate", self.sample_rate))

    def model_status(self) -> dict[str, Any]:
        status = self.server.status_snapshot()
        # gpu_performance（显卡性能模式请求状态）：只用于诊断展示，不参与合成决策。
        status["gpu_performance"] = self.gpu_performance.status()
        return status

    async def switch_model(self, local_model: str) -> None:
        """切换本地模型并同步下行率。

        VoxCPM1 与 VoxCPM2 的原生率不同（16 kHz / 48 kHz），下行率必须跟着模型走。
        此前切换只改服务端模型，客户端仍按旧率重采样——VoxCPM2 会被一直砍到 8 kHz
        带宽，且播放器时钟与真实 PCM 不一致。调用方显式锁定的下行率优先保留。
        """
        previous = self.sample_rate
        await self.server.switch_model(local_model)
        self._warmed = False
        if self._sample_rate_locked:
            return
        desired = self.native_sample_rate
        if desired != previous:
            self.sample_rate = desired
            logger.info("audio_cpp_tts.client_rate_followed_model local_model=%s previous_rate=%d sample_rate=%d",
                        local_model, previous, desired)

    @staticmethod
    def normalize_emotion(emotion: str | None) -> str:
        return emotion if emotion in {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"} else "calm"

    async def prepare_emotion(self, emotion: str | None, *, detach_active: bool = False) -> str:
        """prepare_emotion（旧情绪兼容接口）：正式播报固定沿用参考语气。"""
        del detach_active
        self._active_emotion = "calm"
        return self._active_emotion

    @property
    def emotion_status(self) -> str:
        """emotion_status（情绪控制状态）：模型提示提交不代表听感已验收。"""
        return "disabled"

    @property
    def emotion_transport(self) -> str:
        """emotion_transport（情绪控制途径），VoxCPM1 沿用参考音频。"""
        return "reference_audio"

    @property
    def applied_emotion(self) -> str:
        """applied_emotion（实际请求的情绪标签）。"""
        return self._active_emotion

    def resolve_voice(self, emotion: str | None, *, preview: bool = False) -> ResolvedLocalTtsVoice | None:
        if self.voice_library is None or self.active_voice_provider is None:
            return None
        selection = self.active_voice_provider(self.local_model)
        if selection is None:
            return None
        return self.voice_library.resolve(
            self.local_model, selection,
            emotion=None,
            is_preview=preview,
        )

    def session(self, *, fresh: bool = False, turn_id: str | None = None,
                speech_id: str | None = None,
                emotion: str | None = None, voice_snapshot: ResolvedLocalTtsVoice | None = None,
                preview: bool = False, inference_steps: int | None = None) -> "AudioCppTtsSession":
        del fresh
        if inference_steps is not None and (type(inference_steps) is not int or not 1 <= inference_steps <= 30):
            raise AudioCppTtsError("inference_steps（固定推理步数）必须为 1–30 的整数。")
        owner = self.voice_library
        if callable(getattr(owner, "for_model", None)):
            owner = owner.for_model(voice_snapshot.model if voice_snapshot else self.local_model)
        guard = owner.snapshot_guard() if callable(getattr(owner, "snapshot_guard", None)) else contextlib.nullcontext()
        with guard:
            session = AudioCppTtsSession(
                self,
                voice_snapshot=voice_snapshot or self.resolve_voice(emotion, preview=preview),
                turn_id=turn_id,
                speech_id=speech_id,
            )
            if inference_steps is not None:
                session._inference_steps = inference_steps
            return session

    async def ensure_ready(self) -> None:
        await self.server.ensure_ready()
        native = getattr(self.server, "capabilities", {})
        native_continuation = bool(native.get("acoustic_continuation", False))
        # acoustic_continuation（声学续接能力）在当前原生服务中首次推理后才更新；
        # 模型加载时用稳定的同版能力标记补足首次播报前的协商。
        # reference_prompt（参考提示能力）与 full_decode（完整解码能力）由包含
        # VoxCPM2 续接存储及释放接口的运行组件同时声明。
        if (not native_continuation and self.local_model == "voxcpm2"
                and native.get("reference_prompt") is True
                and native.get("full_decode") is True):
            native_continuation = True
            if not self._native_continuation:
                logger.info(
                    "audio_cpp_tts.continuation_capability_inferred（续接能力推断） "
                    "model=%s reference_prompt（参考提示能力）=%s full_decode（完整解码能力）=%s",
                    self.local_model, native.get("reference_prompt"), native.get("full_decode"),
                )
        self._native_continuation = native_continuation
        # 本地服务就绪后请求显卡优先最大性能；失败只记录，不阻断合成。
        self.gpu_performance.apply()

    def _request_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                transport=self._http_transport,
                trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )
        return self._http_client

    async def prewarm(self, *, turn_id: str | None = None,
                      voice_snapshot: ResolvedLocalTtsVoice | None = None) -> dict[str, Any]:
        await self.ensure_ready()
        async with self._prewarm_lock:
            if self._real_waiters:
                return {"status": "READY", "reason": "real_request_priority"}
            if self._warmed and (not self.local_optimization or time.monotonic() - self._last_success_at < 60):
                return {"status": "READY", "reason": "already_warmed"}
            session = self.session(emotion="calm", voice_snapshot=voice_snapshot, turn_id=turn_id)
            if session.voice_snapshot is None:
                raise AudioCppTtsError("尚未配置可用音色，无法执行预热。")
            session._is_prewarm = True
            session._request_options = {"max_tokens": 8}
            await session.start()
            self._warm_task = asyncio.current_task()
            audio_bytes = 0

            async def discard_audio() -> None:
                nonlocal audio_bytes
                async for pcm in session.audio_chunks():
                    audio_bytes += len(pcm)

            reader = asyncio.create_task(discard_audio(), name="audio-cpp-tts-prewarm-reader")
            try:
                await session.send_text(_PREWARM_TEXT)
                await session.finish()
                await reader
                if audio_bytes <= 0:
                    raise AudioCppTtsError("VoxCPM 预热请求没有返回 PCM 音频")
                self._warmed = True
                self.last_prewarm_metrics = list(session.request_metrics)
                logger.info("audio_cpp_tts.prewarm_ready turn=%s speech=None session=%s audio_bytes=%d",
                            turn_id, session.session_tag, audio_bytes)
                return {"status": "READY", "audio_bytes": audio_bytes}
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    logger.warning(
                        "audio_cpp_tts.prewarm_failed turn=%s session=%s error_type=%s error=%s",
                        turn_id, session.session_tag, type(exc).__name__, str(exc)[:300],
                    )
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await session.cancel()
                raise
            finally:
                self._warm_task = None
                await session.close()

    async def close(self) -> None:
        self._warmed = False
        # 还原显卡性能模式；进程退出时驱动本来也会自动还原，这里显式收尾便于在线切换。
        self.gpu_performance.restore()
        if self._warm_task is not None and self._warm_task is not asyncio.current_task():
            self._warm_task.cancel()
            await asyncio.gather(self._warm_task, return_exceptions=True)
        http, self._http_client = self._http_client, None
        if http is not None and not http.is_closed:
            await http.aclose()
        await self.server.close()


class AudioCppTtsSession:
    """单个语音事务的本地 SSE（服务器推送事件流）会话。"""

    transport = "http_sse"
    scheduling = "sentence_window"
    effective_scheduling = "sentence_window"
    bidi = False
    reusable = False

    def __init__(self, client: AudioCppTtsClient,
                 voice_snapshot: ResolvedLocalTtsVoice | None = None, *,
                 turn_id: str | None = None, speech_id: str | None = None) -> None:
        self.client = client
        self.voice_snapshot = voice_snapshot
        # turn_id / speech_id（轮次与语音事务标识）：只用于日志关联，不参与合成决策。
        self.turn_id = turn_id
        self.speech_id = speech_id
        self.model_id = voice_snapshot.native_model_id if voice_snapshot else client.model_id
        self._audio: asyncio.Queue[bytes | BaseException | object] = asyncio.Queue()
        self._started = False
        self._finished = False
        self._cancelled = False
        self._active_response: httpx.Response | None = None
        self._send_task: asyncio.Task | None = None
        self._continuation_id = uuid4().hex  # _continuation_id（唯一事务声学上下文）。
        self._continuation_enabled = False  # _continuation_enabled（本事务实际使用声学续接）。
        self._continuation_reanchor_enabled = False  # 是否对设计音色启用句末重锚定。
        self._continuation_group_start_segment = 0  # 当前续接组开始时已完成的句段数。
        self._continuation_reanchor_count = 0  # 本事务已经完成的句末重锚定次数。
        self._last_completed_text: str | None = None  # 最近一个成功合成句段的原文。
        self._context_requested = False  # _context_requested（是否向原生申请过上下文）。
        self._native_request_submitted = False  # 原生请求已进入网络发送路径。
        self.context_release_confirmed = None  # 原生执行锁取得且上下文释放的明确确认。
        self._context_release_deadline: float | None = None  # 释放截止时间跨重复关闭保留。
        self._is_prewarm = False
        self._is_preview = bool(voice_snapshot and voice_snapshot.is_preview)
        self._request_options = {}
        model = voice_snapshot.model if voice_snapshot else client.local_model
        self._inference_steps = client.inference_steps_by_model[model]  # 每回答固定步数；热更新作用于下一事务。
        self._request_had_audio = False
        self._allow_retry = False
        self._buffered_bytes = 0  # _buffered_bytes（尚未由播放器读取的音频字节）。
        self.first_audio_callback = None  # first_audio_callback（合成首音就绪通知钩子）。
        self.first_audio_ready_at: float | None = None  # 首音就绪的单调时钟，关联播放许可等待。
        self.audio_rejected_callback = None  # audio_rejected_callback（异常音频立即停播钩子）。
        self.audio_rejected = False  # audio_rejected（本事务已经判废，不再接收后续正文）。
        self.request_metrics: list[dict[str, float | int | str | bool | None]] = []
        self.received_bytes = 0
        self.completed_segments = 0
        self.completed_sentences = 0
        self.generation_finished = False
        self._audio_iterator_started = False
        self._release_voice = client.voice_library.pin(voice_snapshot) if voice_snapshot and hasattr(client.voice_library, "pin") else None
        self._segment_available = asyncio.Event()
        self._segment_receipts: list[TtsSegmentReceipt] = []
        self._error: Exception | None = None
        # 重采样器在一个文本请求内跨 SSE 分块保存相位与尾部采样。
        self._resampler = (_Pcm16Resampler(self.client.native_sample_rate, self.client.sample_rate)
                           if self.client.native_sample_rate != self.client.sample_rate else None)

    async def start(self) -> None:
        await self.client.ensure_ready()
        # 官方可控克隆使用 reference-only（仅参考音色）模式。
        # continuation（前文续接）会拼接 prompt_text（前文转写），
        # 每句的括号控制词由此落入正文中间，可能被朗读。
        controlled = bool(self._control_instructions())
        # 声学续接及其 continuation_id（续接状态标识）已按性能实验固定关闭。
        # controlled（带控制词）和预热/样音请求同样不创建跨句段声学状态。
        self._continuation_enabled = False
        voice = self.voice_snapshot
        self._continuation_reanchor_enabled = bool(
            self._continuation_enabled
            and voice is not None
            and voice.model == "voxcpm2"
            and voice.reference_mode == "voice"
            and getattr(voice, "voice_source", "legacy") == "design"
        )
        logger.info(
            "audio_cpp_tts.session_policy turn=%s speech=%s session=%s model_id=%s voice_id=%s "
            "voice_source（音色来源）=%s "
            "voice_revision=%s continuation_enabled=%s control_prefix=%s prewarm=%s preview=%s "
            "continuation_reanchor_enabled（设计音色句末重锚定）=%s "
            "continuation_reanchor_min_segments（重锚定最少句段数）=%d "
            "inference_steps=%s sample_rate=%d native_sample_rate=%d",
            self.turn_id, self.speech_id, self.session_tag, self.model_id,
            voice.voice_id if voice else None,
            getattr(voice, "voice_source", "legacy") if voice else None,
            voice.revision if voice else None,
            self._continuation_enabled, controlled, self._is_prewarm, self._is_preview,
            self._continuation_reanchor_enabled, _CONTINUATION_REANCHOR_MIN_SEGMENTS,
            self._inference_steps, self.client.sample_rate, self.client.native_sample_rate,
        )
        self._started = True

    @property
    def session_tag(self) -> str:
        """session_tag（会话标识）：同一语音事务内多段合成共用，便于日志串联。"""
        return self._continuation_id[:8]

    def _tts_fields(self) -> tuple:
        return self.turn_id, self.speech_id, self.session_tag

    def _reject(self, stage: str, reason: str) -> AudioCppTtsError:
        """前置校验失败：先落 WARNING 再抛，避免只抛异常、日志无痕。"""
        logger.warning(
            "audio_cpp_tts.request_rejected turn=%s speech=%s session=%s stage=%s reason=%s",
            *self._tts_fields(), stage, reason,
        )
        return AudioCppTtsError(reason)

    async def send_text(self, text: str) -> None:
        if self._is_prewarm:
            return await self._send_with_fallback(text)
        self.client._real_waiters += 1
        completed_before = self.completed_segments
        try:
            await self._maybe_reanchor_before_text(text)
            await self._send_with_fallback(text)
            if self.completed_segments > completed_before and text.strip():
                self._last_completed_text = text
        finally:
            self.client._real_waiters -= 1

    async def _maybe_reanchor_before_text(self, text: str) -> None:
        """在合格句末释放续接上下文，再从同一固定音色参考开始新组。"""
        previous_text = self._last_completed_text
        if (
            not self._continuation_reanchor_enabled
            or not text.strip()
            or not previous_text
            or not previous_text.rstrip().endswith(_SENTENCE_END_PUNCTUATION)
        ):
            return

        group_segments = self.completed_segments - self._continuation_group_start_segment
        if group_segments < _CONTINUATION_REANCHOR_MIN_SEGMENTS:
            return

        previous_id = self._continuation_id
        await self._release_context()
        if self.context_release_confirmed is not True:
            # 释放未确认时继续沿用当前上下文，避免静默制造未受控的新上下文。
            logger.warning(
                "audio_cpp_tts.continuation_reanchor_skipped turn=%s speech=%s session=%s "
                "after_segments（已完成句段数）=%d group_segments（本组句段数）=%d "
                "reason（原因）=context_release_unconfirmed（上下文未确认释放）",
                self.turn_id, self.speech_id, self.session_tag,
                self.completed_segments, group_segments,
            )
            return
        if self._cancelled or self._finished:
            return

        next_id = uuid4().hex
        self._continuation_id = next_id
        self._continuation_group_start_segment = self.completed_segments
        self._continuation_reanchor_count += 1
        logger.info(
            "audio_cpp_tts.continuation_reanchored turn=%s speech=%s "
            "previous_session（旧续接组）=%s next_session（新续接组）=%s "
            "reanchor_count（重锚定次数）=%d after_segments（已完成句段数）=%d "
            "group_segments（本组句段数）=%d boundary（重锚定边界）=sentence_end（完整句末） "
            "voice_id（音色编号）=%s",
            self.turn_id, self.speech_id, previous_id[:8], next_id[:8],
            self._continuation_reanchor_count, self.completed_segments, group_segments,
            self.voice_snapshot.voice_id if self.voice_snapshot else None,
        )

    async def _send_with_fallback(self, text: str) -> None:
        if self._error is not None:
            raise self._error
        warm = self.client._warm_task
        if not self._is_prewarm and warm is not None and warm is not asyncio.current_task():
            cancel_started = time.perf_counter()
            warm.cancel()
            await asyncio.gather(warm, return_exceptions=True)
            logger.info("audio_cpp_tts.prewarm_cancel_wait turn=%s speech=%s session=%s elapsed_ms（预热取消等待毫秒）=%.1f",
                        *self._tts_fields(), (time.perf_counter() - cancel_started) * 1000)
        self._allow_retry = self._continuation_enabled
        try:
            await self._send_text_once(text)
        except Exception as exc:
            if isinstance(exc, AudioCppDegenerateError):
                await self._release_context()
                raise
            if not self._allow_retry or self._request_had_audio:
                raise
            logger.warning(
                "audio_cpp_tts.continuation_fallback turn=%s speech=%s session=%s segment=%d reason=%s",
                *self._tts_fields(), self.completed_segments, str(exc)[:180],
            )
            await self._release_context()
            self._allow_retry = False
            # 再建立新上下文；缺失前文时原生使用正式参考独立合成。
            await self._send_text_once(text)

    async def _send_text_once(self, text: str) -> None:
        if not self._started:
            raise self._reject("state", "VoxCPM 会话尚未启动")
        if self._finished or self._cancelled:
            raise self._reject("state", "VoxCPM 会话已结束")
        if not text.strip():
            return
        self._send_task = asyncio.current_task()
        request_started_at = time.perf_counter()
        request_sent_at = request_started_at
        first_audio_at: float | None = None
        headers_at: float | None = None
        last_audio_at: float | None = None
        chunk_count = 0
        max_chunk_gap_ms = 0.0
        native_timing: dict[str, float] = {}  # 原生完成事件中的有限数值计时。
        body = {
            "model": self.model_id,
            "input": self._request_text(text),
            "response_format": "pcm",
            "stream_format": "sse",
            "options": {"retry_badcase": False, **self._request_options},
        }
        body["options"]["num_inference_steps"] = self._inference_steps
        budget_ms = _generation_budget_ms(text)
        # max_tokens（原生生成预算）先限制计算量；时长判废独立按实际 PCM 检查，
        # 不假设不同代模型的令牌一定对应同样的音频时长。
        body["options"].setdefault("max_tokens", min(
            4096 if self.client.local_model == "voxcpm2" else 1024,
            max(32, sum(char.isalnum() for char in text) * 16 + 32),
        ))
        voice = self.voice_snapshot
        if voice is None:
            raise self._reject("voice", "尚未配置可用音色，请先导入或创建音色并启用。")
        if voice.model == "voxcpm1":
            if not voice.reference_path or not voice.reference_text.strip():
                raise self._reject("voice", "VoxCPM1（第一代本地语音模型）需要参考音频和对应转写文本。")
            body["voice_ref"] = str(voice.reference_path.resolve())
            body["reference_text"] = voice.reference_text
        elif voice.reference_mode == "voice":
            if not voice.reference_path:
                raise self._reject("voice", "VoxCPM2（第二代本地语音模型）参考音频不可用。")
            body["voice_ref"] = str(voice.reference_path.resolve())
            if not self._is_preview and not voice.reference_text.strip():
                raise self._reject("voice", "请先校对并保存参考音频的准确转写，再使用高保真克隆播报。")
            # voice_ref（音色克隆参考）和 reference_text（参考音频转写）保留；
            # prompt_audio（续接提示音频）实测造成不可接受的时延和 RTF，因此不提交。
            if voice.reference_text.strip():
                body["reference_text"] = voice.reference_text
                if not self._is_preview:
                    if not getattr(self.client.server, "supports_reference_prompt", True):
                        raise self._reject("runtime", "本地语音运行程序尚不支持高保真流式克隆，请更新本地语音运行组件后重试。")
        self._request_had_audio = False
        # 不向 /v1/audio/speech（流式语音接口）提交 continuation_id（续接状态标识）。
        received_for_request = 0
        output_for_request = 0
        saw_audio_done = False
        saw_done = False
        stage = "submit"
        preview_recipe = self._is_preview and voice.model == "voxcpm2"
        if preview_recipe and not getattr(self.client.server, "supports_full_decode", True):
            raise self._reject("runtime", "本地语音运行程序尚不支持样音完整解码，请更新本地语音运行组件后重试。")
        logger.info(
            "audio_cpp_tts.request_start turn=%s speech=%s session=%s segment=%d chars=%d "
            "continuation=%s steps=%s text_digest=%s%s",
            *self._tts_fields(), self.completed_segments, len(text),
            self._continuation_enabled, self._inference_steps, text_digest(text),
            text_preview_suffix(text),
        )
        try:
            async with self.client._request_lock:
                request_sent_at = time.perf_counter()
                http = self.client._request_client()
                self._native_request_submitted = True
                stage = "stream"
                request_body = body
                path = "/v1/audio/speech"
                if preview_recipe:
                    path = "/v1/tasks/run"
                    options = {**body["options"], "voxcpm2.full_decode": True}
                    request_body = {
                        "model": self.model_id, "name": "tts",
                        "request": {
                            "text": body["input"],
                            "options": {key: (str(value).lower() if isinstance(value, bool) else str(value))
                                        for key, value in options.items()},
                        },
                    }
                    if voice.reference_path is not None:
                        request_body["request"]["voice_ref"] = str(voice.reference_path.resolve())
                        # 带设计描述的混合创作使用隔离音色通道，不能叠加续写提示。
                        if voice.reference_text.strip() and not voice.style_description.strip():
                            request_body["request"].update(
                                reference_text=voice.reference_text,
                                audio_base64=base64.b64encode(
                                    await asyncio.to_thread(voice.reference_path.read_bytes)).decode("ascii"))
                async with http.stream(
                    "POST",
                    f"{self.client.base_url}{path}",
                    json=request_body,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    headers_at = time.perf_counter()
                    self._active_response = response
                    response.raise_for_status()
                    lines = self._preview_lines(response) if preview_recipe else response.aiter_lines()
                    async for line in lines:
                        if self._cancelled:
                            break
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        if data == "[DONE]":
                            saw_done = True
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise AudioCppTtsError("VoxCPM 返回了无效 SSE JSON 事件") from exc
                        event_type = event.get("type")
                        if event_type == "speech.audio.delta":
                            self._request_had_audio = True
                            encoded = event.get("audio")
                            if not isinstance(encoded, str):
                                raise AudioCppTtsError("VoxCPM 音频事件缺少 audio 字段")
                            try:
                                pcm = base64.b64decode(encoded, validate=True)
                            except (ValueError, TypeError) as exc:
                                raise AudioCppTtsError("VoxCPM 音频事件不是有效 Base64") from exc
                            if pcm:
                                chunk_at = time.perf_counter()
                                chunk_count += 1
                                if last_audio_at is not None:
                                    max_chunk_gap_ms = max(max_chunk_gap_ms, (chunk_at - last_audio_at) * 1000)
                                last_audio_at = chunk_at
                                if len(pcm) % 2:
                                    raise AudioCppTtsError("VoxCPM 返回的 PCM 音频未对齐 int16 采样边界")
                                if first_audio_at is None:
                                    first_audio_at = chunk_at
                                    logger.info(
                                        "audio_cpp_tts.first_audio_ready turn=%s speech=%s session=%s segment=%d "
                                        "elapsed_ms（请求至首块毫秒）=%.1f chunk_audio_ms（首块音频毫秒）=%.1f",
                                        *self._tts_fields(), self.completed_segments,
                                        (chunk_at - request_started_at) * 1000,
                                        len(pcm) * 1000 / (self.client.native_sample_rate * 2),
                                    )
                                received_for_request += len(pcm)
                                audio_ms = received_for_request * 1000 / (self.client.native_sample_rate * 2)
                                if not self._is_prewarm and audio_ms > budget_ms:
                                    raise AudioCppDegenerateError(
                                        f"本地语音生成异常：音频超过正文时长预算（{budget_ms} 毫秒），已停止播报。")
                                output_pcm = self._resampler.push(pcm) if self._resampler is not None else pcm
                                if output_pcm:
                                    output_for_request += len(output_pcm)
                                    self.received_bytes += len(output_pcm)
                                    await self._queue_pcm(output_pcm)
                        elif event_type == "speech.audio.done":
                            timing = event.get("timing")
                            if isinstance(timing, dict):
                                native_timing = {key: float(value) for key, value in timing.items()
                                                 if key in {"ttft_ms", "wall_ms", "audio_duration_ms", "rtf"}
                                                 and type(value) in (int, float) and math.isfinite(value)}
                            saw_audio_done = True
                            self.generation_finished = True
                        elif event_type == "error":
                            detail = event.get("message") or event.get("error") or event
                            raise AudioCppTtsError(f"VoxCPM 合成失败：{str(detail)[:300]}")
                    self._active_response = None
            stage = "finalize"
            if self._cancelled:
                return
            if received_for_request <= 0:
                raise AudioCppTtsError("VoxCPM 请求结束但没有返回 PCM 音频")
            if not saw_audio_done:
                raise AudioCppTtsError("VoxCPM SSE 流没有收到 speech.audio.done 结束事件")
            if not saw_done:
                raise AudioCppTtsError("VoxCPM SSE 流没有收到 [DONE] 结束事件")
            if self._resampler is not None:
                tail = self._resampler.finish()
                if tail:
                    output_for_request += len(tail)
                    self.received_bytes += len(tail)
                    await self._queue_pcm(tail)
            receipt = TtsSegmentReceipt(
                index=self.completed_segments,
                text_chars=len(text),
                end_received_bytes=self.received_bytes,
            )
            self._segment_receipts.append(receipt)
            self.completed_segments += 1
            self.completed_sentences += 1
            self._segment_available.set()
            self.generation_finished = True
            if not self._is_preview and not self._is_prewarm:
                self.client._warmed = True
            completed_at = time.perf_counter()
            ttfb_ms = (
                (first_audio_at - request_started_at) * 1000
                if first_audio_at is not None else None
            )
            total_ms = (completed_at - request_started_at) * 1000
            audio_ms = output_for_request * 1000 / (self.client.sample_rate * 2)
            rtf = total_ms / audio_ms if audio_ms > 0 else None
            metrics: dict[str, float | int | str | bool | None] = {
                "chars": len(text),
                "pcm_bytes": output_for_request,
                "native_pcm_bytes": received_for_request,
                "ttfb_ms": ttfb_ms,
                "total_ms": total_ms,
                "audio_ms": audio_ms,
                "rtf": rtf,
                "http_headers_ms": (headers_at - request_sent_at) * 1000 if headers_at else None,  # 请求至响应头毫秒。
                "chunk_count": chunk_count,  # 音频分块数量。
                "max_chunk_gap_ms": max_chunk_gap_ms,  # 相邻非空音频块最大抵达间隔毫秒。
                "engine_ttft_ms": native_timing.get("ttft_ms"),  # 原生首音等待毫秒。
                "engine_wall_ms": native_timing.get("wall_ms"),  # 原生完整任务耗时毫秒。
                "engine_rtf": native_timing.get("rtf"),  # 原生完整任务实时因子。
                "continuation_enabled": self._continuation_enabled,  # 实际是否提交声学续接。
                "segment_index": self.completed_segments - 1,  # 当前事务分段序号。
                "num_inference_steps": body["options"].get("num_inference_steps"),  # 显式请求推理步数，空值沿用服务默认。
                "request_lock_wait_ms": (request_sent_at - request_started_at) * 1000,  # 请求互斥锁等待毫秒。
                "http_first_audio_ms": ((first_audio_at - request_sent_at) * 1000
                                        if first_audio_at is not None else None),  # 网络提交至首个音频块毫秒。
                "sample_rate": self.client.sample_rate,  # 下行采样率。
                "native_sample_rate": self.client.native_sample_rate,  # 原生采样率。
                "voice_id": voice.voice_id,  # 音色标识。
                "continuation_id": self._continuation_id if self._context_requested else None,  # 续接标识。
                "prompt_audio_sent": "prompt_audio" in body,  # prompt_audio_sent（是否提交续接提示音频）。
                "continuation_id_sent": "continuation_id" in body,  # continuation_id_sent（是否提交续接状态标识）。
                "generation_budget_ms": budget_ms,  # 生成时长预算。
                "endpoint": path,  # 本次实际合成入口。
                "reference_recipe": ("reference_prompt" if voice.reference_path and voice.reference_text.strip()
                                     else "reference_only" if voice.reference_path else "voice_design"),  # 实际参考配方。
                "full_decode": preview_recipe,  # 是否在生成结束后完整解码样音。
                "engine_audio_duration_ms": native_timing.get("audio_duration_ms", response.headers.get("X-AudioCPP-Audio-Duration-Ms")),  # 原生声明时长。
            }
            self.request_metrics.append(metrics)
            self.client._last_success_at = time.monotonic()
            logger.info(
                "audio_cpp_tts.request_complete turn=%s speech=%s session=%s local_model=%s model_id=%s chars=%d "
                "pcm_bytes=%d native_pcm_bytes=%d ttfb_ms=%.1f request_lock_wait_ms=%.1f "
                "http_first_audio_ms=%.1f total_ms=%.1f audio_ms=%.1f rtf=%.3f "
                "continuation_enabled=%s prompt_audio_sent（是否提交续接提示音频）=%s "
                "continuation_id_sent（是否提交续接状态标识）=%s segment_index=%d num_inference_steps=%s sample_rate=%d "
                "native_sample_rate=%d voice_id=%s continuation_id=%s endpoint=%s reference_recipe=%s "
                "budget_ms=%d engine_audio_duration_ms=%s "
                "http_headers_ms（响应头等待毫秒）=%.1f chunk_count（音频分块数）=%d "
                "max_chunk_gap_ms（最大块间隔毫秒）=%.1f full_decode（完整解码）=%s "
                "engine_ttft_ms（原生首音毫秒）=%s engine_wall_ms（原生完整任务毫秒）=%s "
                "engine_rtf（原生实时因子）=%s text_digest=%s%s",
                *self._tts_fields(), self.client.local_model, self.client.model_id, len(text),
                output_for_request, received_for_request, ttfb_ms or 0.0, metrics["request_lock_wait_ms"],
                metrics["http_first_audio_ms"] or 0.0, total_ms, audio_ms, rtf or 0.0,
                self._continuation_enabled, metrics["prompt_audio_sent"], metrics["continuation_id_sent"],
                self.completed_segments - 1, metrics["num_inference_steps"],
                self.client.sample_rate, self.client.native_sample_rate, voice.voice_id,
                metrics["continuation_id"], path, metrics["reference_recipe"], budget_ms,
                metrics["engine_audio_duration_ms"], metrics["http_headers_ms"] or 0.0,
                chunk_count, max_chunk_gap_ms, preview_recipe, metrics["engine_ttft_ms"],
                metrics["engine_wall_ms"], metrics["engine_rtf"], text_digest(text), text_preview_suffix(text),
            )
        except asyncio.CancelledError:
            logger.debug(
                "audio_cpp_tts.request_cancelled turn=%s speech=%s session=%s segment=%d stage=%s elapsed_ms=%.1f",
                *self._tts_fields(), self.completed_segments, stage,
                (time.perf_counter() - request_started_at) * 1000,
            )
            if not self._cancelled:
                error = AudioCppTtsError("VoxCPM 音频请求被取消")
                self._error = error
                self._segment_available.set()
                await self._audio.put(error)
            raise
        except BaseException as exc:
            if isinstance(exc, AudioCppDegenerateError):
                self.audio_rejected = True
                self._error = exc
                self.client._warmed = False
                # 丢弃提前合成但尚未交给播放器的数据，不能把错误排在坏音频后面。
                while not self._audio.empty():
                    self._audio.get_nowait()
                self._buffered_bytes = 0
                self._segment_available.set()
                callback = self.audio_rejected_callback
                if callable(callback):
                    try:
                        callback()
                    except Exception:
                        logger.exception("audio_cpp_tts.reject_playback_failed")
                logger.warning(
                    "audio_cpp_tts.degenerate_segment turn=%s speech=%s session=%s chars=%d "
                    "native_pcm_bytes=%d budget_ms=%d sample_rate=%d",
                    *self._tts_fields(), len(text), received_for_request, budget_ms, self.client.native_sample_rate,
                )
            # 失败原因必须落盘：此前只把异常塞进内部队列，日志里没有任何痕迹。
            retry_pending = bool(self._allow_retry and not self._request_had_audio
                                 and not self.audio_rejected)
            logger.log(
                logging.INFO if retry_pending else logging.WARNING,
                "audio_cpp_tts.request_failed turn=%s speech=%s session=%s segment=%d chars=%d stage=%s "
                "error_type=%s error=%s retry=%s had_audio=%s elapsed_ms=%.1f",
                *self._tts_fields(), self.completed_segments, len(text), stage,
                type(exc).__name__, str(exc)[:300], retry_pending, self._request_had_audio,
                (time.perf_counter() - request_started_at) * 1000,
            )
            if not self._cancelled and not retry_pending:
                error = exc if isinstance(exc, Exception) else AudioCppTtsError(str(exc))
                self._error = error
                self._segment_available.set()
                await self._audio.put(error)
            raise
        finally:
            self._active_response = None
            self._send_task = None

    async def _preview_lines(self, response: httpx.Response) -> AsyncIterator[str]:
        """把任务接口的完整 WAV（波形音频）接入既有回执、取消与音频收集流程。"""
        import io
        import soundfile as sf

        payload = bytearray()
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > 32 * 1024 * 1024:
                raise AudioCppTtsError("试音响应超过 32 MiB（二进制兆字节）。")
        result = json.loads(payload)
        encoded = None
        if isinstance(result, dict):
            candidates = [result.get(key) for key in ("audio", "audio_base64", "audio_output")]
            named = result.get("named_audio_outputs", [])
            if isinstance(named, list):
                candidates.extend(named)
            for candidate in candidates:
                if isinstance(candidate, dict):
                    candidate = next((candidate[key] for key in ("data", "base64", "audio")
                                      if isinstance(candidate.get(key), str)), None)
                if isinstance(candidate, str) and candidate:
                    encoded = candidate
                    break
        if encoded is None:
            raise AudioCppTtsError("原生试音任务没有返回 WAV（波形音频）数据。")
        try:
            wav = base64.b64decode(encoded, validate=True)
            samples, rate = sf.read(io.BytesIO(wav), dtype="int16", always_2d=True)
        except (ValueError, TypeError, sf.LibsndfileError) as exc:
            raise AudioCppTtsError("原生试音任务返回的音频无效。") from exc
        if rate != self.client.native_sample_rate or samples.shape[1] != 1:
            raise AudioCppTtsError("原生试音音频必须使用模型原生采样率和单声道。")
        pcm = samples[:, 0].astype("<i2", copy=False).tobytes()
        # 完整任务结果已经在手，判废必须早于向收集器交付任何样音。
        yield "data: " + json.dumps({"type": "speech.audio.delta", "audio": base64.b64encode(pcm).decode("ascii")})
        yield "data: " + json.dumps({"type": "speech.audio.done", "timing": result.get("timing")})
        yield "data: [DONE]"

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._resampler is not None:
            tail = self._resampler.finish()
            if tail:
                self.received_bytes += len(tail)
                await self._queue_pcm(tail)
        self._segment_available.set()
        await self._audio.put(_QUEUE_END)

    async def wait_for_segment(self, previous_count: int) -> TtsSegmentReceipt:
        """Return the completion boundary for one fully received text request."""
        while self.completed_segments <= previous_count:
            if self._error is not None:
                raise self._error
            if self._cancelled or self._finished:
                raise self._segment_wait_failure("session_closed", previous_count)
            self._segment_available.clear()
            try:
                await asyncio.wait_for(
                    self._segment_available.wait(), self.client.timeout_seconds,
                )
            except TimeoutError as exc:
                self._log_segment_wait("timeout", previous_count)
                raise AudioCppTtsError("等待 VoxCPM 文本分段回执超时") from exc
        try:
            return self._segment_receipts[previous_count]
        except IndexError as exc:
            self._log_segment_wait("missing_receipt", previous_count)
            raise AudioCppTtsError("VoxCPM 文本分段回执缺失") from exc

    def _log_segment_wait(self, reason: str, previous_count: int) -> None:
        logger.warning(
            "audio_cpp_tts.segment_wait_failed turn=%s speech=%s session=%s reason=%s segment=%d "
            "completed_segments=%d timeout_seconds=%.1f",
            *self._tts_fields(), reason, previous_count, self.completed_segments,
            self.client.timeout_seconds,
        )

    def _segment_wait_failure(self, reason: str, previous_count: int) -> AudioCppTtsError:
        self._log_segment_wait(reason, previous_count)
        return AudioCppTtsError("VoxCPM 未确认当前文本分段完成")

    async def cancel(self, *, timeout_seconds: float | None = None) -> bool:
        del timeout_seconds
        self._cancelled = True
        response = self._active_response
        if response is not None:
            with contextlib.suppress(Exception):
                await response.aclose()
        task = self._send_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._finished = True
        if self._resampler is not None:
            self._resampler.reset()
        self._segment_available.set()
        while True:
            try:
                self._audio.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._buffered_bytes = 0
        await self._audio.put(_QUEUE_END)
        return True

    async def audio_chunks(self) -> AsyncIterator[bytes]:
        if self._audio_iterator_started:
            raise AudioCppTtsError("VoxCPM PCM 只能读取一次")
        self._audio_iterator_started = True
        while True:
            item = await self._audio.get()
            if item is _QUEUE_END:
                return
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, bytes):
                self._buffered_bytes -= len(item)
                yield item

    async def _queue_pcm(self, pcm: bytes) -> None:
        if self.first_audio_ready_at is None:
            self.first_audio_ready_at = time.monotonic()
        callback = getattr(self, "first_audio_callback", None)
        if callable(callback):
            callback()  # first_audio_callback（原生音频就绪，独立于允许播放时刻）。
        if self._buffered_bytes + len(pcm) > 32 * 1024 * 1024:
            logger.warning(
                "audio_cpp_tts.buffer_overflow turn=%s speech=%s session=%s buffered_bytes=%d chunk_bytes=%d",
                *self._tts_fields(), self._buffered_bytes, len(pcm),
            )
            raise AudioCppTtsError("提前合成音频暂存超过 32 MiB（二进制兆字节）")
        self._buffered_bytes += len(pcm)
        await self._audio.put(pcm)

    async def _release_context(self) -> None:
        needs_handoff = ((self._cancelled or self.audio_rejected) and self._native_request_submitted
                         and isinstance(self.client.server, AudioCppTtsServer))
        if not self._context_requested and not needs_handoff:
            return
        try:
            if self._context_release_deadline is None:
                # 正式中止不沿用默认五分钟合成超时；试听仍可等待长样音计算结束。
                budget = self.client.timeout_seconds if self._is_preview else min(10.0, self.client.timeout_seconds)
                self._context_release_deadline = time.monotonic() + max(0.0, budget)
            deadline = self._context_release_deadline
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.context_release_confirmed = False
                    logger.warning("audio_cpp_tts.context_release_timeout turn=%s speech=%s session=%s",
                                   *self._tts_fields())
                    return
                # HTTPX（网络客户端）的分阶段超时不等于整个请求的墙钟期限。
                async with asyncio.timeout(remaining):
                    response = await self.client._request_client().post(
                        f"{self.client.base_url}/v1/audio/speech/contexts/release",
                        json={"model": self.model_id, "continuation_id": self._continuation_id},
                        # 原生取得执行锁最多等待两秒，响应读取需留出传输余量。
                        timeout=min(3.0, remaining),
                    )
                # 样音取消或正式流式判废后，原生计算可能尚未退出。
                # server_busy（服务忙）仅重试交接，不重新提交已播放的正文。
                busy = False
                if response.status_code == 503:
                    with contextlib.suppress(ValueError, AttributeError):
                        busy = response.json().get("error", {}).get("type") == "server_busy"
                if not (busy and time.monotonic() < deadline):
                    break
                await asyncio.sleep(min(.1, max(0.0, deadline - time.monotonic())))
            response.raise_for_status()
            self.context_release_confirmed = response.json().get("released") is True
            if self.context_release_confirmed:
                self._context_requested = False
                self._native_request_submitted = False
                self._context_release_deadline = None
        except (httpx.HTTPError, OSError) as exc:
            self.context_release_confirmed = False
            logger.warning(
                "audio_cpp_tts.context_release_failed turn=%s speech=%s session=%s reason=%s",
                *self._tts_fields(), str(exc)[:160],
            )

    async def close(self) -> None:
        if not self._finished and not self._cancelled:
            await self.cancel()
        await self._release_context()
        if self._release_voice:
            self._release_voice()
            self._release_voice = None

    def _control_instructions(self) -> list[str]:
        """control_instructions（逐请求控制提示）；只适用于 VoxCPM2。"""
        voice = self.voice_snapshot
        if voice is None or voice.model != "voxcpm2" or not self._is_preview:
            return []
        instructions: list[str] = []
        if voice.style_description.strip():
            instructions.append(voice.style_description.strip())
        return instructions

    def _request_text(self, text: str) -> str:
        """每个独立文本块使用同一快照，不改事务回执中的原始字符数。"""
        instructions = self._control_instructions()
        if not instructions:
            return text
        return f"({'；'.join(instructions)}){text}"


class _Pcm16Resampler:
    """soxr（高质量重采样）流：跨分块保存滤波状态与采样尾部。"""

    def __init__(self, source_rate: int = 48000, target_rate: int = 16000) -> None:
        import soxr
        self._stream = soxr.ResampleStream(source_rate, target_rate, 1, dtype="int16", quality="HQ")
        self._byte_carry = b""  # byte_carry（半个 PCM16 采样的字节尾数）。

    def push(self, pcm: bytes) -> bytes:
        import numpy as np
        raw = self._byte_carry + pcm
        if len(raw) % 2:
            self._byte_carry, raw = raw[-1:], raw[:-1]
        else:
            self._byte_carry = b""
        return self._stream.resample_chunk(np.frombuffer(raw, dtype="<i2")).astype("<i2", copy=False).tobytes()

    def finish(self) -> bytes:
        import numpy as np
        output = self._stream.resample_chunk(np.empty(0, dtype=np.int16), last=True).astype("<i2", copy=False).tobytes()
        self.reset()
        return output

    def reset(self) -> None:
        self._byte_carry = b""
        self._stream.clear()

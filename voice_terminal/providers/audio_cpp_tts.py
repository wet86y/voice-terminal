"""audio.cpp（原生音频推理框架）VoxCPM（本地语音合成模型）适配。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4
from typing import Any, AsyncIterator, Callable, Mapping

import httpx

from ..tts.scheduling import TtsSegmentReceipt
from ..tts.capabilities import TtsCapabilities
from ..tts.voices import ResolvedLocalTtsVoice, VoiceLibrary
from ..gpu_performance import GpuPerformanceMode

logger = logging.getLogger(__name__)

_QUEUE_END = object()
_PREWARM_TEXT = "你好，准备开始语音播报。"
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


class AudioCppTtsServer:
    """启动和管理本机 audio.cpp（原生音频推理框架）TTS 服务。"""

    @classmethod
    def runtime_directory(cls, runtime_root: Path) -> Path:
        """runtime_directory（原生运行目录）：优先使用组件安装目录，兼容此前迁入的资源。"""
        root = Path(runtime_root).resolve()
        managed = root / "models/audiocpp/runtime"
        return managed if (managed / "cuda/audiocpp_server.exe").is_file() else root / "runtimes/audiocpp"

    @classmethod
    def available_models(cls, runtime_root: Path, *, program_root: Path | None = None) -> list[str]:
        """available_models（完整本地候选）：权重、原生程序和模型规格均存在才算可启动。"""
        root = Path(runtime_root).resolve()
        runtime = cls.runtime_directory(root)
        specs = runtime / "model_specs"
        if not specs.is_dir():
            specs = root / "sources/audio.cpp/model_specs"
        if not (runtime / "cuda/audiocpp_server.exe").is_file():
            return []
        # 正式安装具有收据：启动选路时复核内容，避免损坏的可选模型抢占仍可用的另一后端。
        from ..model_installation import inspect_installed_model
        if (runtime / "resource-install.json").is_file() and inspect_installed_model(
                root / "models", "audiocpp/runtime", "tts-audiocpp-runtime", verify_hashes=True).error:
            return []
        result: list[str] = []
        catalog = None
        for key in ("voxcpm2", "voxcpm1"):
            relative = Path("audiocpp/tts") / _MODEL_CATALOG[key]["relative_path"].parent
            folder = root / "models" / relative
            if not (folder / _MODEL_CATALOG[key]["relative_path"].name).is_file() or not (specs / f"{key}.json").is_file():
                continue
            if (folder / "resource-install.json").is_file():
                if inspect_installed_model(root / "models", relative.as_posix(), f"tts-{key}", verify_hashes=True).error:
                    continue
            else:
                # 旧迁入目录没有收据时，同样按随包清单验证，不能让损坏但仍存在的权重抢占可用后端。
                from ..resource_distribution import ResourceCatalog, ResourceError
                try:
                    if catalog is None:
                        catalog = ResourceCatalog(program_root or Path(__file__).resolve().parents[2])
                    resource = catalog.get(f"tts-{key}")
                    valid = True
                    for item in resource.required_files:
                        path = folder / item.path
                        if not path.is_file() or path.stat().st_size != item.size_bytes:
                            valid = False
                            break
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
        self._owns_process = False
        self._ready = False
        self._http_transport = http_transport
        self._management_http: httpx.AsyncClient | None = None
        self._management_transport: httpx.AsyncBaseTransport | None = http_transport
        self._lock = asyncio.Lock()
        self.local_model = local_model
        self._loaded_model: str | None = None
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
        return {
            "local_model": local_model,
            "loaded_model": loaded_model,
            "models": [
                {
                    "id": key,
                    "name": str(info["name"]),
                    "native_sample_rate": int(info["native_sample_rate"]),
                    "available": (root / "models" / "audiocpp" / "tts" / Path(info["relative_path"])).is_file(),
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
        return {
            "local_model": self.local_model,  # local_model（所选本地模型）。
            "loaded_model": self._loaded_model,  # loaded_model（当前驻留内存的模型）。
            "models": [
                {
                    "id": key,  # id（稳定的本地模型选择值）。
                    "name": str(info["name"]),  # name（界面显示名称）。
                    "native_sample_rate": int(info["native_sample_rate"]),  # native_sample_rate（原生采样率）。
                    "available": self.model_available(key),  # available（权重与参考音频是否齐全）。
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
        self._log_file = log_path.open("ab", buffering=0)
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
        self._process = subprocess.Popen(
            command,
            cwd=source_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        self._owns_process = True
        logger.info("audio_cpp_tts.server_started pid=%s config=%s", self._process.pid, self.config_path)

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
            await self._unload_model_locked(prior[0])
        body = {
            **configured["load_payload"],
            "id": configured["id"],
            "path": str(configured["path"]),
            "family": configured["family"],
            "task": "tts",
            "mode": "streaming",
        }
        await self._post_model_action("/v1/models/load", body, timeout_seconds=300.0)
        loaded_entries = await self._read_model_entries()
        if loaded_entries.get(model_id, {}).get("loaded") is not True:
            raise AudioCppTtsError(f"{_MODEL_CATALOG[local_model]['name']} 加载请求完成，但服务未确认模型已驻留。")
        self._loaded_model = local_model

    async def _unload_model_locked(self, model_id: str) -> None:
        await self._post_model_action("/v1/models/unload", {"id": model_id})
        entries = await self._read_model_entries()
        if entries.get(model_id, {}).get("loaded") is True:
            raise AudioCppTtsError(f"audio.cpp 服务未确认旧模型已卸载：{model_id}")
        self._loaded_model = None

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
            try:
                entries = await self._read_model_entries()
                if entries.get(previous_id, {}).get("loaded") is True:
                    await self._unload_model_locked(previous_id)
                await self._load_model_locked(local_model)
            except Exception as exc:
                try:
                    entries = await self._read_model_entries()
                    target_id = str(_MODEL_CATALOG[local_model]["id"])
                    if entries.get(target_id, {}).get("loaded") is True:
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
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None


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
    playback_startup_buffer_ms = 800  # playback_startup_buffer_ms（本地流式首次连续音频储备）。
    playback_resume_buffer_ms = 400  # playback_resume_buffer_ms（欠载后连续音频储备）。

    def __init__(self, server: AudioCppTtsServer, *, timeout_seconds: float = 300.0,
                 sample_rate: int = 16000, sentence_min_effective_chars: int = 10,
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
        self.sample_rate = int(sample_rate)
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
        self.acoustic_continuation = acoustic_continuation
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
        await self.server.switch_model(local_model)
        self._warmed = False

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
                emotion: str | None = None, voice_snapshot: ResolvedLocalTtsVoice | None = None,
                preview: bool = False, inference_steps: int | None = None) -> "AudioCppTtsSession":
        del fresh, turn_id
        if inference_steps is not None and (type(inference_steps) is not int or not 1 <= inference_steps <= 30):
            raise AudioCppTtsError("inference_steps（固定推理步数）必须为 1–30 的整数。")
        owner = self.voice_library
        if callable(getattr(owner, "for_model", None)):
            owner = owner.for_model(voice_snapshot.model if voice_snapshot else self.local_model)
        guard = owner.snapshot_guard() if callable(getattr(owner, "snapshot_guard", None)) else contextlib.nullcontext()
        with guard:
            session = AudioCppTtsSession(self, voice_snapshot=voice_snapshot or self.resolve_voice(emotion, preview=preview))
            if inference_steps is not None:
                session._inference_steps = inference_steps
            return session

    async def ensure_ready(self) -> None:
        await self.server.ensure_ready()
        native = getattr(self.server, "capabilities", {})
        self._native_continuation = bool(native.get("acoustic_continuation", False))
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
        del turn_id
        await self.ensure_ready()
        async with self._prewarm_lock:
            if self._real_waiters:
                return {"status": "READY", "reason": "real_request_priority"}
            if self._warmed and (not self.local_optimization or time.monotonic() - self._last_success_at < 60):
                return {"status": "READY", "reason": "already_warmed"}
            session = self.session(emotion="calm", voice_snapshot=voice_snapshot)
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
                logger.info("audio_cpp_tts.prewarm_ready audio_bytes=%d", audio_bytes)
                return {"status": "READY", "audio_bytes": audio_bytes}
            except BaseException:
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
                 voice_snapshot: ResolvedLocalTtsVoice | None = None) -> None:
        self.client = client
        self.voice_snapshot = voice_snapshot
        self.model_id = voice_snapshot.native_model_id if voice_snapshot else client.model_id
        self._audio: asyncio.Queue[bytes | BaseException | object] = asyncio.Queue()
        self._started = False
        self._finished = False
        self._cancelled = False
        self._active_response: httpx.Response | None = None
        self._send_task: asyncio.Task | None = None
        self._continuation_id = uuid4().hex  # _continuation_id（唯一事务声学上下文）。
        self._continuation_enabled = False  # _continuation_enabled（本事务实际使用声学续接）。
        self._context_requested = False  # _context_requested（是否向原生申请过上下文）。
        self._native_request_submitted = False  # 原生请求已进入网络发送路径。
        self.context_release_confirmed = None  # 原生执行锁取得且上下文释放的明确确认。
        self._is_prewarm = False
        self._is_preview = bool(voice_snapshot and voice_snapshot.is_preview)
        self._request_options = {}
        model = voice_snapshot.model if voice_snapshot else client.local_model
        self._inference_steps = client.inference_steps_by_model[model]  # 每回答固定步数；热更新作用于下一事务。
        self._request_had_audio = False
        self._allow_retry = False
        self._buffered_bytes = 0  # _buffered_bytes（尚未由播放器读取的音频字节）。
        self.first_audio_callback = None  # first_audio_callback（合成首音就绪通知钩子）。
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
        self._continuation_enabled = (
            self.client.capabilities.acoustic_continuation
            and not self._is_prewarm and not self._is_preview and not controlled
        )
        voice = self.voice_snapshot
        logger.info(
            "audio_cpp_tts.session_policy model_id=%s voice_id=%s voice_revision=%s "
            "continuation_enabled=%s control_prefix=%s prewarm=%s preview=%s",
            self.model_id, voice.voice_id if voice else None, voice.revision if voice else None,
            self._continuation_enabled, controlled, self._is_prewarm, self._is_preview,
        )
        self._started = True

    async def send_text(self, text: str) -> None:
        if self._is_prewarm:
            return await self._send_with_fallback(text)
        self.client._real_waiters += 1
        try:
            await self._send_with_fallback(text)
        finally:
            self.client._real_waiters -= 1

    async def _send_with_fallback(self, text: str) -> None:
        warm = self.client._warm_task
        if not self._is_prewarm and warm is not None and warm is not asyncio.current_task():
            warm.cancel()
            await asyncio.gather(warm, return_exceptions=True)
        self._allow_retry = self._continuation_enabled
        try:
            await self._send_text_once(text)
        except Exception as exc:
            if not self._allow_retry or self._request_had_audio:
                raise
            logger.warning("audio_cpp_tts.continuation_fallback reason=%s", str(exc)[:180])
            await self._release_context()
            self._allow_retry = False
            # 再建立新上下文；缺失前文时原生使用正式参考独立合成。
            await self._send_text_once(text)

    async def _send_text_once(self, text: str) -> None:
        if not self._started:
            raise AudioCppTtsError("VoxCPM 会话尚未启动")
        if self._finished or self._cancelled:
            raise AudioCppTtsError("VoxCPM 会话已结束")
        if not text.strip():
            return
        self._send_task = asyncio.current_task()
        request_started_at = time.perf_counter()
        request_sent_at = request_started_at
        first_audio_at: float | None = None
        body = {
            "model": self.model_id,
            "input": self._request_text(text),
            "response_format": "pcm",
            "stream_format": "sse",
            "options": {"retry_badcase": False, **self._request_options},
        }
        body["options"]["num_inference_steps"] = self._inference_steps
        voice = self.voice_snapshot
        if voice is None:
            raise AudioCppTtsError("尚未配置可用音色，请先导入或创建音色并启用。")
        if voice.model == "voxcpm1":
            if not voice.reference_path or not voice.reference_text.strip():
                raise AudioCppTtsError("VoxCPM1（第一代本地语音模型）需要参考音频和对应转写文本。")
            body["voice_ref"] = str(voice.reference_path.resolve())
            body["reference_text"] = voice.reference_text
        elif voice.reference_mode == "voice":
            if not voice.reference_path:
                raise AudioCppTtsError("VoxCPM2（第二代本地语音模型）参考音频不可用。")
            body["voice_ref"] = str(voice.reference_path.resolve())
        self._request_had_audio = False
        if self._continuation_enabled:
            body["continuation_id"] = self._continuation_id
            self._context_requested = True
        received_for_request = 0
        output_for_request = 0
        saw_audio_done = False
        saw_done = False
        try:
            async with self.client._request_lock:
                request_sent_at = time.perf_counter()
                http = self.client._request_client()
                self._native_request_submitted = True
                async with http.stream(
                    "POST",
                    f"{self.client.base_url}/v1/audio/speech",
                    json=body,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    self._active_response = response
                    response.raise_for_status()
                    async for line in response.aiter_lines():
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
                                if len(pcm) % 2:
                                    raise AudioCppTtsError("VoxCPM 返回的 PCM 音频未对齐 int16 采样边界")
                                if first_audio_at is None:
                                    first_audio_at = time.perf_counter()
                                received_for_request += len(pcm)
                                output_pcm = self._resampler.push(pcm) if self._resampler is not None else pcm
                                if output_pcm:
                                    output_for_request += len(output_pcm)
                                    self.received_bytes += len(output_pcm)
                                    await self._queue_pcm(output_pcm)
                        elif event_type == "speech.audio.done":
                            saw_audio_done = True
                            self.generation_finished = True
                        elif event_type == "error":
                            detail = event.get("message") or event.get("error") or event
                            raise AudioCppTtsError(f"VoxCPM 合成失败：{str(detail)[:300]}")
                    self._active_response = None
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
                "continuation_enabled": self._continuation_enabled,  # 实际是否提交声学续接。
                "segment_index": self.completed_segments - 1,  # 当前事务分段序号。
                "num_inference_steps": body["options"].get("num_inference_steps"),  # 显式请求推理步数，空值沿用服务默认。
                "request_lock_wait_ms": (request_sent_at - request_started_at) * 1000,  # 请求互斥锁等待毫秒。
                "http_first_audio_ms": ((first_audio_at - request_sent_at) * 1000
                                        if first_audio_at is not None else None),  # 网络提交至首个音频块毫秒。
            }
            self.request_metrics.append(metrics)
            self.client._last_success_at = time.monotonic()
            logger.info(
                "audio_cpp_tts.request_complete local_model=%s model_id=%s chars=%d "
                "pcm_bytes=%d native_pcm_bytes=%d ttfb_ms=%.1f request_lock_wait_ms=%.1f "
                "http_first_audio_ms=%.1f total_ms=%.1f audio_ms=%.1f rtf=%.3f "
                "continuation_enabled=%s segment_index=%d num_inference_steps=%s",
                self.client.local_model, self.client.model_id, len(text), output_for_request,
                received_for_request, ttfb_ms or 0.0, metrics["request_lock_wait_ms"],
                metrics["http_first_audio_ms"] or 0.0, total_ms, audio_ms, rtf or 0.0,
                self._continuation_enabled, self.completed_segments - 1, metrics["num_inference_steps"],
            )
        except asyncio.CancelledError:
            if not self._cancelled:
                error = AudioCppTtsError("VoxCPM 音频请求被取消")
                self._error = error
                self._segment_available.set()
                await self._audio.put(error)
            raise
        except BaseException as exc:
            if not self._cancelled and not (self._allow_retry and not self._request_had_audio):
                error = exc if isinstance(exc, Exception) else AudioCppTtsError(str(exc))
                self._error = error
                self._segment_available.set()
                await self._audio.put(error)
            raise
        finally:
            self._active_response = None
            self._send_task = None

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
                raise AudioCppTtsError("VoxCPM 未确认当前文本分段完成")
            self._segment_available.clear()
            try:
                await asyncio.wait_for(
                    self._segment_available.wait(), self.client.timeout_seconds,
                )
            except TimeoutError as exc:
                raise AudioCppTtsError("等待 VoxCPM 文本分段回执超时") from exc
        try:
            return self._segment_receipts[previous_count]
        except IndexError as exc:
            raise AudioCppTtsError("VoxCPM 文本分段回执缺失") from exc

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
        callback = getattr(self, "first_audio_callback", None)
        if callable(callback):
            callback()  # first_audio_callback（原生音频就绪，独立于允许播放时刻）。
        if self._buffered_bytes + len(pcm) > 32 * 1024 * 1024:
            raise AudioCppTtsError("提前合成音频暂存超过 32 MiB（二进制兆字节）")
        self._buffered_bytes += len(pcm)
        await self._audio.put(pcm)

    async def _release_context(self) -> None:
        needs_handoff = (self._cancelled and self._native_request_submitted
                         and isinstance(self.client.server, AudioCppTtsServer))
        if not self._context_requested and not needs_handoff:
            return
        try:
            response = await self.client._request_client().post(
                f"{self.client.base_url}/v1/audio/speech/contexts/release",
                json={"model": self.model_id, "continuation_id": self._continuation_id}, timeout=2.0,
            )
            response.raise_for_status()
            self.context_release_confirmed = response.json().get("released") is True
            if self.context_release_confirmed:
                self._context_requested = False
                self._native_request_submitted = False
        except (httpx.HTTPError, OSError) as exc:
            self.context_release_confirmed = False
            logger.warning("audio_cpp_tts.context_release_failed reason=%s", str(exc)[:160])

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

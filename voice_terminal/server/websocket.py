from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
import json
import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from ..audio.capture import AudioCapture
from ..application.managed import report as application_report
from .application_runtime import ApplicationRuntimeMixin
from ..audio.player import PcmPlayer
from ..audio.processing import AudioPreprocessor
from ..asr.client import AsrWorkerClient
from ..config import AppConfig, LOCAL_TTS_PLAYER_RATES, local_tts_native_rate
from ..paths import AppPaths
from ..custom_instructions import shadow_projection
from ..controller.episode_controller import EpisodeController
from ..dsh import (
    DshBridgeError,
    DshMappingStore,
    DshRuntimeManager,
    DshShadowAgentSession,
)
from ..vad.barge_in import BargeInGate
from ..core.event_bus import EventBus
from ..core.events import VoiceEvent, event
from ..core.state import (
    BLOCKING_SERVICES, SERVICE_LABELS, SERVICE_LOADING_STATES, SERVICE_READY_STATES,
    RuntimeState, TerminalState,
)
from ..kws.detector import KwsDetector, KwsError
from ..kws.service import KwsWorker
from ..providers.deepseek import DeepSeekClient
from ..providers.audio_cpp_tts import AudioCppTtsClient, AudioCppTtsServer
from ..tts.voices import ModelVoiceLibraries, VoiceLibraryError, safe_voice_diagnostic
from ..providers.minimax_search import MiniMaxSearchClient
from ..providers.minimax_tts import MiniMaxTtsClient
from ..tts.router import TtsBackendRouter
from ..agent.tool_registry import FileTools, WebSearchTools
from ..shadow_agent import DurableVoiceEvent, ShadowAgentComponent, ShadowContextProjector, ShadowEvent
from ..shadow_agent.component import ShadowAgentSession
from ..shadow_agent.events import ShadowEventBuffer
from ..shadow_agent.prompts import load_shadow_instructions
from ..shadow_agent.provider import DeepSeekResponsesShadowProvider
from ..shadow_agent.tools import AmbientContextTools, ShadowToolExecutor
from ..shadow_agent.durable_mailbox import DurableVoiceMailbox
from ..continuity import ContinuityManager
from ..session.ledger import SessionLedger
from ..session.runtime import ConversationRuntime
from ..session.ambient import AmbientTranscriptStore
from ..speaker.client import SpeakerWorkerClient
from ..speaker.registry import SpeakerProfileStore
from ..speaker.router import SpeakerRouter
from ..speaker.engine import WeSpeakerOnnxEngine
from ..vad.detector import FsmnVadDetector
from ..compute import device_candidates, hardware_status
from ..analysis.client import AnalysisClient
from .product import (
    PERSONA_FILES,
    ProductSettingsStore,
    ShadowProductProjection,
)
from .snapshot import snapshot_event
from .speaker_runtime import SpeakerRuntimeMixin
from .handoff_runtime import HandoffRuntimeMixin
from .settings_runtime import ProductSettingsRuntimeMixin
from .runtime_operations import runtime_operation, runtime_operations

logger = logging.getLogger(__name__)

@asynccontextmanager
async def _no_session_lock():
    yield


class SessionRuntime(ProductSettingsRuntimeMixin, HandoffRuntimeMixin, SpeakerRuntimeMixin, ApplicationRuntimeMixin):
    """The sole production runtime: a session orchestrating input, agent and speech."""
    def __init__(self, config: AppConfig, *, product_settings: ProductSettingsStore | None = None,
                 speaker_worker_config_path: Path | None = None,
                 runtime_data_dir: Path | None = None) -> None:
        # Retired local TTS settings are normalized to the supported API.
        self.config = config
        self.dsh = DshRuntimeManager(
            config.dsh,
            callback_origin=f"http://127.0.0.1:{config.server.port}",
        )
        self.product_settings = product_settings
        from ..asr.hotwords import HotwordService
        self.hotwords = HotwordService()  # hotwords（共享依赖、分通道的热词服务）。
        self._instruction_snapshot = (
            product_settings.settings_only().get("custom_instructions", [])
            if product_settings is not None else []
        )
        self._pending_product_settings: dict[str, Any] | None = None
        self._product_settings_lock = asyncio.Lock()  # 产品设置应用互斥，防止并行换入模型。
        self.state = RuntimeState()
        self.bus = EventBus()
        self.history = SessionLedger(limit=10)
        self._conversation_tasks: set[asyncio.Task] = set()
        # The conversation log is written from synchronous commit paths, so the
        # runtime owns the publication that follows every append.
        self.history.conversation.set_sink(self._publish_conversation_entry)
        self.startup_errors: list[str] = []
        # _service_errors（能力错误快照）：按能力记录最近一次失败原因，供能力行与恢复清除。
        self._service_errors: dict[str, str] = {}
        # phased_startup（分阶段启动）：轻量接口先监听，本地模型和会话分别由宿主放行。
        self._application_phased_startup = os.environ.get("VOICE_APP_PHASED_STARTUP") == "1"
        self._application_local_ready = False
        self._application_session_ready = False
        # _listen_resume_requested（挂起的开始监听意图）：能力加载中被拦截的用户意图，就绪后自动执行。
        self._listen_resume_requested: bool | None = None
        self._application_startup_lock = asyncio.Lock()
        # compute_status（计算状态）记录 Voice Server 内 KWS 与 FSMN-VAD 的实际设备。
        self._compute_models: dict[str, dict[str, Any]] = {}
        self.capture: AudioCapture | None = None
        self.kws: KwsWorker | None = None
        self.vad: FsmnVadDetector | None = None
        self.asr: AsrWorkerClient | None = None
        self.llm: DeepSeekClient | None = None
        project_root = runtime_data_dir.resolve().parent if runtime_data_dir else next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
        self._project_root = project_root
        self.paths = config.paths if config.paths and config.paths.separated else AppPaths(project_root, project_root)
        if runtime_data_dir is not None:
            self._runtime_local_root = Path(runtime_data_dir).resolve().parent
        elif self.paths.separated:
            self._runtime_local_root = self.paths.install_root
        else:
            self._runtime_local_root = project_root / ".local"
        self._tts_voice_root = (Path(runtime_data_dir).resolve() / "tts"
                                if runtime_data_dir is not None and not self.paths.separated
                                else self.paths.tts_voices.parent)
        try:
            self.voice_library = ModelVoiceLibraries(self._tts_voice_root)
            self._local_tts_voice_error: str | None = None
        except VoiceLibraryError as exc:
            self.voice_library = None
            self._local_tts_voice_error = str(exc)
            self.startup_errors.append(f"tts_voice_library: {exc}")
        from ..resources import ResourcePolicy
        resource_policy = ResourcePolicy.load()
        self._history_cache_bytes = resource_policy.history_cache_bytes
        self._init_handoff()
        default_worker_config = project_root / "config" / "default.yaml"
        # Child config（子进程配置文件） keeps the worker on the same model and port.
        self._speaker_worker_config_path = (
            speaker_worker_config_path or default_worker_config
        ).resolve()
        if not self._speaker_worker_config_path.is_file():
            self._speaker_worker_config_path = default_worker_config.resolve()
        # Process handle（进程句柄） and lock（互斥锁） keep startup single-owner and race-safe.
        self._speaker_worker_process: asyncio.subprocess.Process | None = None
        self._speaker_worker_lock = asyncio.Lock()
        self.tts: TtsBackendRouter | None = None
        self._local_tts_load_state = "unloaded"  # local_tts_load_state（本地模型加载状态）。
        self._local_tts_error: str | None = None  # local_tts_error（最近一次加载错误）。
        self.shadow_search_tools: WebSearchTools | None = None
        self.shadow_component: ShadowAgentComponent | None = None
        self.shadow_session: ShadowAgentSession | DshShadowAgentSession | None = None
        if product_settings is not None:
            shadow_user_profile = product_settings.read_persona().get("user", "")
        else:
            try:
                shadow_user_profile = (config.persona.directory / config.persona.user_file).read_text(
                    encoding="utf-8",
                )
            except OSError:
                shadow_user_profile = ""
        self._shadow_user_profile = shadow_user_profile
        self.continuity = ContinuityManager(
            self.paths.config / "continuity-state.json",
            self._summarize_continuity,
            on_document_changed=self._continuity_document_changed,
            on_organized=self._commit_continuity_cleanup,
            on_failure=self._handoff_failed,
            max_document_chars=config.continuity.max_document_chars,
        )
        self._dsh_busy = False
        self._voice_silence_anchor: float | None = None
        self._cleanup_in_progress = False
        self._handoff_marker = self.paths.config / "continuity-handoff.json"
        self._task_delivery_path = self.paths.config / "dsh-task-deliveries.json"
        try:
            delivered = json.loads(self._task_delivery_path.read_text(encoding="utf-8"))
            self._task_deliveries = dict(delivered.get("receipts") or {})
        except FileNotFoundError:
            self._task_deliveries = {}
        self.shadow_context = ShadowContextProjector(
            self.history, user_profile=self._shadow_user_profile,
            continuity_document=self.continuity.document,
            custom_instructions=shadow_projection(self._instruction_snapshot),
        )
        self._shadow_pending = ShadowEventBuffer()
        self._voice_mailbox = DurableVoiceMailbox(self.paths.config / "voice-mailbox.json")
        self.shadow_product = ShadowProductProjection()
        self._shadow_debug_sinks: dict[str, Any] = {}
        self.player: PcmPlayer | None = None
        self._pending_local_tts_rate: tuple[str, int] | None = None
        self.audio_processor: AudioPreprocessor | None = None
        self.controller: EpisodeController | None = None
        profile_root = self.paths.speaker if self.paths.separated else config.persona.directory.resolve().parent / "speaker"
        self.ambient_store = AmbientTranscriptStore()
        self.speaker_profile = SpeakerProfileStore(profile_root / config.speaker.model_id, model_id=config.speaker.model_id)
        self.speaker_router = SpeakerRouter(
            self.speaker_profile,
            confirm_threshold=config.speaker.confirm_threshold,
            continuity_threshold=config.speaker.continuity_threshold,
            min_speech_ms=config.speaker.min_speech_ms,
            min_turn_speech_ms=config.speaker.min_turn_speech_ms,
        )
        self.speaker: SpeakerWorkerClient | None = None
        self._speaker_engine: WeSpeakerOnnxEngine | None = None
        self._speaker_engine_lock = threading.RLock()  # 本地声纹实例的构造与缓存释放锁。
        self._speaker_enrollment: dict[str, dict[str, Any]] = {}
        self._speaker_test_result: dict[str, Any] | None = None
        self.conversation = ConversationRuntime()
        self._voice_identity_path = (
            self.paths.history / "active-voice-session.json"
            if self.paths.separated or runtime_data_dir is not None or not config.dsh.enabled
            else config.dsh.home_dir.parent / "active-voice-session.json"
        )
        self._session_lock = asyncio.Lock()
        from .tts_voice_runtime import TtsVoiceOperationManager
        self.tts_voice_operations = TtsVoiceOperationManager(self)
        self._migrate_legacy_default_voice()
        if self.product_settings is not None:
            self.product_settings._tts_voice_selections = self._active_voices_by_model
        self.build_info: dict[str, Any] = {}
        self.reasoning_mode = config.llm.reasoning_mode
        self._credential_overrides: dict[str, str] = {}
        self.state.wake_always_on = bool(config.wake.enabled)
        self.state.set_service(
            "speaker",
            "DISABLED" if config.speaker.mode == "off" and not config.speaker.owner_only_barge_in
            else "STARTING",
        )


    def _build_kws_worker(self, config: AppConfig | None = None) -> KwsWorker:
        config = config or self.config
        requested = config.compute.devices.get("kws", "cpu")
        fallback = config.compute.fallback_devices.get("kws", "cpu")
        last_error: Exception | None = None
        for device in device_candidates(requested, "kws", fallback):
            try:
                detector = KwsDetector(
                    model_dir=config.kws_dir,
                    keywords_file=config.kws_keywords_file,
                    keyword_label=config.wake.keyword_labels,
                    threshold=config.wake.threshold,
                    keywords_score=config.wake.keywords_score,
                    num_trailing_blanks=config.wake.num_trailing_blanks,
                    max_active_paths=config.wake.max_active_paths,
                    provider=device,
                    sample_rate=config.audio.sample_rate,
                )
                worker = KwsWorker(detector)
                worker.start()
                self._compute_models["kws"] = {
                    "runtime": "Sherpa-ONNX（唤醒引擎）",  # runtime（实际加载引擎）。
                    "requested_device": requested,
                    "device": device,
                    "service": "READY",
                    "load_error": (
                        f"CUDA（图形处理器）加载失败，已回退到 {device}：{type(last_error).__name__}: {last_error}"
                        if last_error is not None else None
                    ),
                }
                return worker
            except Exception as exc:
                last_error = exc
        self._compute_models["kws"] = {
            "requested_device": requested,
            "device": None,
            "service": "ERROR",
            "load_error": str(last_error) if last_error else "KWS 初始化失败",
        }
        raise KwsError(str(last_error) if last_error else "KWS 初始化失败") from last_error

    def _record_error(self, service: str, message: str, *, status_service: str | None = None) -> None:
        # Keep the public service map compatible while preserving the actual
        # failing component in startup diagnostics (for example, VAD failure
        # makes the ASR chain unavailable but is not an ASR model failure).
        self.state.set_service(status_service or service, "ERROR")
        self.startup_errors.append(f"{service}: {message}")
        self._service_errors[service] = str(message)
        logger.error("%s", self.startup_errors[-1])

    def _clear_service_error(self, service: str) -> None:
        """_clear_service_error（清除能力错误）：只在能力恢复成功后移除旧原因。"""
        for name in self._CAPABILITY_ERROR_ALIASES.get(service, (service,)):
            self._service_errors.pop(name, None)
            prefix = f"{name}: "
            self.startup_errors = [item for item in self.startup_errors if not item.startswith(prefix)]

    def _disable_optional_search(self, reason: str) -> None:
        safe_reason = " ".join(str(reason or "搜索服务不可用").split())[:240]
        self.state.set_service("web_search", "DISABLED")
        self.startup_errors.append(f"web_search: disabled: {safe_reason or '搜索服务不可用'}")
        logger.warning("%s", self.startup_errors[-1])

    async def initialize(self, *, _session_lock_held: bool = False) -> None:
        """initialize（完整初始化）：独立服务与恢复操作继续使用同一完整路径。"""
        await self.initialize_local()
        if _session_lock_held:
            await self.initialize_session(_session_lock_held=True)
        else:
            await self.initialize_session()

    async def _load_native(self, factory, *args, **kwargs):
        """_load_native（后台原生加载）：保持接口响应，取消时等加载结束后回收结果。"""
        task = asyncio.create_task(asyncio.to_thread(factory, *args, **kwargs))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # 原生构造不能强行中断；避免退出清理后再产生遗漏的关键词线程。
            result = await task
            close = getattr(result, "close", None)
            if close:
                close()
            raise
        if getattr(self, "_application_stopping", False):
            close = getattr(result, "close", None)
            if close:
                close()
            raise asyncio.CancelledError
        return result

    async def _load_measured_model(self, name, factory, *args, **kwargs):
        """加载共享进程模型并记录归属估算，不将进程总量重复分给每个模型。"""
        from ..resources import model_load_footprint
        footprint = None
        def load():
            nonlocal footprint
            with model_load_footprint() as footprint:
                result = factory(*args, **kwargs)
            return result
        result = await self._load_native(load)
        self._compute_models.setdefault(name, {})["resource_footprint"] = footprint
        return result

    async def initialize_local(self) -> None:
        """initialize_local（本地模型初始化）：无需识别或分析进程已就绪。"""
        from ..audio.levels import AudioLevelMeter
        self.audio_meter = AudioLevelMeter(self.state, self.bus,
            playback_active=lambda: self.state.listening and
                getattr(getattr(self.controller, "output", None), "status", "") == "SPEAKING")
        self.audio_meter.start()
        application_report("voice", "初始化音频设备与输入处理")
        self._restore_or_persist_voice_identity()
        self.shadow_product.reset(available=False)
        self._initialize_audio()
        await self._initialize_kws()
        await self._initialize_vad()
        application_report("voice", "初始化声纹工作进程")
        await self._initialize_speaker()
        self._application_local_ready = True

    def _initialize_audio(self) -> None:
        """_initialize_audio（音频采集与输入处理）：能力级恢复可单独重建。"""
        loop = asyncio.get_running_loop()
        try:
            self.capture = AudioCapture(
                loop=loop,
                sample_rate=self.config.audio.sample_rate,
                channels=self.config.audio.channels,
                block_ms=self.config.audio.block_ms,
                input_device=self.config.audio.input_device,
                ring_buffer_ms=self.config.audio.ring_buffer_ms,
                on_audio_frame=lambda pcm: self.audio_meter.feed("input", pcm, self.config.audio.sample_rate, self.config.audio.channels),
            )
            self.state.set_service("audio", "READY")
            self._clear_service_error("audio")
        except Exception as exc:
            self._record_error("audio", str(exc))

        try:
            self.audio_processor = AudioPreprocessor(
                sample_rate=self.config.audio.sample_rate,
                channels=self.config.audio.channels,
                reverse_sample_rate=self.config.tts.sample_rate,
                enabled=self.config.audio.processing_enabled,
                required=self.config.audio.processing_required,
                enable_aec=self.config.audio.echo_cancellation,
                enable_ns=self.config.audio.noise_suppression,
                enable_agc=self.config.audio.automatic_gain_control,
            )
            if self.audio_processor.enabled and self.audio_processor.status in {"ERROR", "DEGRADED"}:
                logger.warning("audio.processing_status status=%s error=%s", self.audio_processor.status, self.audio_processor.error)
            self.state.current.update(
                audio_processing_status=self.audio_processor.status,
                audio_processing_error=self.audio_processor.error or "",
            )
            self._clear_service_error("audio_processing")
        except Exception as exc:
            self._record_error("audio_processing", str(exc), status_service="audio")
            self.state.current.update(
                audio_processing_status="ERROR",
                audio_processing_error=str(exc),
            )

    async def _initialize_kws(self) -> None:
        """_initialize_kws（关键词唤醒）：能力级恢复可单独重建；停用时保持既有诊断口径。"""
        if self.config.wake.enabled:
            try:
                self.kws = await self._load_measured_model("kws", self._build_kws_worker)
                self.state.set_service("kws", "READY")
                self._clear_service_error("kws")
            except Exception as exc:
                self._record_error("kws", str(exc))
        else:
            self.state.set_service("kws", "DISABLED")
            self._compute_models["kws"] = {
                "requested_device": self.config.compute.devices.get("kws", "cpu"),
                "device": None,
                "service": "DISABLED",
                "load_error": None,
            }
            logger.info("wake word disabled; session.start enters the voice turn directly")

    async def _initialize_vad(self) -> None:
        """_initialize_vad（语音活动检测）：归识别链路，失败记为 asr 不可用。"""
        try:
            vad_requested = self.config.compute.devices.get("voice_vad", "cpu")
            self.vad = await self._load_measured_model("voice_vad", FsmnVadDetector,
                model_dir=self.config.vad_model_dir,
                device=vad_requested,
                fallback_device=self.config.compute.fallback_devices.get("voice_vad", "cpu"),
                sample_rate=self.config.audio.sample_rate,
                end_silence_ms=self.config.vad.end_silence_ms,
            )
            self._compute_models["voice_vad"] = {
                "resource_footprint": self._compute_models.get("voice_vad", {}).get("resource_footprint"),
                "runtime": "ONNX Runtime（推理运行时）" if self.vad.backend == "onnx" else "PyTorch（推理框架）",
                "requested_device": vad_requested,
                "device": self.vad.device,
                "service": "READY",
                "load_error": self.vad.load_error,
            }
            self._clear_service_error("vad")
        except Exception as exc:
            self._compute_models["voice_vad"] = {
                "requested_device": self.config.compute.devices.get("voice_vad", "cpu"),
                "device": None,
                "service": "ERROR",
                "load_error": str(exc),
            }
            self._record_error("vad", str(exc), status_service="asr")

    async def initialize_session(self, *, _session_lock_held: bool = False) -> None:
        """initialize_session（会话依赖汇合）：只在模型分支结束后连接并建立调度器。"""
        await self._initialize_asr()
        self.history.screen_capture.configure(self.config.screen_capture)
        await self._initialize_llm()
        await self._initialize_shadow()
        await self._initialize_tts()
        await self._initialize_controller(_session_lock_held=_session_lock_held)

    async def _initialize_asr(self) -> None:
        """_initialize_asr（识别工作进程连接）：能力级恢复可单独重建。"""
        application_report("voice", "连接语音识别与分析服务")
        self.asr = AsrWorkerClient(self.config.asr.worker_url)
        try:
            await self.asr.connect()
            await self.asr.health()
            if self.vad is None:
                # 语音活动检测缺失时识别链路不可用：显式失败，不能停留在“加载中”。
                self._record_error("asr", "语音活动检测未加载，识别链路不可用")
                return
            self.state.set_service("asr", "READY")
            self._clear_service_error("asr")
        except Exception as exc:
            code = getattr(exc, "code", "ASR_WORKER_UNAVAILABLE")
            self._record_error("asr", f"{code}: {exc}")

    async def _initialize_llm(self) -> None:
        """_initialize_llm（主模型客户端）：能力级恢复可单独重建。"""
        application_report("voice", "配置主模型、影子代理与语音合成")
        llm_key = self._api_key(self.config.llm.api_key_env)
        if llm_key:
            self.llm = DeepSeekClient(
                base_url=self.config.llm.base_url,
                model=self.config.llm.model,
                api_key=llm_key,
                provider=self.config.llm.provider,
                api_key_env=self.config.llm.api_key_env,
                session_id=self.conversation.conversation_id,
                timeout_ms=self.config.llm.timeout_ms,
                thinking=self.config.llm.thinking,
                reasoning_effort=self.config.llm.reasoning_effort,
                allowed_reasoning=self.config.llm.allowed_reasoning,
                transport=self.config.llm.transport,
                max_output_tokens=self.config.llm.max_output_tokens,
                protocol_recovery_limit=self.config.llm.protocol_recovery_limit,
                transport_retry_limit=self.config.llm.transport_retry_limit,
            )
            self.state.set_service("llm", "READY")
            self._clear_service_error("llm")
        else:
            self._record_error("llm", f"未配置 {self.config.llm.api_key_env}")

    async def _initialize_shadow(self) -> None:
        """_initialize_shadow（影子代理）：能力级恢复可单独重建。"""
        shadow_cfg = self.config.shadow_agent
        if shadow_cfg.enabled:
            if shadow_cfg.backend == "dsh":
                try:
                    status_provider = None
                    if shadow_cfg.status_observer.enabled:
                        status_cfg = shadow_cfg.status_observer.provider or shadow_cfg.provider
                        status_key = self._api_key(status_cfg.api_key_env)
                        if not status_key:
                            logger.warning(
                                "DSH 状态观察器未启动：缺少凭据环境变量 %s；本地代理桥接仍继续初始化",
                                status_cfg.api_key_env,
                            )
                        else:
                            status_provider = DeepSeekResponsesShadowProvider(
                                base_url=status_cfg.base_url,
                                model=status_cfg.model,
                                api_key=status_key,
                                timeout_ms=status_cfg.timeout_ms,
                                session_id=(self.conversation.conversation_id
                                            if self.config.llm.provider == "opencode_go" else None),
                            )
                    gate_provider = status_provider
                    if gate_provider is None:
                        gate_cfg = shadow_cfg.status_observer.provider or shadow_cfg.provider
                        gate_key = self._api_key(gate_cfg.api_key_env)
                        if gate_key:
                            gate_provider = DeepSeekResponsesShadowProvider(
                                base_url=gate_cfg.base_url,
                                model=gate_cfg.model,
                                api_key=gate_key,
                                timeout_ms=gate_cfg.timeout_ms,
                                session_id=(self.conversation.conversation_id
                                            if self.config.llm.provider == "opencode_go" else None),
                            )
                    self.shadow_session = DshShadowAgentSession(
                        client=self.dsh.bridge,
                        mapping_store=DshMappingStore((self.paths.dsh_home if self.paths.separated else self.config.dsh.home_dir.parent) / "session-mappings.json"),
                        context_source=self.shadow_context,
                        voice_session_id=self.conversation.conversation_id,
                        workspace=self.dsh.repo_root,
                        production_sink=self._on_shadow_events,
                        activity_sink=self._on_shadow_activity,
                        status_provider=status_provider,
                        gate_provider=gate_provider,
                        observer_every_steps=shadow_cfg.status_observer.every_committed_steps,
                        observer_max_output_tokens=shadow_cfg.status_observer.max_output_tokens,
                        observer_timeout_ms=shadow_cfg.status_observer.timeout_ms,
                        availability_waiter=self.dsh.wait_ready,
                        runtime_reminder_interval_ms=shadow_cfg.runtime_reminder_interval_ms,
                    )
                    self.shadow_session.set_context_gate_enabled(
                        shadow_cfg.experimental_context_gate_enabled,
                    )
                    self.configure_dsh_approvals()
                    self.dsh.bridge_hub.register(self.shadow_session)
                    if not self._handoff_marker.exists() and not os.environ.get("VOICE_APP_ORIGIN"):
                        await self.shadow_session.resume_pending_gate()
                    self.history.set_shadow_runtime_state(available=True, active=False)
                    self.shadow_product.reset(available=True)
                    self.state.set_service("shadow_agent", "READY")
                    self.state.set_service("web_search", "DISABLED")
                except Exception as exc:
                    self.state.set_service("shadow_agent", "ERROR")
                    self.state.set_service("web_search", "DISABLED")
                    logger.exception("DSH shadow initialization failed: %s", exc)
            else:
                shadow_key = self._api_key(shadow_cfg.provider.api_key_env)
                if not shadow_key:
                    self.state.set_service("shadow_agent", "DISABLED")
                    logger.warning("shadow agent disabled: missing %s", shadow_cfg.provider.api_key_env)
                else:
                    await self._initialize_native_shadow(shadow_cfg, shadow_key)
        else:
            self.shadow_product.reset(available=False)
            self.state.set_service("shadow_agent", "DISABLED")
            self.state.set_service("web_search", "DISABLED")

    async def _initialize_tts(self) -> bool:
        """_initialize_tts（语音合成路由与播放器）：能力级恢复可单独重建。

        返回本次是否就绪；失败必须落到明确的 ERROR（而不是留下 OFFLINE/STARTING），
        否则宿主与界面无法区分"仍在加载"和"已经失败"，重试页也不会自愈。
        """
        try:
            self.state.set_service("tts", "STARTING")
            await asyncio.to_thread(self._select_available_tts)
            remote_tts, local_tts = self._build_tts_clients()
            self.tts = TtsBackendRouter(remote=remote_tts, local=local_tts, backend=self.config.tts.backend)
            self.state.current.update(
                tts_transport=getattr(self.tts, "transport", "websocket"),
                tts_scheduling=getattr(self.tts, "effective_scheduling", "auto"),
            )
            if self.config.tts.output_format.lower() != "pcm":
                raise RuntimeError("TTS 播放器需要 PCM 输出。")
            if self.config.tts.backend == "voxcpm1":
                if self.config.tts.sample_rate not in LOCAL_TTS_PLAYER_RATES:
                    raise RuntimeError("VoxCPM 播放器 sample_rate（采样率）不受支持")
                self._local_tts_load_state = "loading"
                self.state.set_service("tts", "STARTING")
                await self.tts.ensure_ready()
                # 音色尚未配置时模型仍保持就绪，设置页可以继续导入或设计音色。
                if self._active_voice_snapshot(self.config.tts.local_model) is not None:
                    # 初始化完成前丢弃一次预热音频，避免首笔对话承担冷态计算。
                    await self.tts.prewarm()
                self._local_tts_load_state = "ready"
                self._local_tts_error = None
            else:
                self._local_tts_load_state = "disabled"
            self._ensure_player()
            if self.config.tts.backend == "minimax" and remote_tts is None:
                raise RuntimeError(f"未配置 {self.config.tts.api_key_env}")
            self.state.set_service("tts", "READY")
            self._clear_service_error("tts")
            return True
        except Exception as exc:
            if self.config.tts.backend == "voxcpm1":
                self._local_tts_load_state = "error"
                self._local_tts_error = str(exc)[:500]
            self._record_error("tts", str(exc))
            return False

    def _ensure_player(self) -> None:
        """_ensure_player（播放器复用）：播放器与设备时钟跨能力恢复保持同一实例。

        会话控制器持有播放器引用并挂载播放回调；能力级恢复不能替换实例，
        否则回调与设备时钟会脱离，只有首次初始化才新建。

        本地下行采样率跟随所选本地模型的原生率（VoxCPM1 16 kHz、VoxCPM2 48 kHz）。
        这时**原地**置换设备时钟（`PcmPlayer.reclock`），同样不替换实例——控制器、
        事务输出与语音管线都缓存了这个引用。只在播放器空闲时置换。
        """
        desired = int(self.config.tts.sample_rate)
        pending = self._pending_local_tts_rate
        if pending and self.config.tts.backend != "voxcpm1":
            # 本地下行的延迟换率不能覆盖在线 API 的独立采样率。
            self._pending_local_tts_rate = None
            pending = None
        if pending and pending[1] != desired and self._player_is_idle():
            self._pending_local_tts_rate = None
            previous_rate = desired
            desired = pending[1]
            self.config = replace(self.config, tts=replace(self.config.tts, sample_rate=desired))
            local = getattr(self.tts, "local", None)
            if local is not None and hasattr(local, "sample_rate"):
                local.sample_rate = desired
            logger.info("tts.local_sample_rate_changed local_model=%s previous_rate=%d sample_rate=%d deferred=True",
                        pending[0], previous_rate, desired)
        if self.player is None:
            if self.audio_processor is not None:
                self.audio_processor.reclock_reverse(desired)
            self.player = PcmPlayer(
                playback_permission=lambda: not self.quiet_policy.passive(),
                on_policy_blocked=self._on_passive_playback_blocked,
                sample_rate=self.config.tts.sample_rate,
                channels=1,
                output_device=self.config.audio.output_device,
                device_latency_ms=self.config.audio.device_latency_ms,
                on_audio_rendered=(self.audio_processor.push_reverse if self.audio_processor else None),
                on_audio_frame=lambda pcm: self.audio_meter.feed("playback", pcm, self.config.tts.sample_rate),
            )
        elif self.player.sample_rate != desired and self._player_is_idle():
            self.player.reclock(
                desired,
                on_rate_changed=(self.audio_processor.reclock_reverse if self.audio_processor else None),
            )
        if not self.player.running:
            self.player.start()
        self.state.current["playback_diagnostics"] = self.player.diagnostics

    def _player_is_idle(self) -> bool:
        """_player_is_idle（播放器空闲）：仍有排队或未播完音频时不允许换时钟。"""
        if self.player is None:
            return True
        try:
            return int(self.player.buffered_ms) <= 0
        except Exception:  # noqa: BLE001 - 取不到进度时按"不空闲"处理，避免切坏在播音频。
            return False

    async def _retry_tts(self) -> None:
        """_retry_tts（语音合成恢复）：只重建合成路由，播放器与设备时钟保持不变。"""
        await self._initialize_tts()
        if self.state.services.get("tts") not in SERVICE_READY_STATES:
            return
        # 恢复后重放当前选择：本地模型可能已卸载，需要重新准备并沿用既有音色。
        local = getattr(self.tts, "local", None)
        if local is not None:
            try:
                await local.ensure_ready()
                if self._active_voice_snapshot(self.config.tts.local_model) is not None:
                    await self.tts.prewarm()
            except Exception as exc:
                self._record_error("tts", f"恢复后重新准备失败：{type(exc).__name__}: {exc}")

    def _episode_persona_config(self):
        """返回与产品设置写入位置一致的 Persona（人格资料）读取配置。"""
        persona = self.config.persona
        store = self.product_settings
        if store is None:
            return persona
        return replace(
            persona,
            directory=store.persona_directory,
            personality_file=PERSONA_FILES["personality"],
            user_file=PERSONA_FILES["user"],
        )

    async def _initialize_controller(self, *, _session_lock_held: bool = False) -> None:
        """_initialize_controller（唯一会话调度器）：依赖上游能力全部汇合。"""
        if self.config.audio.sample_rate != 16000 or self.config.audio.channels != 1 or self.config.audio.sample_format != "int16":
            raise ValueError("session runtime requires 16 kHz mono int16 capture")
        if self.product_settings is not None:
            await self.hotwords.configure(lambda: self.product_settings.settings_only().get("hotwords", self.hotwords.bank.settings))
        self.controller = EpisodeController(
            audio_enhancements=self.config.audio_enhancements,
            hotwords=self.hotwords,
            options=self.config.smart_turn, agent_options=self.config.agent,
            persona_config=self._episode_persona_config(),
            custom_instructions_provider=lambda: self._instruction_snapshot,
            continuity_document_provider=lambda: self.continuity.document,
            on_user_activity=self._on_voice_activity,
            on_silence_anchor=self._on_voice_silence_anchor,
            quiet_policy=self.quiet_policy,
            deferred_capture=self._capture_deferred,
            deferred_append=self._append_deferred,
            shadow_waker=self._wake_shadow if self.shadow_session else None,
            shadow_available=self.shadow_session is not None,
            state=self.state,
            bus=self.bus,
            history=self.history,
            capture=self.capture,
            kws=self.kws,
            vad=self.vad,
            asr=self.asr,
            llm=self.llm,
            tts=self.tts,
            player=self.player,
            wake_enabled=self.config.wake.enabled,
            wake_always_on=self.config.wake.enabled,
            wake_keyword=self.config.wake.keyword,
            wake_keywords=self.config.wake.keywords,
            wake_preroll_ms=self.config.audio.wake_preroll_ms,
            vad_min_speech_ms=self.config.vad.min_speech_ms,
            vad_end_silence_ms=self.config.vad.end_silence_ms,
            max_turn_ms=self.config.vad.max_turn_ms,
            bridge_min_chars=self.config.text_bridge.min_chars,
            bridge_max_chars=self.config.text_bridge.max_chars,
            bridge_max_wait_ms=self.config.text_bridge.max_wait_ms,
            bridge_breaks=self.config.text_bridge.preferred_breaks,
            audio_processor=self.audio_processor,
            aec_failure_policy=self.config.audio.aec_failure_policy,
            barge_in_gate=BargeInGate(enabled=True),
            conversation=self.conversation,
            speaker_client=self.speaker,
            speaker_router=self.speaker_router,
            speaker_mode=(self.config.speaker.mode if self.speaker_profile.available else "observe" if self.config.speaker.mode != "off" else "off"),
            speaker_config=self.config.speaker,
            owner_only_barge_in=self.config.speaker.owner_only_barge_in and self.speaker_profile.available,
            speaker_wait_ms=self.config.speaker.wait_ms,
            speaker_window_ms=self.config.speaker.window_ms,
            speaker_min_speech_ms=self.config.speaker.min_speech_ms,
            speaker_min_turn_speech_ms=self.config.speaker.min_turn_speech_ms,
            speaker_min_window_speech_ratio=self.config.speaker.min_window_speech_ratio,
            ambient_store=self.ambient_store,
        )
        self.controller.set_reasoning_mode(self.reasoning_mode)
        self.configure_dsh_approvals()
        application_report("voice", "建立唯一会话调度器")
        await self.controller.initialize()
        if self._handoff_marker.exists():
            async with (_no_session_lock() if _session_lock_held else self._session_lock):
                await self._resume_handoff()
        if self._quiet_clock_task is None or self._quiet_clock_task.done():
            self._quiet_clock_task = asyncio.create_task(self._quiet_clock(), name="quiet-hours-policy")
        self._application_bound_dsh_pid = None
        self._application_session_ready = True
        async with self._product_settings_lock:
            await self._apply_pending_product_settings()

    async def _initialize_native_shadow(self, shadow_cfg: Any, shadow_key: str) -> None:
        try:
                    search_cfg = shadow_cfg.tools.web_search
                    search_key = self._api_key(search_cfg.api_key_env)
                    if search_cfg.enabled and search_key:
                        self.shadow_search_tools = WebSearchTools(MiniMaxSearchClient(
                            base_url=search_cfg.base_url, api_key=search_key,
                            timeout_ms=search_cfg.timeout_ms,
                        ))
                        self.state.set_service("web_search", "READY")
                    else:
                        self.state.set_service("web_search", "DISABLED")
                    files = FileTools(shadow_cfg.tools.allowed_roots) if shadow_cfg.tools.files_enabled else None
                    executor = ShadowToolExecutor(files, self.shadow_search_tools,
                                                   AmbientContextTools(self.ambient_store),
                                                   soft_yield_ms=shadow_cfg.tools.soft_yield_ms,
                                                   on_status=self._on_shadow_tool_status)
                    provider = DeepSeekResponsesShadowProvider(
                        base_url=shadow_cfg.provider.base_url,
                        model=shadow_cfg.provider.model,
                        api_key=shadow_key,
                        timeout_ms=shadow_cfg.provider.timeout_ms,
                        session_id=(self.conversation.conversation_id
                                    if self.config.llm.provider == "opencode_go" else None),
                    )
                    status_cfg = shadow_cfg.status_observer.provider
                    status_provider = provider
                    if status_cfg is not None:
                        status_key = self._api_key(status_cfg.api_key_env)
                        if not status_key:
                            raise RuntimeError(f"未配置状态汇报模型凭据 {status_cfg.api_key_env}")
                        status_provider = DeepSeekResponsesShadowProvider(
                            base_url=status_cfg.base_url, model=status_cfg.model,
                            api_key=status_key, timeout_ms=status_cfg.timeout_ms,
                            session_id=(self.conversation.conversation_id
                                        if self.config.llm.provider == "opencode_go" else None),
                        )
                    self.shadow_component = ShadowAgentComponent(
                        provider, executor, status_provider=status_provider,
                        instructions_loader=load_shadow_instructions,
                        runtime_options={
                            "provider_timeout_ms": shadow_cfg.provider.timeout_ms,
                            "transport_retry_limit": shadow_cfg.provider.transport_retry_limit,
                            "max_context_tokens": shadow_cfg.max_context_tokens,
                            "compaction_threshold_ratio": shadow_cfg.compaction_threshold_ratio,
                            "runtime_reminder_interval_ms": shadow_cfg.runtime_reminder_interval_ms,
                            "observer_enabled": shadow_cfg.status_observer.enabled,
                            "observer_every_steps": shadow_cfg.status_observer.every_committed_steps,
                            "observer_max_output_tokens": shadow_cfg.status_observer.max_output_tokens,
                            "observer_timeout_ms": shadow_cfg.status_observer.timeout_ms,
                            "observer_max_context_tokens": shadow_cfg.status_observer.max_context_tokens,
                            "reasoning_effort": shadow_cfg.reasoning_effort,
                            "allowed_reasoning": shadow_cfg.allowed_reasoning,
                            "on_metric": self._on_shadow_metric,
                        },
                    )
                    self.shadow_session = self.shadow_component.new_session(
                        self.shadow_context,
                        production_sink=self._on_shadow_events,
                        activity_sink=self._on_shadow_activity,
                    )
                    self.shadow_session.set_context_gate_enabled(
                        shadow_cfg.experimental_context_gate_enabled,
                    )
                    self.history.set_shadow_runtime_state(available=True, active=False)
                    self.shadow_product.reset(available=True)
                    self.state.set_service("shadow_agent", "READY")
        except Exception as exc:
            self.state.set_service("shadow_agent", "ERROR")
            self.state.set_service("web_search", "DISABLED")
            logger.exception("shadow agent initialization failed: %s", exc)

    def snapshot(self) -> VoiceEvent:
        value = snapshot_event(self.state)
        value.payload["audio_enhancements_switching"] = bool(getattr(self, "_enhancement_switching", False))
        operations = runtime_operations(self)
        value.payload["runtime_operations"] = operations.registry.snapshot()  # runtime_operations（运行操作状态）。
        value.payload["input_paused"] = bool(operations.holds or operations.restoring)  # input_paused（维护期间暂时停听）。
        value.payload["listening_requested"] = operations.resume_requested if value.payload["input_paused"] else self.state.listening
        local_tts_status = self.local_tts_status_payload()
        value.payload["local_tts"] = local_tts_status  # local_tts（本地模型选择与加载状态）。
        value.payload["session_operation"] = getattr(self, "_session_operation", None)  # session_operation（手动会话操作）。
        if value.payload["session_operation"]:
            value.payload["can_start"] = False
        tts_config = getattr(getattr(self, "config", None), "tts", None)
        operations = getattr(self, "tts_voice_operations", None)
        if ((tts_config is not None and tts_config.backend == "voxcpm1"
             and local_tts_status.get("voice_state") != "ready")
                or (operations is not None and operations.busy)):
            value.payload["can_start"] = False
        if hasattr(self, "quiet_policy"):
            value.payload["quiet_hours"] = self.quiet_policy.snapshot()
        controller = self.controller
        episode = getattr(controller, "episode", None) if controller is not None else None
        if episode is not None:
            try:
                value.payload["agent"] = episode.snapshot(include_history=False)
            except Exception:
                # A live projection is preferable for reconnect recovery, but
                # it must never make subscription less reliable than the
                # existing cached RuntimeState snapshot.
                logger.exception("runtime.live_episode_snapshot_failed")
        value.payload["recent_inputs"] = [record.to_payload() for record in self.history.records]
        # One authoritative, server-ordered conversation: the product rail
        # renders this instead of merging recent_inputs with the episode output
        # projections, and a reload restores the same view.
        conversation_snapshot = getattr(getattr(self.history, "conversation", None), "snapshot", None)
        if callable(conversation_snapshot):
            # Additive projection: fixtures that construct this runtime without
            # a real ledger keep the legacy snapshot shape.
            value.payload["conversation"] = conversation_snapshot()
        value.payload["reasoning_mode"] = self.reasoning_mode
        value.payload["shadow"] = self._shadow_product_snapshot()
        return value

    def local_tts_status_payload(self) -> dict[str, Any]:
        """返回本地模型状态，不向客户端暴露任何本机文件路径。"""
        client = getattr(getattr(self, "tts", None), "local", None)
        tts_config = getattr(getattr(self, "config", None), "tts", None)
        configured = getattr(tts_config, "local_model", "voxcpm1")
        runtime_root = getattr(self, "_runtime_local_root", None)
        status = client.model_status() if callable(getattr(client, "model_status", None)) else (
            AudioCppTtsServer.catalog_snapshot(runtime_root, local_model=configured) if runtime_root else {}
        )
        status_local_model = status.get("local_model") if isinstance(status, Mapping) else None
        loaded_model = status.get("loaded_model") if isinstance(status, Mapping) else None
        model_rows = status.get("models") if isinstance(status, Mapping) else None
        if not isinstance(model_rows, list):
            model_rows = [
                {"id": "voxcpm1", "name": "VoxCPM1", "native_sample_rate": 16000,
                 "available": False, "loaded": False},
                {"id": "voxcpm2", "name": "VoxCPM2", "native_sample_rate": 48000,
                 "available": False, "loaded": False},
            ]
        voice_selection = self._active_voice_selection(configured)
        libraries = getattr(self, "voice_library", None)
        library = libraries.for_model(configured) if libraries else None
        active_voice = None
        voice_state = "unconfigured"
        voice_error = getattr(self, "_local_tts_voice_error", None)
        if library is None:
            voice_state = "invalid"
        if voice_selection is not None and library is not None:
            try:
                voice = library.get_voice(
                    str(voice_selection.get("voice_id") or ""), voice_selection.get("revision"),
                )
                library.asset_path(voice["reference_asset_id"])
                if configured == "voxcpm1" and not voice.get("reference_text", "").strip():
                    voice_state = "reference_text_missing"
                    voice_error = "VoxCPM1（第一代本地语音模型）需要参考音频对应的转写文本。"
                else:
                    voice_state = "ready"
                    voice_error = None
                active_voice = {"voice_id": voice["id"], "revision": voice["revision"],
                                "name": voice["name"]}
            except FileNotFoundError as exc:
                voice_state = "asset_missing"
                voice_error = str(exc)
            except VoiceLibraryError as exc:
                voice_state = "invalid"
                voice_error = str(exc)
        elif voice_selection is not None:
            voice_state = "invalid"
        caps = {
            "voice_clone": True,
            "voice_design": configured == "voxcpm2",
            "style_control": configured == "voxcpm2",
            "emotion_control": False,  # emotion_control（动态情绪控制）已停用。
            "requires_reference_text": configured == "voxcpm1",
        }
        emotion_diagnostic = (
            "VoxCPM1 不支持文字情绪控制；情绪标签不会发送给本地模型。"
            if configured == "voxcpm1" else
            "VoxCPM2 可用参考音频、文字设计或两者共同生成目标样音；播报沿用样音，同一回答内续接上一完整分段。"
        )
        return {
            "backend": getattr(tts_config, "backend", "minimax"),  # backend（当前语音合成后端）。
            "local_model": status_local_model if status_local_model in {"voxcpm1", "voxcpm2"} else configured,
            "loaded_model": loaded_model if loaded_model in {"voxcpm1", "voxcpm2"} else None,
            "load_state": getattr(self, "_local_tts_load_state", "unloaded"),  # load_state（本地模型加载状态）。
            "error": (safe_voice_diagnostic(getattr(self, "_local_tts_error", None), "")
                      if getattr(self, "_local_tts_error", None) else None),  # error（最近一次本地模型错误）。
            "models": model_rows,
            "active_voice": active_voice,
            "voice_state": voice_state,
            "voice_error": safe_voice_diagnostic(voice_error, "") if voice_error else None,
            "voice_capabilities": caps,
            "emotion_diagnostic": emotion_diagnostic,
            "pending_selection": ({"backend": pending["backend"], "local_model": pending["local_model"]}
                                  if isinstance(pending := (getattr(self, "_pending_tts_update", None) or getattr(self, "_applying_tts_selection", None)), Mapping) else None),  # 等待事务结束或正在应用的模型选择。
            "active_voice_by_model": self._active_voices_by_model(),
            "operation": self.tts_voice_operations.current_payload() if getattr(self, "tts_voice_operations", None) else None,
        }

    def _active_voices_by_model(self) -> dict[str, Any]:
        libraries = getattr(self, "voice_library", None)
        return libraries.selections() if libraries else {"voxcpm1": None, "voxcpm2": None}

    def _active_voice_selection(self, model: str) -> Mapping[str, Any] | None:
        libraries = getattr(self, "voice_library", None)
        selected = libraries.for_model(model).active_selection if libraries else None
        return selected if isinstance(selected, Mapping) else None

    def _active_voice_snapshot(self, model: str, *, emotion: str | None = "calm"):
        if self.voice_library is None:
            return None
        selection = self._active_voice_selection(model)
        if selection is None:
            return None
        try:
            return self.voice_library.resolve(
                model, selection,
                emotion=None,
            )
        except (VoiceLibraryError, FileNotFoundError):
            return None

    def _migrate_legacy_default_voice(self) -> None:
        """从已校验的本机种子分别注册两款模型默认项，不复制其他配置。"""
        if self.voice_library is None:
            return
        seed = self._runtime_local_root / "models" / "audiocpp" / "voice-seed"
        audio, text = seed / "default.wav", seed / "default.json"
        if not audio.is_file() or not text.is_file():
            seed = self.paths.program_root / "config/resources/assets/tts"
            audio, text = seed / "default.wav", seed / "default.json"
        if not audio.is_file() or not text.is_file():
            # 兼容实验安装的默认样音；读取旧配置先于重新生成运行配置。
            legacy_config = self._runtime_local_root / "config" / "audiocpp" / "server-tts-evaluation.json"
            try:
                previous = json.loads(legacy_config.read_text(encoding="utf-8"))
                preset = next((item.get("default_voice_preset") for item in previous.get("models", [])
                               if isinstance(item.get("default_voice_preset"), dict)), None)
                if preset and preset.get("voice_ref") and preset.get("reference_text"):
                    legacy_audio = Path(preset["voice_ref"])
                    if legacy_audio.is_file():
                        for library in self.voice_library.libraries.values():
                            library.register_default(legacy_audio.read_bytes(), preset["reference_text"])
                        return
            except (OSError, ValueError, KeyError) as exc:
                self._local_tts_voice_error = safe_voice_diagnostic(exc)
        if audio.is_file() and text.is_file():
            try:
                transcript = json.loads(text.read_text(encoding="utf-8"))["reference_text"]
                for library in self.voice_library.libraries.values():
                    library.register_default(audio.read_bytes(), transcript)
                self._local_tts_voice_error = None
            except (OSError, ValueError, KeyError) as exc:
                self._local_tts_voice_error = safe_voice_diagnostic(exc)

    async def switch_local_tts_model(self, local_model: str) -> None:
        """调用本地 TTS 适配层切换模型并更新加载诊断。"""
        if self.tts is None:
            raise RuntimeError("TTS（语音合成）组件尚未初始化。")
        if not self._player_is_idle():
            raise RuntimeError("播放器音频尚未排空，请等待当前播报结束后切换本地模型。")
        previous_config = self.config
        model_switched = False
        self._local_tts_load_state = "loading"
        self._local_tts_error = None
        try:
            await self.tts.switch_local_model(local_model)
            model_switched = True
            # 模型与设备时钟属于同一切换事务，设备拒绝新率也必须回滚原生模型。
            await self._sync_local_tts_sample_rate(local_model)
        except BaseException as exc:
            if model_switched:
                self.config = previous_config
                try:
                    await self.tts.switch_local_model(previous_config.tts.local_model)
                    local = getattr(self.tts, "local", None)
                    if local is not None:
                        local.sample_rate = previous_config.tts.sample_rate
                    self._pending_local_tts_rate = None
                    self._ensure_player()
                    self._local_tts_load_state = "ready"
                except Exception:
                    self._local_tts_load_state = "error"
                    logger.exception("tts.local_model_clock_rollback_failed")
                self._local_tts_error = str(exc)[:500]
                raise
            client = getattr(self.tts, "local", None)
            status = client.model_status() if callable(getattr(client, "model_status", None)) else {}
            loaded_model = status.get("loaded_model") if isinstance(status, Mapping) else None
            self._local_tts_load_state = "ready" if loaded_model == self.config.tts.local_model else "error"
            self._local_tts_error = str(exc)[:500]
            raise
        self._local_tts_load_state = "ready"
        self._local_tts_error = None

    async def _sync_local_tts_sample_rate(self, local_model: str) -> None:
        """_sync_local_tts_sample_rate（同步下行率）：把本地下行率对齐到模型原生率。

        没有排队音频时立即重建播放器换时钟；正在播报时先记下来，由
        `_ensure_player` 在空闲后落地，避免打断已排队的音频。
        """
        desired = local_tts_native_rate(local_model)
        current = int(self.config.tts.sample_rate)
        if current == desired:
            self._pending_local_tts_rate = None
            return
        if not self._player_is_idle():
            self._pending_local_tts_rate = (local_model, desired)
            logger.info("tts.local_sample_rate_deferred local_model=%s previous_rate=%d sample_rate=%d "
                        "reason=player_busy", local_model, current, desired)
            return
        self._pending_local_tts_rate = None
        self.config = replace(self.config, tts=replace(self.config.tts, sample_rate=desired))
        local = getattr(self.tts, "local", None)
        if local is not None and hasattr(local, "sample_rate"):
            local.sample_rate = desired
        self._ensure_player()
        logger.info("tts.local_sample_rate_changed local_model=%s previous_rate=%d sample_rate=%d",
                    local_model, current, desired)

    async def switch_tts_selection(self, backend: str, local_model: str) -> None:
        """在停止监听时准备语音后端，失败保留原配置；调用方持有会话锁。"""
        if self.tts is None:
            raise RuntimeError("TTS（语音合成）组件尚未初始化。")
        if not self._player_is_idle():
            raise RuntimeError("播放器音频尚未排空，请等待当前播报结束后切换语音后端。")
        if backend == self.config.tts.backend == "voxcpm1":
            await self.switch_local_tts_model(local_model)
            if self._active_voice_snapshot(local_model) is not None:
                await self.tts.prewarm()
            return
        previous_config, previous_router = self.config, self.tts
        next_config = replace(previous_config, tts=replace(
            previous_config.tts, backend=backend, local_model=local_model,
            sample_rate=(local_tts_native_rate(local_model) if backend == "voxcpm1"
                         else previous_config.tts.online_sample_rate),
        ))
        candidate = None
        self.config = next_config
        try:
            remote, local = self._build_tts_clients()
            if backend == "minimax" and remote is None:
                raise RuntimeError("在线 MiniMax TTS（文本转语音）凭据尚未配置，已保留原语音选择。")
            candidate = TtsBackendRouter(remote=remote, local=local, backend=backend)
            await candidate.ensure_ready()
            warmup = (await candidate.prewarm()
                      if backend == "minimax" or self._active_voice_snapshot(local_model) is not None
                      else {"status": "READY", "reason": "voice_not_configured"})
            if backend == "minimax" and warmup.get("reason") == "emotion_unsupported":
                warmup = await candidate.prewarm()
            if warmup.get("status") == "FALLBACK":
                raise RuntimeError(f"新语音后端连接未就绪：{warmup.get('reason') or '预热失败'}")
            self._ensure_player()
        except BaseException:
            self.config = previous_config
            if self.player is not None and self.player.sample_rate != previous_config.tts.sample_rate:
                try:
                    self._ensure_player()
                except Exception:
                    logger.exception("tts.player_clock_rollback_failed")
            if candidate is not None:
                await candidate.shutdown()
            raise
        self.tts = candidate
        if self.controller is not None:
            self.controller.tts = candidate
            # output（发声事务执行器）保存独立路由引用，下一笔播报必须同步切换。
            output = getattr(self.controller, "output", None)
            if output is not None:
                output.tts = candidate
        self.state.current.update(
            tts_transport=candidate.transport, tts_scheduling=candidate.effective_scheduling,
        )
        self._local_tts_load_state = "ready" if backend == "voxcpm1" else "disabled"
        self._local_tts_error = None
        try:
            await previous_router.shutdown()
        except Exception:
            logger.exception("tts.previous_backend_cleanup_failed")

    def tts_switch_busy(self) -> bool:
        """tts_switch_busy（切换屏障）：输入状态不能代表独立播报事务已结算。"""
        if self.tts is not None and self.tts.active_local_sessions:
            return True
        output = getattr(self.controller, "output", None)
        if getattr(output, "continuation_active", False):
            return True
        handle = getattr(output, "handle", None)
        task = getattr(handle, "task", None)
        if task is not None and not task.done():
            return True
        return bool(self.player is not None and self.player.buffered_ms > 0)

    def _publish_conversation_entry(self, entry: dict[str, Any]) -> None:
        """Publish one conversation line from a synchronous commit path."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - sync setup/tests have no loop
            return
        payload = dict(entry)
        turn_id = str(payload.pop("turn_id", "") or "") or None
        task = loop.create_task(
            self.bus.publish(event("conversation.append", turn_id, **payload)),
            name=f"conversation-append-{entry.get('seq')}",
        )
        self._conversation_tasks.add(task)
        task.add_done_callback(self._conversation_tasks.discard)

    async def publish_conversation_snapshot(self) -> None:
        """Re-state the whole ordered conversation (session reset, reinitialize)."""

        conversation = getattr(self.history, "conversation", None)
        bus = getattr(self, "bus", None)
        if conversation is None or bus is None:
            # Fixtures that construct this runtime without __init__ keep the
            # legacy reset behaviour.
            return
        await bus.publish(event("conversation.snapshot", None, **conversation.snapshot()))

    def _shadow_product_snapshot(self) -> dict[str, Any]:
        projection = getattr(self, "shadow_product", None)
        if projection is None:
            # A few compatibility/unit-test fixtures construct SessionRuntime
            # without running __init__.  Product projection is additive and
            # must not make the legacy runtime snapshot less reliable.
            return {
                "available": False,
                "active": False,
                "phase": "unavailable",
                "user_input_required": False,
                "focus": "",
                "summary": "",
                "timeline": [],
                "activities": [],
            }
        runtime_snapshot = None
        session = getattr(self, "shadow_session", None)
        if session is not None:
            try:
                runtime_snapshot = session.snapshot()
            except Exception:
                logger.exception("shadow.product_snapshot_failed")
        value = projection.snapshot(runtime_snapshot)
        value["backend"] = self.config.shadow_agent.backend
        value["model_management"] = (
            "dsh_harness" if self.config.shadow_agent.backend == "dsh" else "voice_terminal"
        )
        return value

    async def publish_shadow_product(self) -> None:
        await self.bus.publish(event("shadow.product", None, **self._shadow_product_snapshot()))

    async def _on_shadow_activity(self, activities: list[dict[str, Any]]) -> None:
        """Publish transient Shadow activities beside the authoritative event relay."""
        activities = [item for item in activities if not str(item.get("state", "")).startswith("observer_status")]
        if not activities:
            return  # 状态摘要及其过期提示均不进入前台活动映射。
        if (
            isinstance(getattr(self, "shadow_session", None), DshShadowAgentSession)
            and any(str(item.get("state") or "") != "failed" for item in activities)
        ):
            self.state.set_service("shadow_agent", "READY")
        projection = getattr(self, "shadow_product", None)
        if projection is not None:
            projection.update_activities(activities)
            session = getattr(self, "shadow_session", None)
            if session is not None:
                projection.update_runtime(session.snapshot())
        for activity in activities:
            await self.bus.publish(event("shadow.activity", None, **dict(activity)))
        await self.publish_shadow_product()

    def stage_product_settings(self, settings: Mapping[str, Any]) -> None:
        self._pending_product_settings = dict(settings)

    async def set_reasoning_mode(self, mode: str) -> bool:
        if mode not in {"fast", "deep"}:
            return False
        async with runtime_operation(self, "reasoning_mode", "推理模式"):
            async with self._session_lock:
                self.reasoning_mode = mode
                if self.controller is not None:
                    self.controller.set_reasoning_mode(mode)
                bus = getattr(self, "bus", None)
                if bus is not None:
                    await bus.publish(self.snapshot())
        return True

    def _api_key(self, environment_name: str) -> str:
        """Read a process credential or a short-lived vault-backed override."""
        return self._credential_overrides.get(environment_name) or self.config.api_key(environment_name)

    def _select_available_tts(self) -> None:
        """校验所选后端；安装与资源发现不得隐式修改用户的播放目标。"""
        current = self.config.tts
        if current.backend == "minimax":
            if not self._api_key(current.api_key_env):
                raise RuntimeError("所选在线 TTS（文本转语音）接口未配置，请填写密钥或显式选择已就绪的本地模型。")
            return
        available = AudioCppTtsServer.available_models(self._runtime_local_root, local_model=current.local_model)
        if current.local_model not in available:
            raise RuntimeError(f"所选 {current.local_model}（本地语音模型）或 audio.cpp（原生语音框架）未就绪，请在组件管理补齐。")
        self.config = replace(self.config, tts=replace(current, sample_rate=local_tts_native_rate(current.local_model)))

    def _build_tts_clients(self):
        """按本机 backend（语音运行后端）配置创建 TTS provider（语音合成供应方）。"""
        remote_tts = None
        local_tts = None
        if self.config.tts.backend == "minimax":
            minimax_key = self._api_key(self.config.tts.api_key_env)
            if minimax_key:
                remote_tts = MiniMaxTtsClient(
                    base_url=self.config.tts.base_url,
                    websocket_path=self.config.tts.websocket_path,
                    model=self.config.tts.model,
                    api_key=minimax_key,
                    output_format=self.config.tts.output_format,
                    sample_rate=self.config.tts.sample_rate,
                    voice_id=self.config.tts.voice_id,
                    timeout_ms=self.config.tts.timeout_ms,
                    transport=self.config.tts.transport,
                    scheduling=self.config.tts.scheduling,
                    subtitle_enable=self.config.tts.subtitle_enable,
                    subtitle_type=self.config.tts.subtitle_type,
                    audio_cache_max_bytes=self.config.tts.audio_cache_max_bytes,
                    cancel_timeout_ms=self.config.tts.cancel_timeout_ms,
                    keepalive_seconds=self.config.tts.keepalive_seconds,
                    emotion_mode=self.config.tts.emotion_mode,
                    default_emotion=self.config.tts.default_emotion,
                    emotion_transport=self.config.tts.emotion_transport,
                    session_reuse=self.config.tts.session_reuse,
                    admission_lead_ms=self.config.tts.admission_lead_ms,
                    sentence_prefetch_ms=self.config.tts.sentence_prefetch_ms,
                    sentence_tick_ms=self.config.tts.sentence_tick_ms,
                    sentence_min_effective_chars=self.config.tts.sentence_min_effective_chars,
                    first_clause_timeout_ms=self.config.tts.first_clause_timeout_ms,
                )
        elif self.config.tts.backend == "voxcpm1":
            local_config = self._runtime_local_root / "config" / "audiocpp" / "server-tts-evaluation.json"
            self._migrate_legacy_default_voice()
            AudioCppTtsServer.write_runtime_config(self._runtime_local_root, local_config)
            local_tts = AudioCppTtsClient(
                AudioCppTtsServer(
                    self._runtime_local_root,
                    local_config,
                    local_model=self.config.tts.local_model,
                ),
                sample_rate=self.config.tts.sample_rate,
                sentence_min_effective_chars=self.config.tts.sentence_min_effective_chars,
                sentence_prefetch_ms=self.config.tts.sentence_prefetch_ms,
                sentence_tick_ms=self.config.tts.sentence_tick_ms,
                first_clause_timeout_ms=self.config.tts.first_clause_timeout_ms,
                local_optimization=self.config.tts.local_optimization,
                acoustic_continuation=self.config.tts.acoustic_continuation,
                gpu_max_performance=self.config.tts.gpu_max_performance,
                voice_library=self.voice_library,
                active_voice_provider=self._active_voice_selection,
                emotion_mode="off",
                inference_steps_by_model=self.config.tts.inference_steps_by_model,
            )
        return remote_tts, local_tts

    def _continuity_document_changed(self, document: str) -> None:
        projector = getattr(self, "shadow_context", None)
        if projector is not None:
            projector.set_continuity_document(document)

    def _restore_or_persist_voice_identity(self) -> None:
        if self.history.journal is None:
            self.history.attach_storage(self.paths.history / "archives/.journals",
                                        self._history_cache_bytes, resume=True)
        self.paired_archives.streaming = True
        saved = self.history.session_state
        if saved.get("voice") is not None:
            self.conversation.restore(saved["voice"])
            if saved.get("shadow_environment") is not None:
                self.shadow_context.restore_environment(saved["shadow_environment"])
            self._persist_voice_identity()
            return
        try:
            value = json.loads(self._voice_identity_path.read_text(encoding="utf-8"))
            session_id = value.get("voice_session_id") if isinstance(value, dict) else None
            if isinstance(session_id, str) and session_id.startswith(("conversation-", "conversation_")):
                self.conversation.conversation_id = session_id
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("voice.session_identity_read_failed")
        self._persist_voice_identity()

    def _persist_voice_identity(self) -> None:
        config = getattr(self, "config", None)
        if config is None or not hasattr(self, "_voice_identity_path"):
            return
        path = self._voice_identity_path
        self.history.session_state = {**self.history.session_state, "voice": self.conversation.persistence_snapshot(),
                                      "shadow_environment": self.shadow_context.environment_snapshot()}
        self.history.persist_state()
        if self.history.journal is not None and not self.history.journal.flush():
            raise OSError("当前语音会话尚未完整落盘，保留原会话和恢复状态")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps({
            "schema_version": 1,
            "voice_session_id": self.conversation.conversation_id,
        }), encoding="utf-8")
        os.replace(temporary, path)

    def _record_task_delivery(self, occurrence_id: str, receipt: dict[str, Any]) -> None:
        self._task_deliveries[occurrence_id] = receipt
        if len(self._task_deliveries) > 4096:
            self._task_deliveries = dict(list(self._task_deliveries.items())[-4096:])
        path = self._task_delivery_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"schema_version": 1, "receipts": self._task_deliveries},
                                        ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def _on_voice_activity(self) -> None:
        self._voice_silence_anchor = None
        self.continuity.user_started()

    def _on_voice_silence_anchor(self, anchor: float) -> None:
        self._voice_silence_anchor = float(anchor)
        if not self._dsh_busy and not self._cleanup_in_progress:
            self.continuity.arm(anchor, self._continuity_snapshot)

    async def note_dsh_activity(self, payload: dict[str, Any]) -> str:
        session = self.shadow_session
        if (not isinstance(session, DshShadowAgentSession)
                or payload.get("dsh_session_id") != session.dsh_session_id
                or payload.get("epoch") != session.epoch):
            return "stale"
        state = str(payload.get("state") or "")
        if state != "idle":
            self._dsh_busy = True
            self.continuity.user_started()
        else:
            self._dsh_busy = False
            anchor = self._voice_silence_anchor
            controller = self.controller
            if anchor is None and not bool(getattr(getattr(controller, "output", None), "busy", False)):
                anchor = time.time()
                self._voice_silence_anchor = anchor
            if anchor is not None and not self._cleanup_in_progress:
                self.continuity.arm(max(anchor, time.time()), self._continuity_snapshot)
        return "accepted"

    def _continuity_snapshot(self) -> list[dict[str, Any]]:
        """Copy the complete model-visible window context before ledger reset."""

        projector = getattr(self.history, "context_messages", None)
        if not callable(projector):
            return []
        return [
            {
                "role": str(message.get("role") or "system"),
                "context_lane": str(message.get("context_lane") or "history"),
                "content": str(message.get("content") or ""),
            }
            for message in projector("")
            if isinstance(message, dict)
        ]

    def _resolve_model_config(self, payload: Mapping[str, Any]) -> tuple[AppConfig, dict[str, str]]:
        """Resolve a registry payload into an immutable runtime configuration."""

        voice = payload.get("voice") if isinstance(payload.get("voice"), Mapping) else {}
        shadow = payload.get("shadow") if isinstance(payload.get("shadow"), Mapping) else {}
        status = payload.get("status") if isinstance(payload.get("status"), Mapping) else {}
        tts = payload.get("tts") if isinstance(payload.get("tts"), Mapping) else {}
        voice_provider = voice.get("provider") if isinstance(voice.get("provider"), Mapping) else {}
        shadow_provider = shadow.get("provider") if isinstance(shadow.get("provider"), Mapping) else {}
        status_provider = status.get("provider") if isinstance(status.get("provider"), Mapping) else {}
        tts_provider = tts.get("provider") if isinstance(tts.get("provider"), Mapping) else {}
        # ``responses`` is the product/registry protocol label for an
        # OpenAI-Responses-compatible endpoint.  The existing LLM client uses
        # ``openai_compatible`` as its provider identity and selects the
        # Responses transport separately, so normalize it at this boundary.
        allowed_llm = {
            "deepseek", "responses", "opencode_go", "openai_compatible",
            "manual", "custom",
        }
        llm_providers = (voice_provider, status_provider) + (
            (shadow_provider,) if self.config.shadow_agent.backend == "native" else ()
        )
        if any(provider.get("protocol") not in allowed_llm for provider in llm_providers):
            raise ValueError("所选 LLM 供应商尚未安装兼容的运行时适配器。")
        if tts_provider.get("protocol") != "minimax":
            raise ValueError("所选 TTS 供应商尚未安装兼容的运行时适配器。")
        profile = tts.get("profile") if isinstance(tts.get("profile"), Mapping) else {}
        if str(profile.get("output_format") or "pcm").lower() != "pcm":
            raise ValueError("当前原生播放器仅支持 PCM TTS 输出。")
        online_sample_rate = int(profile.get("sample_rate") or self.config.tts.online_sample_rate)  # online_sample_rate（当前 TTS profile 的在线采样率）。

        previous_config = self.config
        voice_env = "VOICE_PRODUCT_VOICE_LLM_KEY"
        shadow_env = "VOICE_PRODUCT_SHADOW_LLM_KEY"
        status_env = "VOICE_PRODUCT_STATUS_LLM_KEY"
        tts_env = "VOICE_PRODUCT_TTS_KEY"
        overrides: dict[str, str] = {}
        credential_rows = [(voice, voice_env), (status, status_env), (tts, tts_env)]
        if self.config.shadow_agent.backend == "native":
            credential_rows.append((shadow, shadow_env))
        for item, environment_name in credential_rows:
            api_key = item.get("api_key")
            if isinstance(api_key, str) and api_key:
                overrides[environment_name] = api_key

        shadow_settings = shadow.get("settings") if isinstance(shadow.get("settings"), Mapping) else {}
        status_settings = status.get("settings") if isinstance(status.get("settings"), Mapping) else {}
        voice_settings = voice.get("settings") if isinstance(voice.get("settings"), Mapping) else {}
        raw_voice_protocol = str(voice_provider.get("protocol") or "")
        runtime_voice_provider = (
            "openai_compatible"
            if raw_voice_protocol in {"responses", "manual", "custom"}
            else raw_voice_protocol
        )
        voice_transport = (
            "responses" if raw_voice_protocol in {"deepseek", "responses", "opencode_go"}
            else "chat_completions" if raw_voice_protocol in {"manual", "custom"}
            else previous_config.llm.transport
        )
        previous_status_provider = (
            previous_config.shadow_agent.status_observer.provider
            or previous_config.shadow_agent.provider
        )
        next_status_provider = replace(
            previous_status_provider,
            base_url=str(status_provider.get("base_url") or previous_status_provider.base_url),
            model=str(status.get("model_id") or previous_status_provider.model),
            api_key_env=status_env if status_env in overrides else previous_status_provider.api_key_env,
        )
        next_shadow_provider = previous_config.shadow_agent.provider
        if previous_config.shadow_agent.backend == "native":
            next_shadow_provider = replace(
                previous_config.shadow_agent.provider,
                base_url=str(shadow_provider.get("base_url") or previous_config.shadow_agent.provider.base_url),
                model=str(shadow.get("model_id") or previous_config.shadow_agent.provider.model),
                api_key_env=shadow_env if shadow_env in overrides else previous_config.shadow_agent.provider.api_key_env,
            )
        next_shadow = replace(
            previous_config.shadow_agent,
            provider=next_shadow_provider,
            max_context_tokens=int(
                (shadow_settings.get("max_context_tokens") if previous_config.shadow_agent.backend == "native" else None)
                or (shadow.get("max_context_tokens") if previous_config.shadow_agent.backend == "native" else None)
                or previous_config.shadow_agent.max_context_tokens
            ),
            reasoning_effort="high",
            allowed_reasoning=tuple(
                shadow.get("allowed_reasoning")
                or previous_config.shadow_agent.allowed_reasoning
            ),
            status_observer=replace(
                previous_config.shadow_agent.status_observer,
                provider=next_status_provider,
                max_context_tokens=int(
                    status_settings.get("max_context_tokens")
                    or previous_config.shadow_agent.status_observer.max_context_tokens
                ),
                reasoning_effort="none",
            ),
        )
        next_config = replace(
            previous_config,
            agent=replace(
                previous_config.agent,
                max_context_tokens=int(
                    voice_settings.get("max_context_tokens")
                    or voice.get("max_context_tokens")
                    or previous_config.agent.max_context_tokens
                ),
            ),
            llm=replace(
                previous_config.llm,
                provider=runtime_voice_provider,
                base_url=str(voice_provider.get("base_url")),
                model=str(voice.get("model_id")),
                api_key_env=voice_env if voice_env in overrides else previous_config.llm.api_key_env,
                transport=voice_transport,
                reasoning_effort=str(
                    voice_settings.get("reasoning_effort")
                    or previous_config.llm.reasoning_effort
                ),
                allowed_reasoning=tuple(
                    voice.get("allowed_reasoning")
                    or previous_config.llm.allowed_reasoning
                ),
            ),
            shadow_agent=next_shadow,
            tts=replace(
                previous_config.tts,
                backend="minimax",
                provider="minimax",
                base_url=str(tts_provider.get("base_url")),
                model=str(tts.get("model_id")),
                api_key_env=tts_env if tts_env in overrides else previous_config.tts.api_key_env,
                voice_id=str(profile.get("voice_id") or previous_config.tts.voice_id),
                emotion_mode=str(profile.get("emotion_mode") or previous_config.tts.emotion_mode),
                default_emotion=str(profile.get("default_emotion") or previous_config.tts.default_emotion),
                output_format="pcm",
                sample_rate=online_sample_rate,
                online_sample_rate=online_sample_rate,
                transport=str(profile.get("transport") or previous_config.tts.transport),
                scheduling=str(profile.get("scheduling") or previous_config.tts.scheduling),
                subtitle_enable=bool(profile.get("subtitle_enable", previous_config.tts.subtitle_enable)),
                subtitle_type=str(profile.get("subtitle_type") or previous_config.tts.subtitle_type),
            ),
        )
        return next_config, overrides

    async def restore_model_services(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Restore a committed assignment before audio/agent initialization."""

        async with self._session_lock:
            previous_config = self.config
            previous_overrides = dict(self._credential_overrides)
            try:
                next_config, overrides = self._resolve_model_config(payload)
                self.config = next_config
                self._credential_overrides = overrides
                self.state.wake_always_on = bool(next_config.wake.enabled)
                return {"ok": True, "restored": True}
            except Exception as exc:
                self.config = previous_config
                self._credential_overrides = previous_overrides
                return {"ok": False, "message": f"已保存的模型分配无法恢复：{type(exc).__name__}。"}

    async def restart_model_services(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Rebuild compatible LLM, Shadow and TTS components as one safe unit.

        A controller owns cancellation and output lifetimes, so changing client
        references in place would leave an active episode using old providers.
        The prior configuration is rebuilt if the new one cannot start.
        """
        async with self._session_lock:
            previous_config = self.config
            previous_overrides = dict(self._credential_overrides)
            previous_listening = self.state.listening
            # Validate and construct the immutable replacement before closing
            # anything.  A malformed product payload is a request-level
            # failure, not a reason to interrupt the currently healthy voice
            # session and then rebuild it for no reason.
            try:
                next_config, overrides = self._resolve_model_config(payload)
            except Exception as exc:
                logger.warning("model_registry.restart_validation_failed error=%s", type(exc).__name__)
                return {
                    "ok": False,
                    "message": f"模型分配校验失败：{type(exc).__name__}；已保留上一份有效分配。",
                }
            try:
                await self.close()
                self._prepare_for_reinitialize()
                self.config, self._credential_overrides = next_config, overrides
                await self.initialize()
                required_services = {"llm", "tts"}
                if next_config.shadow_agent.enabled:
                    required_services.add("shadow_agent")
                unavailable = [
                    name for name in sorted(required_services)
                    if self.state.services.get(name) != "READY"
                ]
                if unavailable:
                    raise RuntimeError(
                        "模型服务未就绪：" + ", ".join(unavailable)
                    )
                if previous_listening and self.controller is not None:
                    await self.controller.start_listening()
                await self.bus.publish(self.snapshot())
                return {"ok": True, "restarted": ["llm", "shadow_agent", "status_provider", "tts"]}
            except Exception as exc:
                logger.exception("model_registry.restart_failed")
                try:
                    await self.close()
                    self._prepare_for_reinitialize()
                    self.config, self._credential_overrides = previous_config, previous_overrides
                    await self.initialize()
                    if previous_listening and self.controller is not None:
                        await self.controller.start_listening()
                except Exception:
                    logger.exception("model_registry.rollback_failed")
                return {"ok": False, "message": f"模型服务重启失败：{type(exc).__name__}；已尝试恢复上一份有效分配。"}

    def _prepare_for_reinitialize(self) -> None:
        """Clear closed component references before a controlled rebuild."""

        self.state.clear_current()
        self._closed = False
        self.state.agent = {}
        self.state.input_owner = "none"
        self.state.set_state(TerminalState.STARTING, listening=False)

    async def start_listening(self) -> None:
        if getattr(self, "_closed", False) or getattr(self, "_application_stopping", False):
            return
        operations = runtime_operations(self)
        if operations.holds:
            operations.intent(True)
            await operations.publish()
            return
        if getattr(self, "_enhancement_switching", False):
            self._enhancement_resume_requested = True
            return
        if getattr(self, "_pending_tts_update", None) or getattr(self, "_applying_tts_selection", None):
            self._tts_resume_requested = True  # 用户主动请求切换后继续监听。
            await self.bus.publish(event("status", code="TTS_SWITCH_PENDING", message="语音模型切换完成后开始监听。"))
            return
        if self.application_initializing:
            await self.bus.publish(event("error", code="RUNTIME_STARTING", message="组件仍在加载，请等待启动完成。", recoverable=True))
            return
        reason = self.state.can_start_reason
        if reason is not None and reason["code"] == "CAPABILITY_LOADING":
            # 能力加载中：暂时拦截主监听并记住意图，加载完成后自动开始，不进入报错页面。
            self._listen_resume_requested = True
            await self.bus.publish(event("status", code="CAPABILITY_LOADING",
                message=f"组件加载中：{reason['message']}。加载完成后将自动开始监听。"))
            return
        self._listen_resume_requested = None  # 成功进入启动路径后撤销更早的挂起意图。
        self._tts_resume_requested = None  # 新的主动启动撤销更早的停止意图。
        async with self._session_lock:
            if getattr(self, "_closed", False) or getattr(self, "_application_stopping", False):
                return
            if operations.holds:
                operations.intent(True)
                return
            voice_operations = getattr(self, "tts_voice_operations", None)
            operation = voice_operations.current_payload() if voice_operations is not None else None
            if operation and operation.get("kind") in {"preview", "play"}:
                await self.bus.publish(event("error", code="TTS_VOICE_BUSY", message="请等待音色试音结束或取消后，再开始监听。", recoverable=True))
                return
            if getattr(self, "_speaker_captures", {}):
                await self.bus.publish(event("error", code="SPEAKER_INPUT_BUSY", message="请结束声纹采集后再开始语音会话。", recoverable=True))
                return
            if self._handoff_marker.exists():
                try:
                    await self._resume_handoff()
                except Exception as exc:
                    await self._handoff_failed(exc)
                    return
            self.continuity.user_started()
            if not await self._apply_pending_product_settings():
                await self.bus.publish(self.snapshot())
                return
            if self.controller is None:
                await self.bus.publish(event("error", code="RUNTIME_NOT_INITIALIZED", message="Voice Runtime 尚未初始化。", recoverable=False))
                return
            if self.state.wake_always_on and (
                self.kws is None or self.kws.error is not None or not self.kws.running
            ):
                if self.kws is not None:
                    self.kws.close()
                try:
                    self.kws = self._build_kws_worker()
                    self.controller.kws = self.kws
                    self.state.set_service("kws", "READY")
                except Exception as exc:
                    self.kws = None
                    self.controller.kws = None
                    self._record_error("kws", str(exc))
                    await self.bus.publish(event(
                        "error", code="KWS_WORKER_FAILED", message=str(exc), recoverable=True,
                    ))
                    await self.bus.publish(self.snapshot())
                    return
            if not self.state.listening:
                self._approval_listen_generation = getattr(self, "_approval_listen_generation", 0) + 1
            if getattr(self, "_closed", False):
                return
            if operations.holds:
                operations.intent(True)
                return
            await self.controller.start_listening()
            pending_buffer = getattr(self, "_shadow_pending", None)
            pending = pending_buffer.drain() if pending_buffer is not None else []
            if pending:
                await self.controller.accept_shadow_events(pending)
            await self._deliver_voice_mailbox()

    async def stop_listening(self) -> None:
        runtime_operations(self).intent(False)
        self._enhancement_resume_requested = False
        self._tts_resume_requested = False  # 用户主动停止优先于后台切换的恢复意图。
        self._listen_resume_requested = False  # 用户主动停止也撤销能力加载中的挂起意图。
        async with self._session_lock:
            if isinstance(self.shadow_session, DshShadowAgentSession):
                self.shadow_session.approvals.revoke()
            if self.controller is not None:
                await self.controller.stop_listening()

    async def _rotate_session(self, *, _lock_held: bool = False, _strict_shadow: bool = False,
                              target_voice_session_id: str | None = None,
                              target_dsh_session_id: str | None = None) -> None:
        async with (self._session_lock if not _lock_held else _no_session_lock()):
            continuity = getattr(self, "continuity", None)
            capture_snapshot = getattr(continuity, "capture_before_reset", None)
            if callable(capture_snapshot):
                capture_snapshot(self._continuity_snapshot())
            if self.controller is not None and hasattr(self.controller, "reset_session"):
                await self.controller.reset_session()
            shadow_session = getattr(self, "shadow_session", None)
            if shadow_session is not None:
                if isinstance(shadow_session, DshShadowAgentSession):
                    await shadow_session.reset(strict=_strict_shadow)
                    if target_dsh_session_id:
                        shadow_session.dsh_session_id = target_dsh_session_id
                else:
                    await shadow_session.reset()
            pending_buffer = getattr(self, "_shadow_pending", None)
            if pending_buffer is not None:
                pending_buffer.drain()
            self.history.reset()
            # The ordered conversation starts over with the session; clients
            # adopt this snapshot instead of merging across the boundary.
            await self.publish_conversation_snapshot()
            ambient_store = getattr(self, "ambient_store", None)
            if ambient_store is not None:
                ambient_store.clear()
            product_settings = getattr(self, "product_settings", None)
            if product_settings is not None:
                shadow_user_profile = product_settings.read_persona().get("user", "")
                self._instruction_snapshot = product_settings.settings_only().get("custom_instructions", [])
            else:
                config = getattr(self, "config", None)
                persona = getattr(config, "persona", None)
                if persona is None:
                    shadow_user_profile = getattr(self, "_shadow_user_profile", "")
                else:
                    try:
                        shadow_user_profile = (persona.directory / persona.user_file).read_text(
                            encoding="utf-8",
                        )
                    except OSError:
                        shadow_user_profile = ""
            self._shadow_user_profile = shadow_user_profile
            shadow_context = ShadowContextProjector(
                self.history, user_profile=self._shadow_user_profile,
                continuity_document=str(getattr(continuity, "document", "") or ""),
                custom_instructions=shadow_projection(getattr(self, "_instruction_snapshot", [])),
            )
            self.shadow_context = shadow_context
            if shadow_session is not None:
                shadow_session.replace_context_source(shadow_context)
                reload_instructions = getattr(shadow_session, "reload_instructions", None)
                if callable(reload_instructions):
                    reload_instructions()
            projection = getattr(self, "shadow_product", None)
            if projection is not None:
                projection.reset(available=shadow_session is not None and shadow_session.available)
            self.conversation.reset()
            if target_voice_session_id:
                self.conversation.conversation_id = target_voice_session_id
            self._persist_voice_identity()
            if isinstance(shadow_session, DshShadowAgentSession):
                self.dsh.bridge_hub.unregister(shadow_session)
                shadow_session.rotate_voice_session_id(self.conversation.conversation_id)
                self.dsh.bridge_hub.register(shadow_session)
            # Reset rotates the conversation but must not silently discard the
            # mode explicitly selected by the client.  The next Episode is
            # rebuilt below (or on the next session start) from this staged
            # mode, keeping its static prompt/schema cache contract intact.
            if self.controller is not None:
                self.controller.set_reasoning_mode(self.reasoning_mode)
            if self.llm is not None:
                self.llm.set_session_id(self.conversation.conversation_id)
            shadow_provider = getattr(getattr(self, "shadow_component", None), "provider", None)
            set_shadow_session = getattr(shadow_provider, "set_session_id", None)
            if callable(set_shadow_session):
                set_shadow_session(
                    self.conversation.conversation_id
                    if self.config.llm.provider == "opencode_go" else None
                )
            bus = getattr(self, "bus", None)
            if bus is not None:
                await bus.publish(self.snapshot())

    async def close(self, *, preserve_tts_updates: bool = False) -> None:
        runtime_operations(self).intent(False)
        self._enhancement_resume_requested = False
        self._closed = True
        compute_switches = tuple(getattr(self, "_compute_switch_tasks", {}).items())  # compute_switches（退出前需回收的设备切换任务）。
        for task, _ticket in compute_switches:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        if compute_switches:
            await asyncio.gather(*(task for task, _ticket in compute_switches), return_exceptions=True)
            # Usually the operation context releases each reservation itself.
            # This also covers cancellation before a scheduled task first runs.
            for _task, ticket in compute_switches:
                if not ticket.released:
                    try:
                        await runtime_operations(self).finish(ticket, asyncio.CancelledError())
                    except Exception:
                        logger.exception("计算设备切换关闭时释放运行租约失败")
        update_task = getattr(self, "_tts_update_task", None)
        if not preserve_tts_updates and update_task is not None and update_task is not asyncio.current_task() and not update_task.done():
            update_task.cancel()
            await asyncio.gather(update_task, return_exceptions=True)
        if not preserve_tts_updates:
            self._pending_tts_update = None
            self._tts_resume_requested = False
        self._application_session_ready = False
        self._application_local_ready = False
        async def cleanup(label, action):
            """单项关闭失败记入日志，继续清理其余资源和运行时引用。"""
            try:
                result = action()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("runtime.cleanup_failed component=%s", label)
        await cleanup("声纹采集", self.close_speaker_captures)
        if getattr(self, "audio_meter", None) is not None:
            await cleanup("音频电平", self.audio_meter.close)
        if self.controller is not None:
            await cleanup("会话调度器", self.controller.close)
        await cleanup("热词", self.hotwords.clear)
        speaker = getattr(self, "speaker", None)
        if speaker is not None:
            await cleanup("声纹", speaker.close)
        await cleanup("声纹工作进程", self._stop_managed_speaker_worker)
        if self.kws is not None:
            await cleanup("唤醒", self.kws.close)
        shadow_component = getattr(self, "shadow_component", None)
        if shadow_component is not None:
            await cleanup("影子代理", shadow_component.close)
        shadow_session = getattr(self, "shadow_session", None)
        if shadow_component is None and isinstance(shadow_session, DshShadowAgentSession):
            self.dsh.bridge_hub.unregister(shadow_session)
            await cleanup("影子会话", shadow_session.close)
        await cleanup("音色操作", self.tts_voice_operations.close)
        if self.tts is not None:
            shutdown = getattr(self.tts, "shutdown", self.tts.close)
            await cleanup("语音合成", shutdown)
        if self.llm is not None:
            await cleanup("语言模型", self.llm.close)
        if self.audio_processor is not None:
            await cleanup("音频处理", self.audio_processor.close)
        # A model apply deliberately calls close() followed by initialize().
        # Do not let a disabled replacement accidentally reuse closed LLM,
        # TTS, KWS, Shadow or audio objects through the next controller.
        self.controller = None
        self.capture = None
        self.kws = None
        self.vad = None
        self.asr = None
        self.llm = None
        self.tts = None
        self.player = None
        self._pending_local_tts_rate = None
        self.audio_processor = None
        self.speaker = None
        self._speaker_engine = None
        self._speaker_enrollment = {}
        self._speaker_test_result = None
        self.shadow_search_tools = None
        self.shadow_component = None
        self.shadow_session = None

    async def close_background_services(self) -> None:
        """Stop project-owned timers when the Voice Server process exits."""

        self.hotwords.close()

        if self._quiet_clock_task is not None:
            self._quiet_clock_task.cancel()
            await asyncio.gather(self._quiet_clock_task, return_exceptions=True)
        await asyncio.gather(
            self.continuity.close(),
            return_exceptions=True,
        )
        if not await asyncio.to_thread(self.history.close_storage):
            logger.error("会话记录尚未完整落盘，保留待写材料：%s", self.history.journal.error)

    async def _wake_shadow(self, *, turn_id: str | None = None) -> str:
        if self.shadow_session is None or not self.shadow_session.available:
            return "unavailable"
        try:
            if isinstance(self.shadow_session, DshShadowAgentSession):
                current_turn, user_text = self._approval_user_input()
                if turn_id and turn_id == current_turn:
                    await self.shadow_session.respond_to_voice_approval(turn_id, user_text)
            return await self.shadow_session.request_wake()
        except DshBridgeError as exc:
            revision = int(getattr(self.shadow_context, "shadow_input_revision", 0))
            await self._on_shadow_events([ShadowEvent(
                event_id=str(uuid4()), kind="failed",
                text=f"DSH Shadow 基础设施失败：{exc}",
                based_on_context_revision=revision,
                failure_code="dsh_bridge_unavailable",
                infrastructure=True,
            )])
            self.state.set_service("shadow_agent", "ERROR")
            return "unavailable"

    def _approval_user_input(self) -> tuple[str, str]:
        """只取主模型权威用户轮次；旧回合、草稿和旁路转写不能代替授权答复。"""
        episode = getattr(self.controller, "episode", None)
        turn_id = str(getattr(episode, "last_turn_id", "") or "")
        current = getattr(episode, "inputs", {}).get(turn_id, {})
        return turn_id, (str(current.get("refined_text") or current.get("text") or "") if current.get("final") else "")

    def _approval_window(self) -> tuple | None:
        """approval window（授权窗口）：停止、关键词回归或会话旋转后身份立即失效。"""
        controller = self.controller
        if controller is None or not self.state.listening or self.state.input_owner != "asr":
            return None
        return (self.conversation.conversation_id, id(getattr(controller, "episode", None)),
                getattr(controller, "_activation_generation", 0),
                getattr(self, "_approval_listen_generation", 0))

    def _approval_spoken_context(self, turn_id: str | None = None) -> tuple[int, str]:
        # 只允许已在当前用户作答前入账的播报；同轮刚生成的问句不能反向授权。
        entries = self.history.conversation.entries
        before_seq = next((int(item["seq"]) for item in entries
                           if item.get("kind") == "user_turn" and item.get("turn_id") == turn_id), None) if turn_id else None
        if turn_id and before_seq is None:
            return 0, ""
        for item in reversed(entries):
            if item.get("kind") != "assistant_speech":
                continue
            if turn_id and (int(item["seq"]) >= before_seq or item.get("turn_id") == turn_id):
                continue
            delivery = item.get("delivery") or {}
            text = str(delivery.get("heard_text") or "")
            if not text and delivery.get("status") == "full":
                text = str(item.get("text") or "")
            if text:
                return int(item["seq"]), text[-2000:]
        return 0, ""

    def configure_dsh_approvals(self) -> None:
        session = self.shadow_session
        if not isinstance(session, DshShadowAgentSession):
            return
        preferences = self.product_settings.settings_only().get("approvals", {}) if self.product_settings is not None else {}
        session.approvals.window_provider = self._approval_window
        session.approvals.input_provider = self._approval_user_input
        session.approvals.dialogue_provider = self._approval_spoken_context
        session.approvals.configure(preferences.get("voice_confirmation_enabled") is True,
                                    preferences.get("continuous_enabled") is True)
        if self.controller is not None:
            self.controller.approval_scope_changed = self._approval_scope_changed

    async def _approval_scope_changed(self) -> None:
        session = self.shadow_session
        if not isinstance(session, DshShadowAgentSession):
            return
        window = self._approval_window()
        if window == getattr(session, "_observed_approval_window", None):
            return
        session._observed_approval_window = window
        session.approvals.continuous_active  # 读取权威边界并立即撤销过期窗口。
        await session.notify_voice_approval()
        await self.publish_shadow_product()

    async def set_shadow_context_gate_enabled(self, enabled: bool) -> bool:
        """Apply the experimental timing gate without restarting the session."""
        value = bool(enabled)
        previous = self.config.shadow_agent.experimental_context_gate_enabled
        old_config = self.config
        self.config = replace(
            self.config,
            shadow_agent=replace(
                self.config.shadow_agent,
                experimental_context_gate_enabled=value,
            ),
        )
        try:
            if self.shadow_session is not None:
                self.shadow_session.set_context_gate_enabled(value)
                if not value and isinstance(self.shadow_session, DshShadowAgentSession):
                    await self.shadow_session.flush_context_gate()
        except Exception:
            self.config = old_config
            if self.shadow_session is not None:
                self.shadow_session.set_context_gate_enabled(previous)
            raise
        return value

    async def set_speaker_settings(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Apply optional speaker mode/thresholds without restarting audio."""
        previous_mode = self.config.speaker.mode
        previous_owner_only = self.config.speaker.owner_only_barge_in
        mode = str(settings.get("mode") or "off")
        owner_only = bool(settings.get("owner_only_barge_in", self.config.speaker.owner_only_barge_in))
        confirm = float(settings.get("confirm_threshold", self.config.speaker.confirm_threshold))
        continuity = float(settings.get("continuity_threshold", self.config.speaker.continuity_threshold))
        relaxation = float(settings.get("continuity_relaxation", min(0.20, max(0.0, confirm - continuity))))
        from ..speaker.settings import validate_tuning
        next_speaker = replace(
            self.config.speaker,
            mode=mode,
            owner_only_barge_in=owner_only,
            confirm_threshold=confirm,
            continuity_threshold=continuity,
            continuity_relaxation=relaxation,
            **validate_tuning({**vars(self.config.speaker), **settings}, continuity=max(0.0, confirm-relaxation)),
        )
        if mode == "filter" and previous_mode != "filter" and not self.speaker_profile.available:
            raise ValueError("未注册声纹前不能启用筛选模式")
        if mode == "filter" and self.speaker_profile.available:
            if not await self.ensure_speaker_worker():
                raise ValueError("Speaker Worker 不可用，暂时不能启用筛选模式")
        elif owner_only and self.speaker_profile.available:
            # The API checks prerequisites when enabling.  A profile removed
            # later leaves the selected toggle intact but ineffective.
            await self.ensure_speaker_worker()
        self.config = replace(self.config, speaker=next_speaker)
        if self.speaker_router is not None:
            self.speaker_router.configure(
                confirm_threshold=confirm,
                continuity_threshold=next_speaker.continuity_threshold,
            )
        if self.controller is not None:
            self.controller.speaker_mode = mode if self.speaker_profile.available else "observe" if mode != "off" else "off"
            self.controller.owner_only_barge_in = owner_only and self.speaker_profile.available
            if previous_mode != mode:
                self.controller._speaker_snapshot = None
                self.controller._speaker_snapshot_turn_id = None
                self.controller._speaker_snapshot_generation = None
                self.controller._speaker_snapshot_barge_id = None
            if mode == "off":
                self.state.current.pop("speaker_rolling", None)
                self.state.current.pop("speaker_decision", None)
            if previous_owner_only != owner_only:
                self.controller.barge_in_gate.reset()
                self.controller._clear_owner_barge_candidate()
                self.controller._owner_barge_expired = False
            # ``ensure_speaker_worker`` may have replaced a degraded/stale
            # client while preparing filter mode. Always refresh the
            # controller reference for every enabled mode; otherwise a
            # reconnect can leave the controller holding the old ``None`` or
            # disconnected client even though the runtime is READY.
            if mode != "off" or owner_only:
                self.controller.speaker_client = self.speaker
        if mode == "off" and not owner_only:
            if self.speaker is not None:
                await self.speaker.close()
            self.speaker = None
            await self._stop_managed_speaker_worker()
            await asyncio.to_thread(self._release_speaker_engine)
            self.state.set_service("speaker", "DISABLED")
            if self.controller is not None:
                self.controller.speaker_client = None
        elif self.speaker is None and self.speaker_profile.available:
            await self._initialize_speaker()
            if self.controller is not None:
                self.controller.speaker_client = self.speaker
        if self.controller is not None:
            if hasattr(self.controller, "speaker_policy"):
                self.controller.speaker_policy.tune(next_speaker)
            self.controller.speaker_config = next_speaker
            self.controller.configure_speaker_runtime()
        await self.bus.publish(self.snapshot())
        status = self.speaker_status()
        return {
            "mode": mode,
            "confirm_threshold": confirm,
            "continuity_threshold": next_speaker.continuity_threshold,
            "continuity_relaxation": relaxation,
            "owner_only_barge_in": owner_only,
            "owner_only_barge_in_selected": status["owner_only_barge_in_selected"],
            "owner_only_barge_in_ready": status["owner_only_barge_in_ready"],  # 配置及组件就绪。
            "owner_only_barge_in_effective": status["owner_only_barge_in_effective"],
            "owner_only_barge_in_reason": status["owner_only_barge_in_reason"],
        }

    async def publish_voice_event(self, payload: dict[str, Any]) -> str:
        """Accept an ordinary DSH event without a Voice-initiated activation."""
        source = str(payload.get("source_session_id") or "")
        event_id = str(payload.get("event_id") or "")
        message = payload.get("text")
        if not event_id or not isinstance(message, str) or not message.strip():
            return "conflict"
        extra = payload.get("payload")
        if extra is not None and not isinstance(extra, dict):
            return "conflict"
        voice_event = DurableVoiceEvent(
            event_id=event_id,
            text=message,
            source_session_id=source,
            payload=dict(extra or {}),
        )
        async with self._session_lock:
            # 在清除或重置结束后重新核对，阻止等待锁的旧 DSH（后台代理）事件进入新会话。
            current = getattr(self.shadow_session, "dsh_session_id", "")
            if source.startswith("voice-shadow-") and source != current:
                return "stale"
            code = await self._voice_mailbox.publish(voice_event)
            if code == "accepted":
                self.continuity.user_started()
                await self._deliver_voice_mailbox()
            return code

    async def _deliver_voice_mailbox(self) -> None:
        controller = self.controller
        if controller is None or not self.state.listening or controller.episode.closed:
            return
        for item in await self._voice_mailbox.pending():
            await controller.accept_durable_voice_event(item)
            await self._voice_mailbox.acknowledge(item.event_id)

    async def _on_shadow_events(self, events: list[ShadowEvent]) -> None:
        # 停止输入后影子继续运行；普通信息和持久语义等待下一次开始，临时进度不跨停止边界。
        if (
            isinstance(getattr(self, "shadow_session", None), DshShadowAgentSession)
            and any(item.kind != "failed" for item in events)
        ):
            self.state.set_service("shadow_agent", "READY")
        if self._shadow_debug_sinks:
            await asyncio.gather(
                *(sink(events) for sink in tuple(self._shadow_debug_sinks.values())),
                return_exceptions=True,
            )
        self.shadow_product.update_events(events)
        await self.publish_shadow_product()
        model_events = [
            item for item in events
            if item.kind in {"progress", "information", "input_required", "result", "no_action", "failed"}
        ]
        if not model_events:
            return
        controller = self.controller
        if controller is None or not self.state.listening or controller.episode.closed:
            for item in model_events:
                if item.kind != "progress" or item.source == "observer":
                    self._shadow_pending.publish(item)
            return
        await controller.accept_shadow_events(model_events)

    def _on_shadow_tool_status(self, name: str, status: str, reason: str) -> None:
        if name == "web_search" and "web_search" in self.state.services:
            self.state.set_service("web_search", status)
            if status != "READY":
                logger.warning("shadow web search status=%s reason=%s", status, str(reason)[:200])

    def _on_shadow_metric(self, name: str, payload: dict[str, Any]) -> None:
        logger.info(
            "shadow.metric name=%s payload=%s",
            name,
            {key: value for key, value in payload.items() if key not in {"text", "content"}},
        )

    def debug_shadow_wake(self, connection_id: str, *, event_sink) -> dict[str, Any]:
        if not self.config.shadow_agent.debug_websocket_enabled:
            raise ValueError("SHADOW_DEBUG_DISABLED")
        if self.shadow_session is None or not self.shadow_session.available:
            raise ValueError("SHADOW_UNAVAILABLE")
        self._shadow_debug_sinks[connection_id] = event_sink
        result = self.shadow_session.wake()
        return {"result": result, "shadow": self.shadow_session.snapshot()}

    async def close_shadow_debug(self, connection_id: str) -> None:
        self._shadow_debug_sinks.pop(connection_id, None)

    # _CAPABILITY_ROWS（能力行定义）：id（标识）、label（名称）与 phase（阶段说明）。
    _CAPABILITY_ROWS = (
        ("audio", "音频设备", "音频采集与输入处理"),
        ("asr", "ASR（语音识别）", "识别工作进程连接"),
        ("llm", "LLM（大语言模型）", "主模型客户端"),
        ("tts", "TTS（语音合成）", "语音合成路由与播放器"),
        ("kws", "KWS（关键词唤醒）", "关键词唤醒"),
        ("speaker", "声纹工作进程", "声纹工作进程"),
    )
    # _CAPABILITY_RECOVERY（能力恢复方法）：只重建一个能力，不重启语音服务进程。
    _CAPABILITY_RECOVERY = {
        "audio": "_initialize_audio", "asr": "_initialize_asr", "llm": "_initialize_llm",
        "tts": "_retry_tts", "kws": "_initialize_kws", "speaker": "_initialize_speaker",
    }
    # _CAPABILITY_ERROR_ALIASES（错误归属别名）：子环节失败记录在自己的键上，但归属同一能力。
    _CAPABILITY_ERROR_ALIASES = {"audio": ("audio", "audio_processing"), "asr": ("asr", "vad")}

    def _capability_error(self, capability: str) -> str:
        for name in self._CAPABILITY_ERROR_ALIASES.get(capability, (capability,)):
            message = self._service_errors.get(name)
            if message:
                return message
        return ""

    def capability_rows(self) -> list[dict[str, Any]]:
        """capability_rows（分能力状态）：宿主与界面据此显示“启动中/不可用”并单独恢复。"""
        rows: list[dict[str, Any]] = []
        for capability, label, phase in self._CAPABILITY_ROWS:
            disabled = ((capability == "kws" and not self.config.wake.enabled)
                        or (capability == "speaker" and self.config.speaker.mode == "off"
                            and not self.config.speaker.owner_only_barge_in))
            if disabled:
                rows.append({"id": capability, "label": label, "state": "DISABLED", "blocking": False,
                             "phase": "按配置停用", "error": "", "retry": None})
                continue
            status = self.state.services.get(capability, "OFFLINE")
            ready = status in SERVICE_READY_STATES
            rows.append({
                "id": capability, "label": label, "state": status,
                "blocking": capability in BLOCKING_SERVICES, "phase": phase,
                "error": "" if ready else self._capability_error(capability),
                "retry": capability if not ready and status in (SERVICE_LOADING_STATES | {"ERROR", "OFFLINE"}) else None,
            })
        return rows

    async def retry_capability(self, capability: str) -> dict[str, Any]:
        """retry_capability（能力级恢复）：只重建一个能力，不重启语音服务进程。"""
        name = str(capability or "")
        method = self._CAPABILITY_RECOVERY.get(name)
        if method is None:
            return {"ok": False, "message": "未知组件能力。"}
        if getattr(self, "_application_stopping", False):
            return {"ok": False, "message": "应用正在退出，不能恢复组件。"}
        if self.application_initializing:
            return {"ok": False, "message": "组件仍在加载，请等待本轮启动结束后再恢复。"}
        async with self._session_lock:
            logger.info("capability.retry_started capability=%s", name)
            self._clear_service_error(name)
            self.state.set_service(name, "STARTING")
            await self.bus.publish(self.snapshot())
            try:
                result = getattr(self, method)()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.exception("capability.retry_failed capability=%s", name)
                self._record_error(name, f"恢复失败：{type(exc).__name__}: {exc}")
                await self.bus.publish(self.snapshot())
                return {"ok": False, "message": f"组件恢复失败：{type(exc).__name__}"}
            ready = self.state.services.get(name) in SERVICE_READY_STATES
            if ready:
                self._clear_service_error(name)
            await self.bus.publish(self.snapshot())
            logger.info("capability.retry_finished capability=%s ready=%s", name, ready)
            message = "" if ready else "组件仍未就绪，请查看组件日志。"
        if (self._listen_resume_requested and not self.state.listening
                and self.state.can_start_reason is None):
            # 用户在加载期间点过开始：能力恢复完成后自动开始监听（与 TTS 切换的挂起意图同构）。
            self._listen_resume_requested = None
            await self.start_listening()
        return {"ok": ready, "message": message}

    def health_payload(self) -> dict[str, Any]:
        payload = {
            "state": self.state.state.value,
            "listening": self.state.listening,
            "can_start": self.state.can_start,
            "can_start_reason": self.state.can_start_reason,
            "services": dict(self.state.services),
            "capabilities": self.capability_rows(),
            "startup_errors": list(self.startup_errors),
            "wake_enabled": self.config.wake.enabled,
            "wake_always_on": self.state.wake_always_on,
            "input_owner": self.state.input_owner,
            "wake_keyword": self.config.wake.keyword,
            "wake_keywords": list(self.config.wake.keywords),
            "runtime": dict(self.build_info),
            "kws": self.kws.diagnostics if self.kws is not None else {
                "running": False,
                "error": None,
                "submitted_frames": 0,
                "dropped_frames": 0,
                "detected_count": 0,
                "queue_depth": 0,
            },
        }
        if self.product_settings is not None:
            payload["product_settings"] = {
                "warnings": list(self.product_settings.warnings),
            }
        return payload

    async def _asr_compute_health(self) -> dict[str, Any]:
        """在独立连接查询识别模型，避免排在设备切换命令后面。"""
        probe = AsrWorkerClient(self.asr.url)
        try:
            await probe.connect()
            return await probe.health(timeout_s=2.0, allow_starting=True)
        finally:
            await probe.close()

    async def compute_status(self, *, refresh_capabilities: bool = False) -> dict[str, Any]:
        """读取设备能力、模型实际设备和工作进程常驻内存。"""
        from ..resources import process_resource_status, aggregate_resource_status
        from ..compute import model_hardware_status
        resource_rows = [process_resource_status()]
        missing_resource_roles = set()
        hardware = await asyncio.to_thread(hardware_status, refresh=True) if refresh_capabilities else await asyncio.to_thread(hardware_status)
        hardware = await asyncio.to_thread(model_hardware_status, hardware, self.config, verify=True)
        requests = self.config.compute.devices
        models: dict[str, dict[str, Any]] = {
            name: {
                "requested_device": requests.get(name, "cpu"),
                "device": None,
                "service": "UNKNOWN",
                "load_error": None,
                "runtime_error": None,  # runtime_error（运行故障）：与加载回退原因分开。
                "process_memory_rss_bytes": None,
                "gpu_available": bool(hardware["models"].get(name, {}).get("gpu_available")),
                "gpu_unavailable_reason": hardware["models"].get(name, {}).get("reason"),
            }
            for name in ("paraformer", "emotion", "voice_vad", "kws", "smart_turn", "campplus")
        }

        models["kws"].update(self._compute_models.get("kws", {}))
        models["voice_vad"].update(self._compute_models.get("voice_vad", {}))
        for name in ("kws", "voice_vad"):
            models[name]["resource_status"] = resource_rows[0]  # resource_status（同一统计进程快照）。
            models[name]["process_memory_rss_bytes"] = resource_rows[0]["rss_bytes"]
        models["kws"]["service"] = self.state.services.get("kws", models["kws"].get("service", "UNKNOWN"))
        hotwords = getattr(self, "hotwords", None)
        auxiliary = {"hotwords": {  # auxiliary（共享进程的辅助组件）。
            "service": "READY" if hotwords is not None and hotwords.bank.matchers else "DISABLED",
            "runtime": "音素词典与快速模糊匹配", "device": "cpu",
            "resource_status": resource_rows[0], "resource_footprint": getattr(hotwords, "resource_footprint", None),
        }}
        if self.asr is not None:
            try:
                status = await self._asr_compute_health()
                if isinstance(status.get("resource_status"), dict):
                    resource_rows.append(status["resource_status"])
                else:
                    missing_resource_roles.add("asr")
                punctuation = status.get("punctuation", {})
                if not isinstance(punctuation, Mapping):
                    punctuation = {}
                auxiliary["punctuation"] = {
                    "service": punctuation.get("status", "UNKNOWN"),
                    "runtime": "PyTorch（实时标点）", "device": punctuation.get("device"),
                    "resource_status": status.get("resource_status"),
                    "resource_footprint": punctuation.get("resource_footprint"),
                    "load_error": punctuation.get("load_error") or punctuation.get("last_error"),
                }
                offline = status.get("offline", {})
                if isinstance(offline.get("resource_status"), dict):
                    resource_rows.append(offline["resource_status"])
                auxiliary["dual_pass"] = {"service": offline.get("status", "DISABLED"),
                    "runtime": "PyTorch（双路离线识别）", "device": offline.get("device"),
                    "resource_status": offline.get("resource_status") or status.get("resource_status"),
                    "resource_footprint": offline.get("resource_footprint"), "load_error": offline.get("error"),
                    "model_loaded": offline.get("model_loaded"), "model_warmed": offline.get("model_warmed")}
                models["paraformer"].update({
                    "resource_status": status.get("resource_status"),
                    "runtime": status.get("runtime"),
                    "device": status.get("device"),
                    "requested_device": status.get("requested_device", requests.get("paraformer", "auto")),
                    "service": status.get("status", "READY"),
                    "load_error": status.get("load_error"),
                    "model_loaded": status.get("model_loaded"),
                    "model_warmed": status.get("model_warmed"),
                    "resource_footprint": status.get("resource_footprint"),
                    "process_memory_rss_bytes": status.get("process_memory_rss_bytes"),
                })
            except Exception as exc:
                missing_resource_roles.add("asr")
                models["paraformer"].update({
                    "service": "ERROR",
                    "load_error": str(exc)[:300],
                })
        else:
            models["paraformer"]["service"] = self.state.services.get("asr", "DISABLED")

        try:
            analysis = await AnalysisClient(self.config.smart_turn.analysis_url).health()
            acoustic = analysis.get("acoustic", {})
            if isinstance(acoustic.get("resource_status"), dict):
                resource_rows.append(acoustic["resource_status"])
            auxiliary["acoustic_events"] = {"service": acoustic.get("status", "DISABLED"),
                "runtime": acoustic.get("runtime"), "device": "cpu", "resource_status": acoustic.get("resource_status"),
                "resource_footprint": acoustic.get("resource_footprint"), "load_error": acoustic.get("error")}
            for lane_name, model_name in (("emotion", "emotion"), ("endpoint", "smart_turn")):
                lane = analysis.get(lane_name, {})
                if isinstance(lane, dict):
                    if isinstance(lane.get("resource_status"), dict):
                        resource_rows.append(lane["resource_status"])
                    elif lane.get("status") != "DISABLED":
                        missing_resource_roles.add("analysis")
                    models[model_name].update({
                        "resource_status": lane.get("resource_status"),
                        "resource_footprint": lane.get("resource_footprint"),
                        "runtime": lane.get("runtime"),
                        "device": lane.get("device"),
                        "requested_device": lane.get("requested_device", requests.get(model_name, "cpu")),
                        "service": lane.get("status", "UNKNOWN"),
                        "load_error": lane.get("load_error") or lane.get("error"),
                        "model_loaded": lane.get("model_loaded"),
                        "model_warmed": lane.get("model_warmed"),
                        "process_memory_rss_bytes": lane.get("process_memory_rss_bytes"),
                    })
                    if model_name == "emotion":
                        models[model_name]["internal_vad_device"] = lane.get("internal_vad_device")
        except Exception as exc:
            if self.config.smart_turn.smart_turn or self.config.audio_enhancements.emotion:
                missing_resource_roles.add("analysis")
            for name in ("emotion", "smart_turn"):
                models[name].update({"service": "UNAVAILABLE", "load_error": str(exc)[:300]})

        if self.speaker is not None and self.speaker.connected:
            try:
                status = await self.speaker.health(timeout_s=2.0)
                speaker_runtime = self.speaker_status()
                if isinstance(status.get("resource_status"), dict):
                    resource_rows.append(status["resource_status"])
                else:
                    missing_resource_roles.add("speaker")
                models["campplus"].update({
                    "resource_status": status.get("resource_status"),
                    "runtime": status.get("runtime"),
                    "device": status.get("device"),
                    "requested_device": status.get("requested_device", requests.get("campplus", "cpu")),
                    "service": "ERROR" if speaker_runtime["service"] == "DEGRADED" else speaker_runtime["service"],
                    "load_error": status.get("load_error"),
                    "model_loaded": status.get("model_loaded"),
                    "model_warmed": bool(getattr(self, "_speaker_warmed", False)),
                    "runtime_error": speaker_runtime["last_error"] if speaker_runtime["service"] == "DEGRADED" else None,
                    "process_memory_rss_bytes": status.get("process_memory_rss_bytes"),
                })
            except Exception as exc:
                missing_resource_roles.add("speaker")
                models["campplus"].update({"service": "ERROR", "runtime_error": str(exc)[:300] or f"声纹健康检查失败：{type(exc).__name__}"})
        else:
            speaker_runtime = self.speaker_status()
            models["campplus"].update({
                "service": "ERROR" if speaker_runtime["service"] == "DEGRADED" else speaker_runtime["service"],
                "runtime_error": speaker_runtime["last_error"] if speaker_runtime["service"] == "DEGRADED" else None,
            })

        for name, status in models.items():
            running_on_cuda = (str(status.get("device") or "").lower().startswith("cuda")
                               and status.get("service") == "READY"
                               and status.get("model_loaded") is True)
            capability = hardware["models"].get(name, {})
            status["gpu_available"] = bool(capability.get("gpu_available") or running_on_cuda)
            status["gpu_unavailable_reason"] = None if running_on_cuda else capability.get("reason")
        return {"hardware": hardware, "models": models, "auxiliary": auxiliary,
                "resource_status": aggregate_resource_status(resource_rows, missing_roles=missing_resource_roles)}  # resource_status（资源状态）。



from .http_api import create_app

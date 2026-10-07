from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .env import load_env_file
from .paths import AppPaths
from .quiet_hours import QuietHoursConfig
from .controller.options import InputOptions
from .agent.options import AgentOptions
from .kws.keywords import canonical_labels, normalize_keyword_list


REASONING_EFFORTS = ("off", "low", "medium", "high", "max")


def _string(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def _number(value: Any, default: int | float) -> int | float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _optional_int(value: Any, default: int | None = None) -> int | None:
    """Read an integer where ``None`` deliberately means unlimited."""
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return default


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _reasoning_levels(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = value if isinstance(value, (list, tuple)) else default
    normalized = tuple(dict.fromkeys(str(item).strip().lower() for item in raw))
    if not normalized or any(item not in REASONING_EFFORTS for item in normalized):
        raise ValueError("allowed_reasoning must contain off, low, medium, high, or max")
    return tuple(item for item in REASONING_EFFORTS if item in normalized)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, Mapping) else {}


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


@dataclass(frozen=True)
class AudioConfig:
    input_device: str | int | None = None
    output_device: str | int | None = None
    sample_rate: int = 16000
    channels: int = 1
    sample_format: str = "int16"
    block_ms: int = 20
    ring_buffer_ms: int = 2000
    wake_preroll_ms: int = 1200
    processing_enabled: bool = True
    processing_required: bool = False
    # What to do if the protected AEC path fails while PCM is audible.
    # Do not silently fall back to raw microphone VAD: that reintroduces
    # speaker echo as a false user interruption.
    aec_failure_policy: str = "pause_recover"
    echo_cancellation: bool = True
    noise_suppression: bool = True
    automatic_gain_control: bool = False
    # Conservative device-output tail used when PortAudio does not expose a
    # trustworthy DAC timestamp. Keep a conservative, explicit fallback.
    device_latency_ms: int = 0


@dataclass(frozen=True)
class WakeConfig:
    enabled: bool = False
    always_on: bool = False
    provider: str = "sherpa_kws"
    # ``keyword``/``keyword_label`` stay the *primary* (first) phrase so every
    # older consumer — health payloads, doctor, probe banners, fixtures — keeps
    # working.  ``keywords`` is the authoritative list the detector matches.
    keyword: str = "hei claw"
    keyword_label: str = "HEI_CLAW"
    keywords: tuple[str, ...] = ("hei claw",)
    threshold: float = 0.25
    keywords_score: float = 1.0
    num_trailing_blanks: int = 1
    # Beam width for the keyword spotter.  Wider means more surviving paths for
    # a short phrase; 4 is sherpa's default and was previously hard-coded.
    max_active_paths: int = 4
    device: str = "cpu"
    model_dir: Path | None = None
    keywords_file: Path | None = None

    @property
    def keyword_labels(self) -> tuple[str, ...]:
        """The ``@LABEL`` of every configured phrase, in order."""

        return canonical_labels(self.keywords or (self.keyword,))


@dataclass(frozen=True)
class VadConfig:
    model: str = "fsmn-vad"
    min_speech_ms: int = 250
    end_silence_ms: int = 200
    max_turn_ms: int = 15000


@dataclass(frozen=True)
class AsrConfig:
    provider: str = "funasr"
    model: str = "paraformer-zh-streaming"
    device: str = "auto"
    worker_url: str = "ws://127.0.0.1:8766/asr"
    chunk_size: tuple[int, int, int] = (0, 10, 5)
    encoder_chunk_look_back: int = 4
    decoder_chunk_look_back: int = 1


COMPUTE_MODEL_IDS = (
    "paraformer",
    "sensevoice",
    "voice_vad",
    "kws",
    "smart_turn",
    "wespeaker",
)
COMPUTE_DEVICE_MODES = {"auto", "cpu", "cuda"}


@dataclass(frozen=True)
class ComputeConfig:
    """各本地模型的设备偏好和上次成功设备。"""

    # devices（设备偏好）：auto 自动选择，cpu 仅中央处理器，cuda 使用 NVIDIA GPU。
    devices: Mapping[str, str] = field(default_factory=lambda: {
        "paraformer": "auto",
        "sensevoice": "cpu",
        "voice_vad": "cpu",
        "kws": "cpu",
        "smart_turn": "cpu",
        "wespeaker": "cpu",
    })
    # fallback_devices（回退设备）：显式 GPU 加载失败时使用的上次成功设备。
    fallback_devices: Mapping[str, str] = field(default_factory=lambda: {
        "paraformer": "cpu",
        "sensevoice": "cpu",
        "voice_vad": "cpu",
        "kws": "cpu",
        "smart_turn": "cpu",
        "wespeaker": "cpu",
    })

    def __post_init__(self) -> None:
        for name in COMPUTE_MODEL_IDS:
            device = self.devices.get(name, "cpu")
            fallback = self.fallback_devices.get(name, "cpu")
            if device not in COMPUTE_DEVICE_MODES:
                raise ValueError(f"compute.devices.{name} must be auto, cpu, or cuda")
            if fallback not in {"cpu", "cuda"}:
                raise ValueError(f"compute.fallback_devices.{name} must be cpu or cuda")


@dataclass(frozen=True)
class LlmConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    stream: bool = True
    reasoning_mode: str = "fast"  # reasoning_mode（运行模式）：默认快速，仅支持快速和深度。
    thinking: str = "enabled"
    reasoning_effort: str = "low"
    # Direct programmatic construction keeps the provider-compatible full
    # capability set for backwards compatibility.  YAML/product loading
    # supplies the model-specific policy explicitly.
    allowed_reasoning: tuple[str, ...] = REASONING_EFFORTS
    timeout_ms: int = 15000
    transport: str = "chat_completions"
    max_output_tokens: int = 393216
    protocol_recovery_limit: int = 1
    transport_retry_limit: int = 2

    def __post_init__(self) -> None:
        if self.provider not in {"deepseek", "opencode_go", "openai_compatible"}:
            raise ValueError("llm.provider must be deepseek, opencode_go, or openai_compatible")
        if self.transport not in {"chat_completions", "responses"}:
            raise ValueError("llm.transport must be chat_completions or responses")
        if self.thinking not in {"disabled", "enabled"}:
            raise ValueError("llm.thinking must be disabled or enabled")
        if self.reasoning_mode not in {"fast", "deep"}:
            raise ValueError("llm.reasoning_mode（运行模式）必须是 fast（快速）或 deep（深度）")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("llm.reasoning_effort must be off, low, medium, high, or max")
        object.__setattr__(self, "allowed_reasoning", _reasoning_levels(self.allowed_reasoning, REASONING_EFFORTS))
        if self.reasoning_effort not in self.allowed_reasoning:
            raise ValueError("llm.reasoning_effort must be included in llm.allowed_reasoning")
        if self.max_output_tokens <= 0 or self.timeout_ms <= 0:
            raise ValueError("llm token and timeout limits must be positive")
        if self.protocol_recovery_limit < 0:
            raise ValueError("llm.protocol_recovery_limit cannot be negative")
        if self.transport_retry_limit < 0:
            raise ValueError("llm.transport_retry_limit cannot be negative")


@dataclass(frozen=True)
class TtsConfig:
    provider: str = "minimax"
    # Product selection; ``provider`` remains the legacy MiniMax model metadata.
    backend: str = "minimax"
    base_url: str = "https://api.minimaxi.com"
    websocket_path: str = "/ws/v1/t2a_v2"
    transport: str = "websocket"
    scheduling: str = "auto"
    model: str = "speech-2.8-hd"
    api_key_env: str = "MINIMAX_API_KEY"
    output_format: str = "pcm"
    sample_rate: int = 32000
    voice_id: str = "female-shaonv"
    timeout_ms: int = 15000
    subtitle_enable: bool = False
    subtitle_type: str = "word_streaming"
    audio_cache_max_bytes: int = 64 * 1024 * 1024
    cancel_timeout_ms: int = 2000
    keepalive_seconds: int = 30
    emotion_mode: str = "agent"
    default_emotion: str = "calm"
    emotion_transport: str = "voice_setting"
    session_reuse: bool = True
    # Lead window for text admission.  Text is released to the provider only
    # while the returned-but-unplayed audio stays below this value, so the tail
    # of a live response stays locally replaceable for as long as possible.
    admission_lead_ms: int = 2500
    sentence_prefetch_ms: int = 2000
    sentence_tick_ms: int = 50
    sentence_min_effective_chars: int = 10  # sentence_min_effective_chars（锁定文本块最少有效字符）。
    first_clause_timeout_ms: int = 400

    def __post_init__(self) -> None:
        if self.backend not in {"minimax", "cosyvoice"}:
            raise ValueError("tts.backend 必须为 minimax 或 cosyvoice")
        if self.transport not in {"websocket", "websocket_bidi"}:
            raise ValueError("tts.transport must be websocket or websocket_bidi")
        if self.scheduling not in {"auto", "direct", "playback_window", "sentence_window"}:
            raise ValueError("tts.scheduling must be auto, direct, playback_window, or sentence_window")
        # A Bidi deployment may use playback_window for its lead window.  The
        # legacy per-segment completion wait stays unavailable there because
        # the server breaks sentences itself: one sent frame is not one server
        # segment, so waiting for "this frame finished" would stall.  The two
        # behaviours are selected independently in the speech pipeline.
        if self.admission_lead_ms <= 0:
            raise ValueError("tts.admission_lead_ms must be positive")
        if self.sentence_prefetch_ms <= 0:
            raise ValueError("tts.sentence_prefetch_ms must be positive")
        if self.sentence_tick_ms <= 0:
            raise ValueError("tts.sentence_tick_ms must be positive")
        if self.sentence_min_effective_chars <= 0:
            raise ValueError("tts.sentence_min_effective_chars（锁定文本块最少有效字符）必须为正整数")
        if self.first_clause_timeout_ms <= 0:
            raise ValueError("tts.first_clause_timeout_ms must be positive")
        if self.subtitle_type not in {"sentence", "word", "word_streaming"}:
            raise ValueError("tts.subtitle_type is invalid")
        if self.audio_cache_max_bytes <= 0 or self.cancel_timeout_ms <= 0 or self.keepalive_seconds <= 0:
            raise ValueError("tts cache, cancel timeout, and keepalive values must be positive")
        if self.emotion_mode not in {"off", "agent"}:
            raise ValueError("tts.emotion_mode must be off or agent")
        if self.emotion_transport not in {"voice_setting"}:
            raise ValueError("tts.emotion_transport is invalid")

    @property
    def effective_scheduling(self) -> str:
        if self.transport == "websocket_bidi" and self.scheduling == "auto":
            return "direct"
        return self.scheduling


@dataclass(frozen=True)
class TextBridgeConfig:
    min_chars: int = 8
    max_chars: int = 36
    max_wait_ms: int = 350
    preferred_breaks: str = "。！？；，、"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class AsrWorkerConfig:
    host: str = "127.0.0.1"
    port: int = 8766


@dataclass(frozen=True)
class SpeakerConfig:
    mode: str = "off"
    owner_only_barge_in: bool = False
    confirm_threshold: float = 0.60
    continuity_threshold: float = 0.50
    window_ms: int = 2000
    step_ms: int = 500
    min_speech_ms: int = 700
    min_window_speech_ratio: float = 0.40
    min_turn_speech_ms: int = 1500
    wait_ms: int = 150
    model_id: str = "wespeaker-cnceleb-resnet34"

    def __post_init__(self) -> None:
        if self.mode not in {"off", "observe", "filter"}:
            raise ValueError("speaker.mode must be off, observe, or filter")
        if type(self.owner_only_barge_in) is not bool:
            raise ValueError("speaker.owner_only_barge_in must be boolean")
        if not 0.0 <= self.continuity_threshold < self.confirm_threshold <= 1.0:
            raise ValueError("speaker thresholds must satisfy 0 <= continuity < confirm <= 1")
        if self.window_ms < 500 or self.step_ms < 100 or self.step_ms > self.window_ms:
            raise ValueError("speaker window settings are invalid")
        if self.min_speech_ms < 100 or self.min_speech_ms > self.window_ms:
            raise ValueError("speaker.min_speech_ms is invalid")
        if not 0.0 < float(self.min_window_speech_ratio) <= 1.0:
            raise ValueError("speaker.min_window_speech_ratio must be in (0, 1]")
        if self.min_turn_speech_ms < self.min_speech_ms:
            raise ValueError("speaker.min_turn_speech_ms must be at least min_speech_ms")
        if not 0 <= self.wait_ms <= 150:
            raise ValueError("speaker.wait_ms must be between 0 and 150")


@dataclass(frozen=True)
class SpeakerWorkerConfig:
    host: str = "127.0.0.1"
    # Keep the speaker sidecar separate from the analysis worker, which uses
    # the legacy 8767 endpoint by default.
    port: int = 8768


@dataclass(frozen=True)
class ShadowProviderConfig:
    """Dedicated Responses provider settings for the shadow component.

    ``None`` disables the local request deadline and retry-count ceiling.  A
    reset, close, provider protocol failure, or external service failure can
    still end the operation normally.
    """

    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_ms: int | None = None
    transport_retry_limit: int | None = None

    def __post_init__(self) -> None:
        if self.timeout_ms is not None and self.timeout_ms <= 0:
            raise ValueError("shadow_agent.provider.timeout_ms must be positive or null")
        if self.transport_retry_limit is not None and self.transport_retry_limit < 0:
            raise ValueError("shadow_agent.provider.transport_retry_limit must be nonnegative or null")


@dataclass(frozen=True)
class ShadowWebSearchConfig:
    enabled: bool = True
    base_url: str = "https://api.minimaxi.com"
    api_key_env: str = "MINIMAX_API_KEY"
    timeout_ms: int | None = 15000

    def __post_init__(self) -> None:
        if self.timeout_ms is not None and self.timeout_ms <= 0:
            raise ValueError("shadow_agent.tools.web_search.timeout_ms must be positive or null")


@dataclass(frozen=True)
class ShadowToolsConfig:
    files_enabled: bool = True
    allowed_roots: tuple[Path, ...] = (Path("docs"),)
    # Time before a read-only tool yields a pending job to the model.  The
    # backend continues running; this is deliberately not a cancellation
    # deadline.
    soft_yield_ms: int | None = 8000
    web_search: ShadowWebSearchConfig = field(default_factory=ShadowWebSearchConfig)

    def __post_init__(self) -> None:
        if self.soft_yield_ms is not None and self.soft_yield_ms <= 0:
            raise ValueError("shadow_agent.tools.soft_yield_ms must be positive or null")

    @property
    def timeout_ms(self) -> int | None:
        """Deprecated compatibility view for older embedding callers."""
        return self.soft_yield_ms


@dataclass(frozen=True)
class ShadowStatusObserverConfig:
    enabled: bool = True
    every_committed_steps: int = 5
    reasoning_effort: str = "none"
    max_output_tokens: int = 512
    timeout_ms: int | None = None
    # None intentionally means inherit the Shadow Agent provider.  A managed
    # product assignment supplies an explicit, independent status provider.
    provider: ShadowProviderConfig | None = None
    max_context_tokens: int = 32768

    def __post_init__(self) -> None:
        if self.every_committed_steps < 1 or self.max_output_tokens < 1 or (self.timeout_ms is not None and self.timeout_ms <= 0) or self.max_context_tokens < 1024:
            raise ValueError("invalid shadow_agent.status_observer configuration")
        if self.reasoning_effort != "none":
            raise ValueError("shadow_agent.status_observer.reasoning_effort must be none")


@dataclass(frozen=True)
class ShadowAgentConfig:
    enabled: bool = True
    backend: Literal["native", "dsh"] = "dsh"
    debug_websocket_enabled: bool = False
    experimental_context_gate_enabled: bool = False
    provider: ShadowProviderConfig = field(default_factory=ShadowProviderConfig)
    tools: ShadowToolsConfig = field(default_factory=ShadowToolsConfig)
    max_context_tokens: int = 524288
    reasoning_effort: str = "high"
    allowed_reasoning: tuple[str, ...] = REASONING_EFFORTS
    compaction_threshold_ratio: float = 0.80
    runtime_reminder_interval_ms: int = 20000
    status_observer: ShadowStatusObserverConfig = field(default_factory=ShadowStatusObserverConfig)

    def __post_init__(self) -> None:
        if self.backend not in {"native", "dsh"}:
            raise ValueError("shadow_agent.backend must be native or dsh")
        if self.max_context_tokens < 1024:
            raise ValueError("shadow_agent.max_context_tokens must be at least 1024")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("shadow_agent.reasoning_effort must be off, low, medium, high, or max")
        object.__setattr__(self, "allowed_reasoning", _reasoning_levels(self.allowed_reasoning, REASONING_EFFORTS))
        if "high" not in self.allowed_reasoning:
            raise ValueError("shadow_agent.allowed_reasoning must include high")
        object.__setattr__(self, "reasoning_effort", "high")
        if not 0 < self.compaction_threshold_ratio < 1:
            raise ValueError("shadow_agent.compaction_threshold_ratio must be between 0 and 1")
        if self.runtime_reminder_interval_ms <= 0:
            raise ValueError("shadow_agent.runtime_reminder_interval_ms must be positive")


@dataclass(frozen=True)
class DshConfig:
    enabled: bool = True
    auto_start: bool = True
    source_dir: Path = Path(".runtime/dsh/source")
    home_dir: Path = Path(".runtime/dsh/home")
    profile_patch: Path = Path("config/dsh/web-profile.patch.yml")
    profile: str = "web"
    host: str = "127.0.0.1"
    port: int = 3081
    startup_timeout_ms: int = 120000
    shutdown_timeout_ms: int = 5000
    bridge_timeout_ms: int = 15000
    bridge_protocol_version: str = "2.0"
    restart_limit: int = 2
    restart_delay_ms: int = 1000
    expected_version: str = "0.2.0-rc.2"
    expected_commit: str = "639ed015397290b3745d163aafe02ffee4aa3f84"

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("dsh.host must be 127.0.0.1")
        if self.profile != "web":
            raise ValueError("dsh.profile must be web")
        if self.port != 3081:
            raise ValueError("dsh.port must be 3081")
        if self.startup_timeout_ms <= 0 or self.shutdown_timeout_ms <= 0 or self.bridge_timeout_ms <= 0:
            raise ValueError("dsh timeouts must be positive")
        if self.bridge_protocol_version != "2.0":
            raise ValueError("unsupported dsh.bridge_protocol_version")
        if self.restart_limit < 0 or self.restart_delay_ms < 0:
            raise ValueError("dsh restart policy must be non-negative")
        if not self.expected_version.strip() or len(self.expected_commit) != 40:
            raise ValueError("dsh expected version and full commit are required")


@dataclass(frozen=True)
class PersonaConfig:
    """Paths for the episode-scoped Markdown personality documents."""

    directory: Path = Path("config/persona")
    personality_file: str = "personality.md"
    user_file: str = "user.md"
    instructions_file: str = "instructions.md"


@dataclass(frozen=True)
class ContinuityConfig:
    """continuity（交接整理）：供应方为空时继承影子模型。"""
    provider: ShadowProviderConfig | None = None  # provider（独立整理供应方）。
    max_document_chars: int = 6000  # max_document_chars（文档字符上限）。
    max_output_tokens: int = 16384  # max_output_tokens（输出词元预算）。
    max_context_tokens: int = 524288  # max_context_tokens（输入上下文预算）。

    def __post_init__(self) -> None:
        if (not all(type(value) is int for value in (self.max_document_chars, self.max_output_tokens, self.max_context_tokens))
                or self.max_document_chars < 1 or self.max_output_tokens < 1 or self.max_context_tokens < 1024):
            raise ValueError("continuity（交接整理）预算必须为正，上下文至少1024词元。")
        if self.max_output_tokens >= self.max_context_tokens:
            raise ValueError("max_output_tokens（输出预算）必须小于 max_context_tokens（上下文预算）。")


@dataclass(frozen=True)
class AppConfig:
    quiet_hours: QuietHoursConfig = field(default_factory=QuietHoursConfig)  # quiet_hours（被动静默时间）。
    continuity: ContinuityConfig = field(default_factory=ContinuityConfig)  # continuity（交接整理配置）。
    smart_turn: InputOptions = field(default_factory=InputOptions)
    agent: AgentOptions = field(default_factory=AgentOptions)
    shadow_agent: ShadowAgentConfig = field(default_factory=ShadowAgentConfig)
    dsh: DshConfig = field(default_factory=DshConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    model_dir: Path = Path(r"D:\模型\voice-terminal")
    paths: AppPaths | None = None  # paths（受管应用路径上下文；源码调试兼容原布局）。
    debug_audio_capture: bool = False
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    # compute（本地模型计算设备偏好）：由产品设置覆盖 YAML 默认值。
    compute: ComputeConfig = field(default_factory=ComputeConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    text_bridge: TextBridgeConfig = field(default_factory=TextBridgeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    asr_worker: AsrWorkerConfig = field(default_factory=AsrWorkerConfig)
    speaker: SpeakerConfig = field(default_factory=SpeakerConfig)
    speaker_worker: SpeakerWorkerConfig = field(default_factory=SpeakerWorkerConfig)

    def __post_init__(self) -> None:
        if self.quiet_hours.enabled and not self.wake.enabled:
            raise ValueError("quiet_hours（被动静默）要求开启 wake（关键词唤醒）。")

    @property
    def kws_dir(self) -> Path:
        return self.wake.model_dir or self.model_dir / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M"

    @property
    def kws_keywords_file(self) -> Path:
        return self.wake.keywords_file or self.kws_dir / "keywords.txt"

    @property
    def asr_model_dir(self) -> Path:
        return self.model_dir / "funasr" / "paraformer-zh-streaming"

    @property
    def vad_model_dir(self) -> Path:
        return self.model_dir / "funasr" / "fsmn-vad"

    @property
    def speaker_model_path(self) -> Path:
        return self.model_dir / "speaker" / self.speaker.model_id / "cnceleb_resnet34.onnx"

    @property
    def speaker_worker_url(self) -> str:
        return f"ws://{self.speaker_worker.host}:{self.speaker_worker.port}/speaker"

    @property
    def resolved_asr_device(self) -> str:
        requested = self.compute.devices.get("paraformer", self.asr.device)
        if requested != "auto":
            return requested
        try:
            import torch  # type: ignore

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def api_key(self, env_name: str) -> str:
        return os.environ.get(env_name, "").strip()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, base_dir: Path | None = None) -> "AppConfig":
        root = base_dir or Path.cwd()
        paths = AppPaths.resolve(root)
        model_dir = Path(os.environ.get("VOICE_MODEL_DIR", "").strip() or _string(data.get("model_dir"), r"D:\模型\voice-terminal"))
        if not model_dir.is_absolute():
            model_dir = root / model_dir

        audio = _section(data, "audio")
        wake = _section(data, "wake")
        vad = _section(data, "vad")
        asr = _section(data, "asr")
        llm = _section(data, "llm")
        tts = _section(data, "tts")
        bridge = _section(data, "text_bridge")
        server = _section(data, "server")
        worker = _section(data, "asr_worker")
        speaker = _section(data, "speaker")
        speaker_worker = _section(data, "speaker_worker")
        compute = _section(data, "compute")
        persona = _section(data, "persona")
        shadow = _section(data, "shadow_agent")
        dsh = _section(data, "dsh")
        if "tools" in data:
            raise ValueError("顶层 tools 配置已迁移至 shadow_agent.tools，请更新配置文件")

        configured_llm_provider = _string(llm.get("provider"), "deepseek")
        llm_provider_override = os.environ.get("VOICE_LLM_PROVIDER", "").strip()
        llm_provider = llm_provider_override or configured_llm_provider
        profile_defaults = {
            "deepseek": {
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-flash",
                "api_key_env": "DEEPSEEK_API_KEY",
            },
            "opencode_go": {
                "base_url": "https://opencode.ai/zen/go/v1",
                "model": "deepseek-flash",
                "api_key_env": "OPENCODE_GO_API_KEY",
            },
        }.get(llm_provider, {})
        if llm_provider_override:
            llm_base_url_default = str(profile_defaults.get("base_url", ""))
            llm_model_default = str(profile_defaults.get("model", ""))
            llm_api_key_env_default = str(profile_defaults.get("api_key_env", ""))
        else:
            llm_base_url_default = str(profile_defaults.get("base_url", "https://api.deepseek.com"))
            llm_model_default = str(profile_defaults.get("model", "deepseek-flash"))
            llm_api_key_env_default = str(profile_defaults.get("api_key_env", "DEEPSEEK_API_KEY"))
        if llm_provider_override:
            llm_base_url = _string(os.environ.get("VOICE_LLM_BASE_URL"), llm_base_url_default)
            llm_model = _string(os.environ.get("VOICE_LLM_MODEL"), llm_model_default)
            llm_api_key_env = _string(os.environ.get("VOICE_LLM_API_KEY_ENV"), llm_api_key_env_default)
        else:
            llm_base_url = _string(llm.get("base_url"), llm_base_url_default)
            llm_model = _string(llm.get("model"), llm_model_default)
            llm_api_key_env = _string(llm.get("api_key_env"), llm_api_key_env_default)

        worker_host = os.environ.get("ASR_WORKER_HOST", "").strip() or _string(worker.get("host"), "127.0.0.1")
        worker_port = int(os.environ.get("ASR_WORKER_PORT", "").strip() or _number(worker.get("port"), 8766))
        configured_worker_url = os.environ.get("ASR_WORKER_URL", "").strip()
        worker_url = configured_worker_url or _string(asr.get("worker_url"), f"ws://{worker_host}:{worker_port}/asr")
        if os.environ.get("ASR_WORKER_HOST", "").strip() or os.environ.get("ASR_WORKER_PORT", "").strip():
            worker_url = f"ws://{worker_host}:{worker_port}/asr"

        wake_enabled = _env_bool("VOICE_WAKE_ENABLED", _bool(wake.get("enabled"), False))
        keyword = _string(wake.get("keyword"), "hei claw")
        label = _string(wake.get("keyword_label"), "HEI_CLAW")
        # ``wake.keywords`` is optional and additive: a file that only carries
        # the legacy single ``keyword`` keeps behaving exactly as before.
        raw_keywords = wake.get("keywords")
        if isinstance(raw_keywords, (list, tuple)) and raw_keywords:
            try:
                keywords = normalize_keyword_list(raw_keywords)
            except ValueError:
                keywords = normalize_keyword_list([keyword])
        else:
            keywords = normalize_keyword_list([keyword])
        keyword = keywords[0]
        label = canonical_labels(keywords)[0]
        kws_dir = wake.get("model_dir")
        keywords_file = wake.get("keywords_file")

        def path_or_none(value: Any) -> Path | None:
            if not isinstance(value, str) or not value.strip():
                return None
            path = Path(value)
            return path if path.is_absolute() else root / path

        chunk_value = asr.get("chunk_size", [0, 10, 5])
        if isinstance(chunk_value, (list, tuple)) and len(chunk_value) == 3 and all(isinstance(item, int) for item in chunk_value):
            chunk_size = tuple(chunk_value)
        else:
            chunk_size = (0, 10, 5)

        smart_turn_data = dict(_section(data, "smart_turn"))
        smart_turn_data["enabled"] = True

        shadow_provider = _section(shadow, "provider")
        shadow_tools = _section(shadow, "tools")
        shadow_search = _section(shadow_tools, "web_search")
        shadow_observer = _section(shadow, "status_observer")
        status_provider = _section(shadow_observer, "provider")
        raw_shadow_roots = shadow_tools.get("allowed_roots", ["docs"])
        if not isinstance(raw_shadow_roots, list) or not all(isinstance(item, str) and item.strip() for item in raw_shadow_roots):
            raise ValueError("shadow_agent.tools.allowed_roots must be a nonempty list of paths")
        shadow_roots = tuple(
            path if path.is_absolute() else (root / path).resolve()
            for path in (Path(item) for item in raw_shadow_roots)
        )
        dsh_runtime_root = (root / ".runtime" / "dsh").resolve()

        def dsh_runtime_path(value: Any, default: str, field_name: str) -> Path:
            candidate = Path(_string(value, default))
            resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
            if paths.separated:
                if field_name == "home_dir":
                    return paths.dsh_home
                packaged = os.environ.get("VOICE_DSH_RUNTIME")
                if packaged:
                    return Path(packaged).resolve() / "source"
            try:
                resolved.relative_to(dsh_runtime_root)
            except ValueError as exc:
                raise ValueError(f"dsh.{field_name} must remain inside {dsh_runtime_root}") from exc
            return resolved

        return cls(
            quiet_hours=QuietHoursConfig(**dict(_section(data, "quiet_hours"))),
            continuity=ContinuityConfig(**{
                **{key: value for key, value in _section(data, "continuity").items() if key != "provider"},
                "provider": (ShadowProviderConfig(**dict(_section(_section(data, "continuity"), "provider")))
                             if _section(_section(data, "continuity"), "provider") else None),
            }),
            smart_turn=InputOptions.from_mapping(smart_turn_data, root),
            agent=AgentOptions.from_mapping(_section(data, "agent")),
            shadow_agent=ShadowAgentConfig(
                enabled=_bool(shadow.get("enabled"), True),
                backend=_string(shadow.get("backend"), "dsh").lower(),
                debug_websocket_enabled=_bool(shadow.get("debug_websocket_enabled"), False),
                experimental_context_gate_enabled=_bool(
                    shadow.get("experimental_context_gate_enabled"), False,
                ),
                provider=ShadowProviderConfig(
                    base_url=_string(shadow_provider.get("base_url"), llm_base_url),
                    model=_string(shadow_provider.get("model"), llm_model),
                    api_key_env=_string(shadow_provider.get("api_key_env"), llm_api_key_env),
                    timeout_ms=_optional_int(shadow_provider.get("timeout_ms")),
                    transport_retry_limit=_optional_int(shadow_provider.get("transport_retry_limit")),
                ),
                tools=ShadowToolsConfig(
                    files_enabled=_bool(shadow_tools.get("files_enabled"), True),
                    allowed_roots=shadow_roots,
                    soft_yield_ms=_optional_int(
                        shadow_tools.get("soft_yield_ms"),
                        _optional_int(shadow_tools.get("timeout_ms"), 8000),
                    ),
                    web_search=ShadowWebSearchConfig(
                        enabled=_bool(shadow_search.get("enabled"), True),
                        base_url=_string(shadow_search.get("base_url"), "https://api.minimaxi.com"),
                        api_key_env=_string(shadow_search.get("api_key_env"), "MINIMAX_API_KEY"),
                        timeout_ms=_optional_int(shadow_search.get("timeout_ms"), 15000),
                    ),
                ),
                max_context_tokens=int(_number(shadow.get("max_context_tokens"), 524288)),
                reasoning_effort=_string(shadow.get("reasoning_effort"), "high"),
                allowed_reasoning=_reasoning_levels(
                    shadow.get("allowed_reasoning"), REASONING_EFFORTS,
                ),
                compaction_threshold_ratio=float(_number(shadow.get("compaction_threshold_ratio"), 0.80)),
                runtime_reminder_interval_ms=int(_number(
                    shadow.get("runtime_reminder_interval_ms"), 20000,
                )),
                status_observer=ShadowStatusObserverConfig(
                    enabled=_bool(shadow_observer.get("enabled"), True),
                    every_committed_steps=int(_number(shadow_observer.get("every_committed_steps"), 5)),
                    reasoning_effort=_string(shadow_observer.get("reasoning_effort"), "none"),
                    max_output_tokens=int(_number(shadow_observer.get("max_output_tokens"), 512)),
                    timeout_ms=_optional_int(shadow_observer.get("timeout_ms")),
                    provider=(ShadowProviderConfig(
                        base_url=_string(status_provider.get("base_url"), _string(shadow_provider.get("base_url"), llm_base_url)),
                        model=_string(status_provider.get("model"), _string(shadow_provider.get("model"), llm_model)),
                        api_key_env=_string(status_provider.get("api_key_env"), _string(shadow_provider.get("api_key_env"), llm_api_key_env)),
                        timeout_ms=_optional_int(
                            status_provider.get("timeout_ms"),
                            _optional_int(shadow_provider.get("timeout_ms")),
                        ),
                        transport_retry_limit=_optional_int(
                            status_provider.get("transport_retry_limit"),
                            _optional_int(shadow_provider.get("transport_retry_limit")),
                        ),
                    ) if status_provider else None),
                    max_context_tokens=int(_number(shadow_observer.get("max_context_tokens"), 32768)),
                ),
            ),
            dsh=DshConfig(
                enabled=_bool(dsh.get("enabled"), True),
                auto_start=_bool(dsh.get("auto_start"), True),
                source_dir=dsh_runtime_path(dsh.get("source_dir"), ".runtime/dsh/source", "source_dir"),
                home_dir=dsh_runtime_path(dsh.get("home_dir"), ".runtime/dsh/home", "home_dir"),
                profile_patch=(root / "config" / "dsh" / "web-profile.patch.yml").resolve(),
                profile=_string(dsh.get("profile"), "web"),
                host=_string(dsh.get("host"), "127.0.0.1"),
                port=int(_number(dsh.get("port"), 3081)),
                startup_timeout_ms=int(_number(dsh.get("startup_timeout_ms"), 120000)),
                shutdown_timeout_ms=int(_number(dsh.get("shutdown_timeout_ms"), 5000)),
                bridge_timeout_ms=int(_number(dsh.get("bridge_timeout_ms"), 15000)),
                bridge_protocol_version=_string(dsh.get("bridge_protocol_version"), "2.0"),
                restart_limit=int(_number(dsh.get("restart_limit"), 2)),
                restart_delay_ms=int(_number(dsh.get("restart_delay_ms"), 1000)),
                expected_version=_string(dsh.get("expected_version"), "0.2.0-rc.2"),
                expected_commit=_string(dsh.get("expected_commit"), "639ed015397290b3745d163aafe02ffee4aa3f84"),
            ),
            persona=PersonaConfig(
                directory=paths.persona if paths.separated else path_or_none(persona.get("directory")) or root / "config" / "persona",
                personality_file=_string(persona.get("personality_file"), "personality.md"),
                user_file=_string(persona.get("user_file"), "user.md"),
                instructions_file=_string(persona.get("instructions_file"), "instructions.md"),
            ),
            model_dir=model_dir,
            paths=paths,
            debug_audio_capture=_bool(
                data.get("debug_audio_capture"),
                os.environ.get("VOICE_DEBUG_AUDIO_CAPTURE", "false").lower() == "true",
            ),
            audio=AudioConfig(
                input_device=audio.get("input_device"),
                output_device=audio.get("output_device"),
                sample_rate=int(_number(audio.get("sample_rate"), 16000)),
                channels=int(_number(audio.get("channels"), 1)),
                sample_format=_string(audio.get("sample_format"), "int16"),
                block_ms=int(_number(audio.get("block_ms"), 20)),
                ring_buffer_ms=int(_number(audio.get("ring_buffer_ms"), 2000)),
                wake_preroll_ms=int(_number(audio.get("wake_preroll_ms"), 1200)),
                processing_enabled=_bool(audio.get("processing_enabled"), True),
                processing_required=_bool(audio.get("processing_required"), False),
                aec_failure_policy=_string(audio.get("aec_failure_policy"), "pause_recover").lower()
                if _string(audio.get("aec_failure_policy"), "pause_recover").lower()
                in {"pause_recover", "stop_and_listen", "continue_without_barge_in"}
                else "pause_recover",
                echo_cancellation=_bool(audio.get("echo_cancellation"), True),
                noise_suppression=_bool(audio.get("noise_suppression"), True),
                automatic_gain_control=_bool(audio.get("automatic_gain_control"), False),
                device_latency_ms=max(0, int(_number(audio.get("device_latency_ms"), 0))),
            ),
            wake=WakeConfig(
                enabled=wake_enabled,
                # Kept as a compatibility alias for the existing runtime
                # state/controller contract.  Product semantics are now
                # single-switch: enabling KWS always means standby.
                always_on=wake_enabled,
                provider=_string(wake.get("provider"), "sherpa_kws"),
                keyword=keyword,
                keyword_label=label,
                keywords=keywords,
                threshold=float(_number(wake.get("threshold"), 0.25)),
                keywords_score=float(_number(wake.get("keywords_score"), 1.0)),
                num_trailing_blanks=int(_number(wake.get("num_trailing_blanks"), 1)),
                max_active_paths=min(16, max(1, int(_number(wake.get("max_active_paths"), 4)))),
                device=_string(wake.get("device"), "cpu"),
                model_dir=path_or_none(kws_dir),
                keywords_file=paths.keywords if paths.separated else path_or_none(keywords_file),
            ),
            vad=VadConfig(
                model=_string(vad.get("model"), "fsmn-vad"),
                min_speech_ms=int(_number(vad.get("min_speech_ms"), 250)),
                end_silence_ms=int(_number(vad.get("end_silence_ms"), 200)),
                max_turn_ms=int(_number(vad.get("max_turn_ms"), 15000)),
            ),
            asr=AsrConfig(
                provider=_string(asr.get("provider"), "funasr"),
                model=_string(asr.get("model"), "paraformer-zh-streaming"),
                device=_string(asr.get("device"), "auto"),
                worker_url=worker_url,
                chunk_size=chunk_size,
                encoder_chunk_look_back=int(_number(asr.get("encoder_chunk_look_back"), 4)),
                decoder_chunk_look_back=int(_number(asr.get("decoder_chunk_look_back"), 1)),
            ),
            compute=ComputeConfig(
                devices={
                    **ComputeConfig().devices,
                    **({key: value for key, value in compute.get("devices", {}).items()
                        if key in COMPUTE_MODEL_IDS and isinstance(value, str) and value in COMPUTE_DEVICE_MODES}
                       if isinstance(compute.get("devices"), Mapping) else {}),
                },
                fallback_devices={
                    **ComputeConfig().fallback_devices,
                    **({key: value for key, value in compute.get("fallback_devices", {}).items()
                        if key in COMPUTE_MODEL_IDS and isinstance(value, str) and value in {"cpu", "cuda"}}
                       if isinstance(compute.get("fallback_devices"), Mapping) else {}),
                },
            ),
            llm=LlmConfig(
                provider=llm_provider,
                base_url=llm_base_url,
                model=llm_model,
                api_key_env=llm_api_key_env,
                stream=_bool(llm.get("stream"), True),
                reasoning_mode=_string(llm.get("reasoning_mode"), "fast"),
                thinking=_string(llm.get("thinking"), "enabled"),
                reasoning_effort=_string(llm.get("reasoning_effort"), "low"),
                allowed_reasoning=_reasoning_levels(
                    llm.get("allowed_reasoning"), ("off", "low"),
                ),
                timeout_ms=int(_number(llm.get("timeout_ms"), 15000)),
                transport=_string(llm.get("transport"), "chat_completions"),
                max_output_tokens=int(_number(llm.get("max_output_tokens"), 393216)),
                protocol_recovery_limit=max(0, int(_number(llm.get("protocol_recovery_limit"), 1))),
                transport_retry_limit=max(0, int(_number(llm.get("transport_retry_limit"), 2))),
            ),
            tts=TtsConfig(
                provider="minimax",
                # 历史 YAML 可能仍含 cosyvoice；产品配置加载固定使用在线 API。
                backend="minimax",
                base_url=os.environ.get("MINIMAX_API_HOST", "").strip() or _string(tts.get("base_url"), "https://api.minimaxi.com"),
                websocket_path=_string(tts.get("websocket_path"), "/ws/v1/t2a_v2"),
                transport=_string(tts.get("transport"), "websocket"),
                scheduling=_string(tts.get("scheduling"), "auto"),
                model=_string(tts.get("model"), "speech-2.8-hd"),
                api_key_env=_string(tts.get("api_key_env"), "MINIMAX_API_KEY"),
                output_format=_string(tts.get("output_format"), "pcm"),
                sample_rate=int(_number(tts.get("sample_rate"), 32000)),
                voice_id=_string(tts.get("voice_id"), "female-shaonv"),
                timeout_ms=int(_number(tts.get("timeout_ms"), 15000)),
                subtitle_enable=_bool(tts.get("subtitle_enable"), False),
                subtitle_type=_string(tts.get("subtitle_type"), "word_streaming"),
                audio_cache_max_bytes=int(_number(tts.get("audio_cache_max_bytes"), 64 * 1024 * 1024)),
                cancel_timeout_ms=int(_number(tts.get("cancel_timeout_ms"), 2000)),
                keepalive_seconds=int(_number(tts.get("keepalive_seconds"), 30)),
                emotion_mode=_string(tts.get("emotion_mode"), "agent"),
                default_emotion=_string(tts.get("default_emotion"), "calm"),
                emotion_transport=_string(tts.get("emotion_transport"), "voice_setting"),
                session_reuse=_bool(tts.get("session_reuse"), True),
                admission_lead_ms=int(_number(tts.get("admission_lead_ms"), 2500)),
                sentence_prefetch_ms=int(_number(tts.get("sentence_prefetch_ms"), 2000)),
                sentence_tick_ms=int(_number(tts.get("sentence_tick_ms"), 50)),
                sentence_min_effective_chars=int(_number(tts.get("sentence_min_effective_chars"), 10)),
                first_clause_timeout_ms=int(_number(tts.get("first_clause_timeout_ms"), 400)),
            ),
            text_bridge=TextBridgeConfig(
                min_chars=int(_number(bridge.get("min_chars"), 8)),
                max_chars=int(_number(bridge.get("max_chars"), 36)),
                max_wait_ms=int(_number(bridge.get("max_wait_ms"), 350)),
                preferred_breaks=_string(bridge.get("preferred_breaks"), "。！？；，、"),
            ),
            server=ServerConfig(
                host=os.environ.get("VOICE_SERVER_HOST", "").strip() or _string(server.get("host"), "127.0.0.1"),
                port=int(os.environ.get("VOICE_SERVER_PORT", "").strip() or _number(server.get("port"), 8765)),
            ),
            asr_worker=AsrWorkerConfig(
                host=worker_host,
                port=worker_port,
            ),
            speaker=SpeakerConfig(
                mode=_string(speaker.get("mode"), "off").lower(),
                owner_only_barge_in=speaker.get("owner_only_barge_in", False),
                confirm_threshold=float(_number(speaker.get("confirm_threshold"), 0.60)),
                continuity_threshold=float(_number(speaker.get("continuity_threshold"), 0.50)),
                window_ms=int(_number(speaker.get("window_ms"), 2000)),
                step_ms=int(_number(speaker.get("step_ms"), 500)),
                min_speech_ms=int(_number(speaker.get("min_speech_ms"), 700)),
                min_window_speech_ratio=float(_number(speaker.get("min_window_speech_ratio"), 0.40)),
                min_turn_speech_ms=int(_number(speaker.get("min_turn_speech_ms"), 1500)),
                wait_ms=int(_number(speaker.get("wait_ms"), 150)),
                model_id=_string(speaker.get("model_id"), "wespeaker-cnceleb-resnet34"),
            ),
            speaker_worker=SpeakerWorkerConfig(
                host=_string(speaker_worker.get("host"), "127.0.0.1"),
                port=int(_number(speaker_worker.get("port"), 8768)),
            ),
        )


def load_config(default_path: Path, local_path: Path | None = None) -> AppConfig:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 PyYAML，请先运行 scripts\\setup.ps1 安装后端依赖。") from exc

    paths = AppPaths.resolve(default_path.resolve().parent.parent)
    load_env_file(paths.env_file)
    data: dict[str, Any] = {}
    for path in (default_path, local_path):
        if path is None or not path.is_file():
            continue
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, Mapping):
            raise ValueError(f"配置文件必须是 YAML 对象：{path}")
        _deep_merge(data, dict(loaded))
    return AppConfig.from_mapping(data, base_dir=default_path.parent.parent)

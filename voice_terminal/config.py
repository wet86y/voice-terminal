from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .env import load_env_file
from .controller.options import InputOptions
from .agent.options import AgentOptions


def _string(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def _number(value: Any, default: int | float) -> int | float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


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
    provider: str = "sherpa_kws"
    keyword: str = "hei claw"
    keyword_label: str = "HEI_CLAW"
    threshold: float = 0.25
    keywords_score: float = 1.0
    num_trailing_blanks: int = 1
    device: str = "cpu"
    model_dir: Path | None = None
    keywords_file: Path | None = None


@dataclass(frozen=True)
class VadConfig:
    model: str = "fsmn-vad"
    min_speech_ms: int = 250
    end_silence_ms: int = 300
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


@dataclass(frozen=True)
class LlmConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    stream: bool = True
    reasoning_mode: str = "balanced"
    thinking: str = "enabled"
    reasoning_effort: str = "low"
    timeout_ms: int = 15000
    transport: str = "chat_completions"
    max_output_tokens: int = 512
    shadow_enabled: bool = False
    shadow_sample_rate: float = 1.0
    shadow_thinking_enabled: bool = False
    thinking_action_enabled: bool = False
    protocol_recovery_limit: int = 1
    transport_retry_limit: int = 2

    def __post_init__(self) -> None:
        if self.provider not in {"deepseek", "opencode_go"}:
            raise ValueError("llm.provider must be deepseek or opencode_go")
        if self.transport not in {"chat_completions", "responses"}:
            raise ValueError("llm.transport must be chat_completions or responses")
        if self.thinking not in {"disabled", "enabled"}:
            raise ValueError("llm.thinking must be disabled or enabled")
        if self.reasoning_mode not in {"fast", "balanced", "deep"}:
            raise ValueError("llm.reasoning_mode must be fast, balanced, or deep")
        if self.reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("llm.reasoning_effort must be low, high, or max")
        if self.max_output_tokens <= 0 or self.timeout_ms <= 0:
            raise ValueError("llm token and timeout limits must be positive")
        if not 0 <= self.shadow_sample_rate <= 1:
            raise ValueError("llm.shadow_sample_rate must be between zero and one")
        if self.protocol_recovery_limit < 0:
            raise ValueError("llm.protocol_recovery_limit cannot be negative")
        if self.transport_retry_limit < 0:
            raise ValueError("llm.transport_retry_limit cannot be negative")


@dataclass(frozen=True)
class TtsConfig:
    provider: str = "minimax"
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

    def __post_init__(self) -> None:
        if self.transport not in {"websocket", "websocket_bidi"}:
            raise ValueError("tts.transport must be websocket or websocket_bidi")
        if self.scheduling not in {"auto", "direct", "playback_window"}:
            raise ValueError("tts.scheduling must be auto, direct, or playback_window")
        if self.transport == "websocket_bidi" and self.scheduling == "playback_window":
            raise ValueError("Bidi TTS cannot use playback_window scheduling")
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
class ToolConfig:
    enabled: bool = True
    web_search: bool = True
    allowed_roots: tuple[str, ...] = ("docs",)


@dataclass(frozen=True)
class PersonaConfig:
    """Paths for the episode-scoped Markdown personality documents."""

    directory: Path = Path("config/persona")
    personality_file: str = "personality.md"
    user_file: str = "user.md"
    instructions_file: str = "instructions.md"


@dataclass(frozen=True)
class AppConfig:
    smart_turn: InputOptions = field(default_factory=InputOptions)
    agent: AgentOptions = field(default_factory=AgentOptions)
    tools: ToolConfig = field(default_factory=ToolConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    model_dir: Path = Path(r"D:\模型\voice-terminal")
    debug_audio_capture: bool = False
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    text_bridge: TextBridgeConfig = field(default_factory=TextBridgeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    asr_worker: AsrWorkerConfig = field(default_factory=AsrWorkerConfig)

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
    def resolved_asr_device(self) -> str:
        if self.asr.device != "auto":
            return self.asr.device
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
        persona = _section(data, "persona")

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

        keyword = _string(wake.get("keyword"), "hei claw")
        label = _string(wake.get("keyword_label"), "HEI_CLAW")
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
        tool_data = _section(data, "tools")
        smart_turn_data.update({
            "enabled": True,
            "tools": _bool(tool_data.get("enabled"), True),
            "web_search": _bool(tool_data.get("web_search"), True),
            "allowed_roots": tool_data.get("allowed_roots", ["docs"]),
        })
        return cls(
            smart_turn=InputOptions.from_mapping(smart_turn_data, root),
            agent=AgentOptions.from_mapping(_section(data, "agent")),
            tools=ToolConfig(enabled=_bool(tool_data.get("enabled"), True),
                             web_search=_bool(tool_data.get("web_search"), True),
                             allowed_roots=tuple(item for item in tool_data.get("allowed_roots", ["docs"])
                                                 if isinstance(item, str) and item.strip()) or ("docs",)),
            persona=PersonaConfig(
                directory=path_or_none(persona.get("directory")) or root / "config" / "persona",
                personality_file=_string(persona.get("personality_file"), "personality.md"),
                user_file=_string(persona.get("user_file"), "user.md"),
                instructions_file=_string(persona.get("instructions_file"), "instructions.md"),
            ),
            model_dir=model_dir,
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
                enabled=_env_bool("VOICE_WAKE_ENABLED", _bool(wake.get("enabled"), False)),
                provider=_string(wake.get("provider"), "sherpa_kws"),
                keyword=keyword,
                keyword_label=label,
                threshold=float(_number(wake.get("threshold"), 0.25)),
                keywords_score=float(_number(wake.get("keywords_score"), 1.0)),
                num_trailing_blanks=int(_number(wake.get("num_trailing_blanks"), 1)),
                device=_string(wake.get("device"), "cpu"),
                model_dir=path_or_none(kws_dir),
                keywords_file=path_or_none(keywords_file),
            ),
            vad=VadConfig(
                model=_string(vad.get("model"), "fsmn-vad"),
                min_speech_ms=int(_number(vad.get("min_speech_ms"), 250)),
                end_silence_ms=int(_number(vad.get("end_silence_ms"), 300)),
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
            llm=LlmConfig(
                provider=llm_provider,
                base_url=llm_base_url,
                model=llm_model,
                api_key_env=llm_api_key_env,
                stream=_bool(llm.get("stream"), True),
                reasoning_mode=_string(llm.get("reasoning_mode"), "balanced"),
                thinking=_string(llm.get("thinking"), "enabled"),
                reasoning_effort=_string(llm.get("reasoning_effort"), "low"),
                timeout_ms=int(_number(llm.get("timeout_ms"), 15000)),
                transport=_string(llm.get("transport"), "chat_completions"),
                max_output_tokens=int(_number(llm.get("max_output_tokens"), 512)),
                shadow_enabled=_bool(llm.get("shadow_enabled"), False),
                shadow_sample_rate=float(_number(llm.get("shadow_sample_rate"), 1.0)),
                shadow_thinking_enabled=_bool(llm.get("shadow_thinking_enabled"), False),
                thinking_action_enabled=_bool(llm.get("thinking_action_enabled"), False),
                protocol_recovery_limit=max(0, int(_number(llm.get("protocol_recovery_limit"), 1))),
                transport_retry_limit=max(0, int(_number(llm.get("transport_retry_limit"), 2))),
            ),
            tts=TtsConfig(
                provider=_string(tts.get("provider"), "minimax"),
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
        )


def load_config(default_path: Path, local_path: Path | None = None) -> AppConfig:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 PyYAML，请先运行 scripts\\setup.ps1 安装后端依赖。") from exc

    load_env_file(default_path.resolve().parent.parent / ".env.local")
    data: dict[str, Any] = {}
    for path in (default_path, local_path):
        if path is None or not path.is_file():
            continue
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, Mapping):
            raise ValueError(f"配置文件必须是 YAML 对象：{path}")
        _deep_merge(data, dict(loaded))
    return AppConfig.from_mapping(data, base_dir=default_path.parent.parent)

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from ..audio.capture import AudioCapture
from ..audio.devices import AudioDeviceError
from ..audio.player import AudioPlayerError, PcmPlayer
from ..audio.processing import AudioPreprocessor
from ..asr.client import AsrWorkerClient
from ..config import AppConfig
from ..controller.episode_controller import EpisodeController
from ..vad.barge_in import BargeInGate
from ..core.event_bus import EventBus
from ..core.events import VoiceEvent, event
from ..core.state import RuntimeState, TerminalState
from ..kws.detector import KwsDetector, KwsError
from ..kws.service import KwsWorker
from ..providers.deepseek import DeepSeekClient
from ..providers.minimax_search import MiniMaxSearchClient
from ..providers.minimax_tts import MiniMaxTtsClient
from ..agent.tool_registry import FileTools, WebSearchTools
from ..shadow_agent import ShadowAgentComponent, ShadowContextProjector, ShadowEvent
from ..shadow_agent.component import ShadowAgentSession
from ..shadow_agent.events import ShadowEventBuffer
from ..shadow_agent.prompts import load_shadow_instructions
from ..shadow_agent.provider import DeepSeekResponsesShadowProvider
from ..shadow_agent.tools import ShadowToolExecutor
from ..session.ledger import SessionLedger
from ..session.runtime import ConversationRuntime
from ..vad.detector import FsmnVadDetector, VadError
from ..model_registry.service import ModelRegistryError, ModelRegistryService
from ..model_registry.vault import CredentialVaultUnavailable
from .product import (
    ProductSettingsError,
    ProductSettingsStore,
    ShadowProductProjection,
    _threshold_to_sensitivity,
    apply_settings_to_config,
    sanitize_runtime_snapshot,
)
from .snapshot import ProtocolSequencer, snapshot_event

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # type: ignore
except ImportError:  # pragma: no cover - exercised by doctor/setup, not unit tests
    FastAPI = None  # type: ignore[assignment]
    WebSocket = Any  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]
    HTTPException = RuntimeError  # type: ignore[assignment,misc]


class SessionRuntime:
    """The sole production runtime: a session orchestrating input, agent and speech."""
    def __init__(self, config: AppConfig, *, product_settings: ProductSettingsStore | None = None) -> None:
        self.config = config
        self.product_settings = product_settings
        self._pending_product_settings: dict[str, Any] | None = None
        self.state = RuntimeState()
        self.bus = EventBus()
        self.history = SessionLedger(limit=10)
        self.startup_errors: list[str] = []
        self.capture: AudioCapture | None = None
        self.kws: KwsWorker | None = None
        self.vad: FsmnVadDetector | None = None
        self.asr: AsrWorkerClient | None = None
        self.llm: DeepSeekClient | None = None
        self.tts: MiniMaxTtsClient | None = None
        self.shadow_search_tools: WebSearchTools | None = None
        self.shadow_component: ShadowAgentComponent | None = None
        self.shadow_session: ShadowAgentSession | None = None
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
        self.shadow_context = ShadowContextProjector(
            self.history, user_profile=self._shadow_user_profile,
        )
        self._shadow_pending = ShadowEventBuffer()
        self.shadow_product = ShadowProductProjection()
        self._shadow_debug_sinks: dict[str, Any] = {}
        self.player: PcmPlayer | None = None
        self.audio_processor: AudioPreprocessor | None = None
        self.controller: EpisodeController | None = None
        self.conversation = ConversationRuntime()
        self._session_lock = asyncio.Lock()
        self.build_info: dict[str, Any] = {}
        self.reasoning_mode = config.llm.reasoning_mode
        self._credential_overrides: dict[str, str] = {}
        self.state.wake_always_on = bool(config.wake.enabled)

    def _build_kws_worker(self) -> KwsWorker:
        detector = KwsDetector(
            model_dir=self.config.kws_dir,
            keywords_file=self.config.kws_keywords_file,
            keyword_label=self.config.wake.keyword_label,
            threshold=self.config.wake.threshold,
            keywords_score=self.config.wake.keywords_score,
            num_trailing_blanks=self.config.wake.num_trailing_blanks,
            provider=self.config.wake.device,
            sample_rate=self.config.audio.sample_rate,
        )
        worker = KwsWorker(detector)
        worker.start()
        return worker

    def _record_error(self, service: str, message: str, *, status_service: str | None = None) -> None:
        # Keep the public service map compatible while preserving the actual
        # failing component in startup diagnostics (for example, VAD failure
        # makes the ASR chain unavailable but is not an ASR model failure).
        self.state.set_service(status_service or service, "ERROR")
        self.startup_errors.append(f"{service}: {message}")
        logger.error("%s", self.startup_errors[-1])

    def _disable_optional_search(self, reason: str) -> None:
        safe_reason = " ".join(str(reason or "搜索服务不可用").split())[:240]
        self.state.set_service("web_search", "DISABLED")
        self.startup_errors.append(f"web_search: disabled: {safe_reason or '搜索服务不可用'}")
        logger.warning("%s", self.startup_errors[-1])

    async def initialize(self) -> None:
        loop = asyncio.get_running_loop()
        self.shadow_product.reset(available=False)
        try:
            self.capture = AudioCapture(
                loop=loop,
                sample_rate=self.config.audio.sample_rate,
                channels=self.config.audio.channels,
                block_ms=self.config.audio.block_ms,
                input_device=self.config.audio.input_device,
                ring_buffer_ms=self.config.audio.ring_buffer_ms,
            )
            self.state.set_service("audio", "READY")
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
        except Exception as exc:
            self._record_error("audio_processing", str(exc), status_service="audio")
            self.state.current.update(
                audio_processing_status="ERROR",
                audio_processing_error=str(exc),
            )

        if self.config.wake.enabled:
            try:
                self.kws = self._build_kws_worker()
                self.state.set_service("kws", "READY")
            except Exception as exc:
                self._record_error("kws", str(exc))
        else:
            self.state.set_service("kws", "DISABLED")
            logger.info("wake word disabled; session.start enters the voice turn directly")

        try:
            self.vad = FsmnVadDetector(
                model_dir=self.config.vad_model_dir,
                device="cpu",
                sample_rate=self.config.audio.sample_rate,
                end_silence_ms=300 if self.config.smart_turn.enabled else None,
            )
        except Exception as exc:
            self._record_error("vad", str(exc), status_service="asr")

        self.asr = AsrWorkerClient(self.config.asr.worker_url)
        try:
            await self.asr.connect()
            await self.asr.health()
            if self.vad is not None:
                self.state.set_service("asr", "READY")
        except Exception as exc:
            code = getattr(exc, "code", "ASR_WORKER_UNAVAILABLE")
            self._record_error("asr", f"{code}: {exc}")

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
                thinking_action_enabled=self.config.llm.thinking_action_enabled,
                protocol_recovery_limit=self.config.llm.protocol_recovery_limit,
                transport_retry_limit=self.config.llm.transport_retry_limit,
            )
            self.state.set_service("llm", "READY")
        else:
            self._record_error("llm", f"未配置 {self.config.llm.api_key_env}")

        shadow_cfg = self.config.shadow_agent
        if shadow_cfg.enabled:
            shadow_key = self._api_key(shadow_cfg.provider.api_key_env)
            if not shadow_key:
                self.state.set_service("shadow_agent", "DISABLED")
                logger.warning("shadow agent disabled: missing %s", shadow_cfg.provider.api_key_env)
            else:
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
                    )
                    self.history.set_shadow_runtime_state(available=True, active=False)
                    self.shadow_product.reset(available=True)
                    self.state.set_service("shadow_agent", "READY")
                except Exception as exc:
                    self.state.set_service("shadow_agent", "ERROR")
                    self.state.set_service("web_search", "DISABLED")
                    logger.exception("shadow agent initialization failed: %s", exc)
        else:
            self.shadow_product.reset(available=False)
            self.state.set_service("shadow_agent", "DISABLED")
            self.state.set_service("web_search", "DISABLED")

        minimax_key = self._api_key(self.config.tts.api_key_env)

        if minimax_key:
            self.tts = MiniMaxTtsClient(
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
            )
            self.state.current.update(
                tts_transport=self.config.tts.transport,
                tts_scheduling=self.config.tts.effective_scheduling,
            )
            try:
                if self.config.tts.output_format.lower() != "pcm":
                    raise RuntimeError("TTS output_format 必须为 pcm，当前播放器不自动回退 MP3。")
                self.player = PcmPlayer(
                    sample_rate=self.config.tts.sample_rate,
                    channels=1,
                    output_device=self.config.audio.output_device,
                    device_latency_ms=self.config.audio.device_latency_ms,
                    on_audio_rendered=(self.audio_processor.push_reverse if self.audio_processor else None),
                )
                self.player.start()
                self.state.current["playback_diagnostics"] = self.player.diagnostics
                self.state.set_service("tts", "READY")
            except Exception as exc:
                self._record_error("tts", str(exc))
        else:
            self._record_error("tts", f"未配置 {self.config.tts.api_key_env}")

        if self.config.audio.sample_rate != 16000 or self.config.audio.channels != 1 or self.config.audio.sample_format != "int16":
            raise ValueError("session runtime requires 16 kHz mono int16 capture")
        self.controller = EpisodeController(
            options=self.config.smart_turn, agent_options=self.config.agent,
            persona_config=self.config.persona,
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
            barge_in_gate=BargeInGate(min_speech_ms=max(250, self.config.vad.min_speech_ms), enabled=True),
            conversation=self.conversation,
        )
        self.controller.set_reasoning_mode(self.reasoning_mode)
        await self.controller.initialize()

    def snapshot(self) -> VoiceEvent:
        value = snapshot_event(self.state)
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
        value.payload["reasoning_mode"] = self.reasoning_mode
        value.payload["shadow"] = self._shadow_product_snapshot()
        return value

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
            }
        runtime_snapshot = None
        session = getattr(self, "shadow_session", None)
        if session is not None:
            try:
                runtime_snapshot = session.snapshot()
            except Exception:
                logger.exception("shadow.product_snapshot_failed")
        return projection.snapshot(runtime_snapshot)

    async def publish_shadow_product(self) -> None:
        await self.bus.publish(event("shadow.product", None, **self._shadow_product_snapshot()))

    def stage_product_settings(self, settings: Mapping[str, Any]) -> None:
        self._pending_product_settings = dict(settings)

    async def _apply_pending_product_settings(self) -> bool:
        """Apply staged product settings at the next safe start boundary."""

        pending = self._pending_product_settings
        if pending is None or self.state.listening:
            return True
        previous_config = self.config
        next_config = apply_settings_to_config(self.config, pending)
        wake_changed = next_config.wake != previous_config.wake
        try:
            if wake_changed and self.kws is not None:
                self.kws.close()
                self.kws = None
            self.config = next_config
            self.state.wake_always_on = bool(self.config.wake.enabled)
            if self.controller is not None:
                set_agent_options = getattr(self.controller, "set_agent_options", None)
                if callable(set_agent_options):
                    set_agent_options(self.config.agent)
                else:
                    # Compatibility fixtures may expose only the old public
                    # attribute; live EpisodeController has the boundary
                    # aware setter above.
                    self.controller.agent_options = self.config.agent
                self.controller.wake_enabled = self.config.wake.enabled
                self.controller.wake_always_on = self.state.wake_always_on
                self.controller.state.wake_always_on = self.state.wake_always_on
                self.controller.wake_keyword = self.config.wake.keyword
            if wake_changed:
                if self.config.wake.enabled:
                    self.kws = self._build_kws_worker()
                    self.state.set_service("kws", "READY")
                else:
                    self.state.set_service("kws", "DISABLED")
                if self.controller is not None:
                    self.controller.kws = self.kws
            self._pending_product_settings = None
            return True
        except Exception as exc:
            logger.exception("product.settings_apply_failed")
            # Restore the previous in-memory config and leave the persisted
            # setting pending so the next start can retry after the asset or
            # model problem has been corrected.
            self.config = previous_config
            self.state.wake_always_on = bool(previous_config.wake.enabled)
            if self.controller is not None:
                set_agent_options = getattr(self.controller, "set_agent_options", None)
                if callable(set_agent_options):
                    set_agent_options(previous_config.agent)
                else:
                    self.controller.agent_options = previous_config.agent
                self.controller.wake_enabled = previous_config.wake.enabled
                self.controller.wake_always_on = self.state.wake_always_on
                self.controller.state.wake_always_on = self.state.wake_always_on
                self.controller.wake_keyword = previous_config.wake.keyword
            if wake_changed:
                try:
                    if previous_config.wake.enabled:
                        self.kws = self._build_kws_worker()
                        self.state.set_service("kws", "READY")
                    else:
                        self.kws = None
                        self.state.set_service("kws", "DISABLED")
                    if self.controller is not None:
                        self.controller.kws = self.kws
                except Exception as restore_exc:
                    self.kws = None
                    if self.controller is not None:
                        self.controller.kws = None
                    self._record_error("kws", str(restore_exc))
            self.startup_errors.append(f"product_settings: {str(exc)[:300]}")
            return False

    async def set_reasoning_mode(self, mode: str) -> bool:
        if mode not in {"fast", "balanced", "deep"}:
            return False
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
        if any(provider.get("protocol") not in allowed_llm for provider in (voice_provider, shadow_provider, status_provider)):
            raise ValueError("所选 LLM 供应商尚未安装兼容的运行时适配器。")
        if tts_provider.get("protocol") != "minimax":
            raise ValueError("所选 TTS 供应商尚未安装兼容的运行时适配器。")
        profile = tts.get("profile") if isinstance(tts.get("profile"), Mapping) else {}
        if str(profile.get("output_format") or "pcm").lower() != "pcm":
            raise ValueError("当前原生播放器仅支持 PCM TTS 输出。")

        previous_config = self.config
        voice_env = "VOICE_PRODUCT_VOICE_LLM_KEY"
        shadow_env = "VOICE_PRODUCT_SHADOW_LLM_KEY"
        status_env = "VOICE_PRODUCT_STATUS_LLM_KEY"
        tts_env = "VOICE_PRODUCT_TTS_KEY"
        overrides: dict[str, str] = {}
        for item, environment_name in (
            (voice, voice_env), (shadow, shadow_env),
            (status, status_env), (tts, tts_env),
        ):
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
        next_shadow = replace(
            previous_config.shadow_agent,
            provider=replace(
                previous_config.shadow_agent.provider,
                base_url=str(shadow_provider.get("base_url") or previous_config.shadow_agent.provider.base_url),
                model=str(shadow.get("model_id") or previous_config.shadow_agent.provider.model),
                api_key_env=shadow_env if shadow_env in overrides else previous_config.shadow_agent.provider.api_key_env,
            ),
            max_context_tokens=int(
                shadow_settings.get("max_context_tokens")
                or shadow.get("max_context_tokens")
                or previous_config.shadow_agent.max_context_tokens
            ),
            reasoning_effort=str(
                shadow_settings.get("reasoning_effort")
                or previous_config.shadow_agent.reasoning_effort
            ),
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
                provider="minimax",
                base_url=str(tts_provider.get("base_url")),
                model=str(tts.get("model_id")),
                api_key_env=tts_env if tts_env in overrides else previous_config.tts.api_key_env,
                voice_id=str(profile.get("voice_id") or previous_config.tts.voice_id),
                emotion_mode=str(profile.get("emotion_mode") or previous_config.tts.emotion_mode),
                default_emotion=str(profile.get("default_emotion") or previous_config.tts.default_emotion),
                output_format="pcm",
                sample_rate=int(profile.get("sample_rate") or previous_config.tts.sample_rate),
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
        self.state.agent = {}
        self.state.input_owner = "none"
        self.state.set_state(TerminalState.STARTING, listening=False)

    async def start_listening(self) -> None:
        async with self._session_lock:
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
            await self.controller.start_listening()
            pending_buffer = getattr(self, "_shadow_pending", None)
            pending = pending_buffer.drain() if pending_buffer is not None else []
            if pending:
                await self.controller.accept_shadow_events(pending)

    async def stop_listening(self) -> None:
        async with self._session_lock:
            if self.controller is not None:
                await self.controller.stop_listening()

    async def reset_session(self) -> None:
        async with self._session_lock:
            if self.controller is not None and hasattr(self.controller, "reset_session"):
                await self.controller.reset_session()
            shadow_session = getattr(self, "shadow_session", None)
            if shadow_session is not None:
                await shadow_session.reset()
            pending_buffer = getattr(self, "_shadow_pending", None)
            if pending_buffer is not None:
                pending_buffer.drain()
            self.history.reset()
            product_settings = getattr(self, "product_settings", None)
            if product_settings is not None:
                shadow_user_profile = product_settings.read_persona().get("user", "")
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

    async def close(self) -> None:
        if self.controller is not None:
            await self.controller.close()
        if self.kws is not None:
            self.kws.close()
        shadow_component = getattr(self, "shadow_component", None)
        if shadow_component is not None:
            await shadow_component.close()
        if self.tts is not None:
            await self.tts.close()
        if self.llm is not None:
            await self.llm.close()
        if self.audio_processor is not None:
            self.audio_processor.close()
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
        self.audio_processor = None
        self.shadow_search_tools = None
        self.shadow_component = None
        self.shadow_session = None

    def _wake_shadow(self) -> str:
        if self.shadow_session is None or not self.shadow_session.available:
            return "unavailable"
        return self.shadow_session.wake()

    async def _on_shadow_events(self, events: list[ShadowEvent]) -> None:
        # Stop intentionally ends only the voice episode.  The independent
        # manager continues, while its coalesced events wait for the next
        # session.start.
        if self._shadow_debug_sinks:
            await asyncio.gather(
                *(sink(events) for sink in tuple(self._shadow_debug_sinks.values())),
                return_exceptions=True,
            )
        self.shadow_product.update_events(events)
        await self.publish_shadow_product()
        controller = self.controller
        if controller is None or not self.state.listening or controller.episode.closed:
            for item in events:
                self._shadow_pending.publish(item)
            return
        await controller.accept_shadow_events(events)

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

    def health_payload(self) -> dict[str, Any]:
        payload = {
            "state": self.state.state.value,
            "listening": self.state.listening,
            "can_start": self.state.can_start,
            "services": dict(self.state.services),
            "startup_errors": list(self.startup_errors),
            "wake_enabled": self.config.wake.enabled,
            "wake_always_on": self.state.wake_always_on,
            "input_owner": self.state.input_owner,
            "wake_keyword": self.config.wake.keyword,
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


def _valid_command(message: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(message, dict) or message.get("type") != "command":
        return None
    request_id = message.get("request_id")
    command = message.get("command")
    payload = message.get("payload")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    if not isinstance(command, str) or not isinstance(payload, dict):
        return None
    return command, payload


def create_app(runtime: SessionRuntime, *, lifespan: Any = None,
               model_registry: ModelRegistryService | None = None,
               product_settings: ProductSettingsStore | None = None) -> Any:
    if FastAPI is None:
        raise RuntimeError("缺少 fastapi，请先运行 scripts\\setup.ps1。")

    app = FastAPI(title="Voice Terminal Server", version="0.8.0", lifespan=lifespan)
    try:
        from fastapi.middleware.cors import CORSMiddleware  # type: ignore
        configured_origins = [
            item.strip() for item in os.environ.get("VOICE_WEB_ORIGINS", "").split(",")
            if item.strip() and item.strip() != "*"
        ]
        origins = list(dict.fromkeys([
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            *configured_origins,
        ]))
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "PUT", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type"],
        )
    except ImportError:  # pragma: no cover - FastAPI installs middleware with the app
        pass

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return runtime.health_payload()

    def registry_or_404() -> ModelRegistryService:
        if model_registry is None:
            raise HTTPException(status_code=404, detail="模型管理服务尚未启用。")
        return model_registry

    def registry_error(exc: Exception) -> None:
        if isinstance(exc, CredentialVaultUnavailable):
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if isinstance(exc, (ModelRegistryError, ProductSettingsError)):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise exc

    @app.get("/api/v1/product/snapshot")
    async def get_product_snapshot() -> dict[str, Any]:
        runtime_payload = sanitize_runtime_snapshot(runtime.snapshot().payload)
        conversation = runtime_payload.pop("conversation", {"recent": [], "current": {}})
        settings = product_settings.snapshot() if product_settings is not None else {
            "wake": {
                "enabled": runtime.config.wake.enabled,
                "keyword": runtime.config.wake.keyword,
                "keyword_label": runtime.config.wake.keyword_label,
                "sensitivity": _threshold_to_sensitivity(runtime.config.wake.threshold),
            },
            "silence": {
                "seconds": int(getattr(runtime.config.agent, "final_silence_ms", 30000) / 1000),
                "smart_turn_hard_silence_ms": runtime.config.smart_turn.hard_silence_ms,
            },
            "persona": {},
            "warnings": [],
        }
        models = await model_registry.snapshot() if model_registry is not None else {
            "providers": [], "models": [], "tts_profiles": [], "assignments": {},
        }
        restore_warnings = [
            str(item)[:320]
            for item in getattr(runtime, "startup_errors", [])
            if str(item).startswith("model_assignment_restore")
        ]
        if restore_warnings:
            settings["warnings"] = list(dict.fromkeys([
                *[str(item) for item in settings.get("warnings", [])],
                *restore_warnings,
            ]))
        return {
            "schema_version": 1,
            "runtime": runtime_payload,
            "conversation": conversation,
            "shadow": runtime._shadow_product_snapshot(),
            "settings": settings,
            "models": models,
            "warnings": restore_warnings,
        }

    def product_store_or_404() -> ProductSettingsStore:
        if product_settings is None:
            raise HTTPException(status_code=404, detail="产品设置服务尚未启用。")
        return product_settings

    @app.put("/api/v1/product/settings/wake")
    async def update_product_wake(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            value = await store.save_wake(body)
            runtime.stage_product_settings(store.settings_only())
            return {
                "settings": value,
                "effective_from": "next_session",
                "pending": True,
                "message": "KWS 设置已保存，将在下一次会话生效。",
            }
        except Exception as exc:
            product_error = exc
            if isinstance(product_error, HTTPException):
                raise
            registry_error(product_error)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/silence")
    async def update_product_silence(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            value = await store.save_silence(body)
            runtime.stage_product_settings(store.settings_only())
            return {
                "settings": value,
                "effective_from": "next_episode",
                "pending": True,
                "message": "静默设置已保存，将在下一次 Episode 生效。",
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.put("/api/v1/product/settings/persona")
    async def update_product_persona(body: dict[str, Any]) -> dict[str, Any]:
        try:
            store = product_store_or_404()
            value = await asyncio.to_thread(store.save_persona, body)
            return {
                "settings": {"persona": value.get("persona", {})},
                "effective_from": "next_episode",
                "pending": True,
                "message": "Persona 已保存，将在下一次 Episode/会话生效。",
            }
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.get("/api/v1/models/registry")
    async def get_model_registry() -> dict[str, Any]:
        try:
            return await registry_or_404().snapshot()
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/scan")
    async def scan_models() -> dict[str, Any]:
        try:
            return await registry_or_404().scan()
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/validate")
    async def validate_model_provider(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await registry_or_404().validate_connection(body)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/providers")
    async def create_model_provider(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await registry_or_404().create_verified_provider(body)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/{model_id}/hide")
    async def hide_model(model_id: str) -> dict[str, Any]:
        try:
            return await registry_or_404().hide(model_id)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/{model_id}/restore")
    async def restore_model(model_id: str) -> dict[str, Any]:
        try:
            return await registry_or_404().restore(model_id)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.patch("/api/v1/models/{model_id}")
    async def update_model(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await registry_or_404().update_model(model_id, body)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.delete("/api/v1/models/{model_id}")
    async def delete_model(model_id: str) -> dict[str, Any]:
        try:
            return await registry_or_404().delete_model(model_id)
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.post("/api/v1/models/assignments/apply")
    async def apply_model_assignments(body: dict[str, Any]) -> dict[str, Any]:
        assignments = body.get("assignments")
        if not isinstance(assignments, dict):
            raise HTTPException(status_code=422, detail="assignments 必须是对象。")
        try:
            result = await registry_or_404().apply_assignments(
                assignments, confirmed=body.get("confirmed") is True,
            )
            if result.get("applied") is True:
                registry_or_404().config = runtime.config
            return result
        except Exception as exc:
            registry_error(exc)
            raise AssertionError("unreachable")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        logger.info("websocket.connected peer=%s", websocket.client)
        sequencer = ProtocolSequencer(lambda: runtime.conversation.conversation_id)
        subscription = runtime.bus.subscribe()
        connection_id = uuid4().hex
        debug_events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        event_task = asyncio.create_task(_forward_events(websocket, subscription, sequencer), name="websocket-events")
        debug_task = asyncio.create_task(_forward_debug_events(websocket, debug_events, sequencer), name="websocket-shadow-debug")
        # Let the forwarding task register its queue before a fast
        # session.start command can publish the first turn event.
        await asyncio.sleep(0)
        try:
            while True:
                message = await websocket.receive_json()
                parsed = _valid_command(message)
                if parsed is None:
                    logger.warning("websocket.invalid_command peer=%s", websocket.client)
                    await _send_error(websocket, sequencer, "INVALID_COMMAND", "命令格式无效。", None, True)
                    continue
                command, _payload = parsed
                logger.info("websocket.command peer=%s command=%s", websocket.client, command)
                if command == "subscribe":
                    await websocket.send_json(sequencer.envelope(runtime.snapshot()))
                elif command == "session.start":
                    await runtime.start_listening()
                elif command == "session.stop":
                    await runtime.stop_listening()
                elif command == "session.reset":
                    await runtime.reset_session()
                elif command == "session.reasoning_mode":
                    mode = _payload.get("mode")
                    if not isinstance(mode, str) or not await runtime.set_reasoning_mode(mode):
                        await _send_error(websocket, sequencer, "INVALID_REASONING_MODE", "思考模式必须是 fast、balanced 或 deep。", None, True)
                elif command == "shadow.debug.wake":
                    async def debug_sink(items: list[ShadowEvent]) -> None:
                        for item in items:
                            await debug_events.put(event(
                                "shadow.debug.event", None,
                                event_id=item.event_id, kind=item.kind, text=item.text,
                                wake_policy=item.wake_policy, payload=item.payload,
                            ))
                    try:
                        result = runtime.debug_shadow_wake(connection_id, event_sink=debug_sink)
                        await debug_events.put(event(
                            "shadow.debug.wake", None, request_id=message["request_id"], **result,
                        ))
                    except ValueError as exc:
                        code = str(exc)
                        await _send_error(websocket, sequencer,
                                          code if code in {"SHADOW_DEBUG_DISABLED", "SHADOW_UNAVAILABLE"} else "INVALID_SHADOW_DEBUG_WAKE",
                                          "影子调试唤醒不可受理。", None, True)
                else:
                    await _send_error(websocket, sequencer, "UNKNOWN_COMMAND", f"不支持的命令：{command}", None, True)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.info("WebSocket closed: %s", exc)
        finally:
            logger.info("websocket.disconnected peer=%s", websocket.client)
            event_task.cancel()
            debug_task.cancel()
            await asyncio.gather(event_task, debug_task, return_exceptions=True)
            await runtime.close_shadow_debug(connection_id)

    return app


async def _forward_events(websocket: Any, subscription: Any, sequencer: ProtocolSequencer) -> None:
    try:
        async for value in subscription:
            if not sequencer.public(value):
                continue
            if value.type == "speech.completed":
                payload = value.payload if isinstance(value.payload, dict) else {}
                delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
                logger.info("websocket.speech_completed input_id=%s speech_id=%s status=%s delivery=%s",
                            payload.get("input_id") or value.turn_id, payload.get("speech_id"), payload.get("status"), delivery.get("status"))
            else:
                logger.debug("websocket.event type=%s input_id=%s", value.type, value.payload.get("input_id") or value.turn_id)
            await websocket.send_json(sequencer.envelope(value))
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            await websocket.close(code=1013)
        except Exception:
            pass
        return


async def _forward_debug_events(websocket: Any, queue: asyncio.Queue[VoiceEvent], sequencer: ProtocolSequencer) -> None:
    try:
        while True:
            value = await queue.get()
            await websocket.send_json(sequencer.envelope(value))
    except asyncio.CancelledError:
        raise


async def _send_error(
    websocket: Any,
    sequencer: ProtocolSequencer,
    code: str,
    message: str,
    turn_id: str | None,
    recoverable: bool,
) -> None:
    await websocket.send_json(
        sequencer.envelope(event("error", turn_id, code=code, message=message, recoverable=recoverable))
    )

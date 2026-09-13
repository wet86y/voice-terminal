from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
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
from .snapshot import ProtocolSequencer, snapshot_event

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # type: ignore
except ImportError:  # pragma: no cover - exercised by doctor/setup, not unit tests
    FastAPI = None  # type: ignore[assignment]
    WebSocket = Any  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]


class SessionRuntime:
    """The sole production runtime: a session orchestrating input, agent and speech."""
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.state = RuntimeState()
        self.bus = EventBus()
        self.history = SessionLedger(limit=10)
        self.startup_errors: list[str] = []
        self.capture: AudioCapture | None = None
        self.kws: KwsDetector | None = None
        self.vad: FsmnVadDetector | None = None
        self.asr: AsrWorkerClient | None = None
        self.llm: DeepSeekClient | None = None
        self.tts: MiniMaxTtsClient | None = None
        self.shadow_search_tools: WebSearchTools | None = None
        self.shadow_component: ShadowAgentComponent | None = None
        self.shadow_session: ShadowAgentSession | None = None
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
        self._shadow_debug_sinks: dict[str, Any] = {}
        self.player: PcmPlayer | None = None
        self.audio_processor: AudioPreprocessor | None = None
        self.controller: EpisodeController | None = None
        self.conversation = ConversationRuntime()
        self._session_lock = asyncio.Lock()
        self.build_info: dict[str, Any] = {}
        self.reasoning_mode = config.llm.reasoning_mode

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
                self.kws = KwsDetector(
                    model_dir=self.config.kws_dir,
                    keywords_file=self.config.kws_keywords_file,
                    keyword_label=self.config.wake.keyword_label,
                    threshold=self.config.wake.threshold,
                    keywords_score=self.config.wake.keywords_score,
                    num_trailing_blanks=self.config.wake.num_trailing_blanks,
                    provider=self.config.wake.device,
                    sample_rate=self.config.audio.sample_rate,
                )
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

        llm_key = self.config.api_key(self.config.llm.api_key_env)
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
            shadow_key = self.config.api_key(shadow_cfg.provider.api_key_env)
            if not shadow_key:
                self.state.set_service("shadow_agent", "DISABLED")
                logger.warning("shadow agent disabled: missing %s", shadow_cfg.provider.api_key_env)
            else:
                try:
                    search_cfg = shadow_cfg.tools.web_search
                    search_key = self.config.api_key(search_cfg.api_key_env)
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
                    self.shadow_component = ShadowAgentComponent(
                        provider, executor, instructions_loader=load_shadow_instructions,
                        runtime_options={
                            "provider_timeout_ms": shadow_cfg.provider.timeout_ms,
                            "transport_retry_limit": shadow_cfg.provider.transport_retry_limit,
                            "max_context_tokens": shadow_cfg.max_context_tokens,
                            "compaction_threshold_ratio": shadow_cfg.compaction_threshold_ratio,
                            "observer_enabled": shadow_cfg.status_observer.enabled,
                            "observer_every_steps": shadow_cfg.status_observer.every_committed_steps,
                            "observer_max_output_tokens": shadow_cfg.status_observer.max_output_tokens,
                            "observer_timeout_ms": shadow_cfg.status_observer.timeout_ms,
                            "on_metric": self._on_shadow_metric,
                        },
                    )
                    self.shadow_session = self.shadow_component.new_session(
                        self.shadow_context,
                        production_sink=self._on_shadow_events,
                    )
                    self.history.set_shadow_runtime_state(available=True, active=False)
                    self.state.set_service("shadow_agent", "READY")
                except Exception as exc:
                    self.state.set_service("shadow_agent", "ERROR")
                    self.state.set_service("web_search", "DISABLED")
                    logger.exception("shadow agent initialization failed: %s", exc)
        else:
            self.state.set_service("shadow_agent", "DISABLED")
            self.state.set_service("web_search", "DISABLED")

        minimax_key = self.config.api_key(self.config.tts.api_key_env)

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
        return value

    async def set_reasoning_mode(self, mode: str) -> bool:
        if mode not in {"fast", "balanced", "deep"}:
            return False
        async with self._session_lock:
            self.reasoning_mode = mode
            if self.controller is not None:
                self.controller.set_reasoning_mode(mode)
            await self.bus.publish(self.snapshot())
        return True

    async def start_listening(self) -> None:
        async with self._session_lock:
            if self.controller is None:
                await self.bus.publish(event("error", code="RUNTIME_NOT_INITIALIZED", message="Voice Runtime 尚未初始化。", recoverable=False))
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
            self.shadow_context = ShadowContextProjector(
                self.history, user_profile=getattr(self, "_shadow_user_profile", ""),
            )
            if shadow_session is not None:
                shadow_session.replace_context_source(self.shadow_context)
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

    async def close(self) -> None:
        if self.controller is not None:
            await self.controller.close()
        shadow_component = getattr(self, "shadow_component", None)
        if shadow_component is not None:
            await shadow_component.close()
        if self.tts is not None:
            await self.tts.close()
        if self.llm is not None:
            await self.llm.close()
        if self.audio_processor is not None:
            self.audio_processor.close()

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
        return {
            "state": self.state.state.value,
            "listening": self.state.listening,
            "can_start": self.state.can_start,
            "services": dict(self.state.services),
            "startup_errors": list(self.startup_errors),
            "wake_enabled": self.config.wake.enabled,
            "wake_keyword": self.config.wake.keyword,
            "runtime": dict(self.build_info),
        }


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


def create_app(runtime: SessionRuntime, *, lifespan: Any = None) -> Any:
    if FastAPI is None:
        raise RuntimeError("缺少 fastapi，请先运行 scripts\\setup.ps1。")

    app = FastAPI(title="Voice Terminal Server", version="0.7.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return runtime.health_payload()

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

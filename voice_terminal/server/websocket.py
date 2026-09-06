from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..audio.capture import AudioCapture
from ..audio.devices import AudioDeviceError
from ..audio.player import AudioPlayerError, PcmPlayer
from ..audio.processing import AudioPreprocessor
from ..asr.client import AsrWorkerClient
from ..config import AppConfig
from ..controller.turn_controller import TurnController
from ..controller.realtime_controller import RealtimeController
from ..vad.barge_in import BargeInGate
from ..core.event_bus import EventBus
from ..core.events import VoiceEvent, event
from ..core.state import RuntimeState, TerminalState
from ..kws.detector import KwsDetector, KwsError
from ..providers.deepseek import DeepSeekClient
from ..providers.minimax_tts import MiniMaxTtsClient
from ..server.history import InMemoryHistory
from ..vad.detector import FsmnVadDetector, VadError
from .snapshot import ProtocolSequencer, snapshot_event

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # type: ignore
except ImportError:  # pragma: no cover - exercised by doctor/setup, not unit tests
    FastAPI = None  # type: ignore[assignment]
    WebSocket = Any  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]


class VoiceRuntime:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.state = RuntimeState()
        self.bus = EventBus()
        self.history = InMemoryHistory(limit=10)
        self.startup_errors: list[str] = []
        self.capture: AudioCapture | None = None
        self.kws: KwsDetector | None = None
        self.vad: FsmnVadDetector | None = None
        self.asr: AsrWorkerClient | None = None
        self.llm: DeepSeekClient | None = None
        self.tts: MiniMaxTtsClient | None = None
        self.player: PcmPlayer | None = None
        self.audio_processor: AudioPreprocessor | None = None
        self.controller: TurnController | None = None
        self._session_lock = asyncio.Lock()

    def _record_error(self, service: str, message: str, *, status_service: str | None = None) -> None:
        # Keep the public service map compatible while preserving the actual
        # failing component in startup diagnostics (for example, VAD failure
        # makes the ASR chain unavailable but is not an ASR model failure).
        self.state.set_service(status_service or service, "ERROR")
        self.startup_errors.append(f"{service}: {message}")
        logger.error("%s", self.startup_errors[-1])

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
                end_silence_ms=300 if self.config.v02.enabled else None,
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

        deepseek_key = self.config.api_key(self.config.llm.api_key_env)
        if deepseek_key:
            self.llm = DeepSeekClient(
                base_url=self.config.llm.base_url,
                model=self.config.llm.model,
                api_key=deepseek_key,
                timeout_ms=self.config.llm.timeout_ms,
                thinking=self.config.llm.thinking,
            )
            self.state.set_service("llm", "READY")
        else:
            self._record_error("llm", f"未配置 {self.config.llm.api_key_env}")

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
                self.state.set_service("tts", "READY")
            except Exception as exc:
                self._record_error("tts", str(exc))
        else:
            self._record_error("tts", f"未配置 {self.config.tts.api_key_env}")

        if self.config.v02.enabled and (self.config.audio.sample_rate != 16000 or self.config.audio.channels != 1 or self.config.audio.sample_format != "int16"):
            raise ValueError("v0.2 requires 16 kHz mono int16 capture")
        controller_class = RealtimeController if self.config.v02.enabled else TurnController
        extra = {"options": self.config.v02} if self.config.v02.enabled else {}
        self.controller = controller_class(
            **extra,
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
            barge_in_gate=BargeInGate(min_speech_ms=max(250, self.config.vad.min_speech_ms), enabled=True),
        )
        await self.controller.initialize()

    def snapshot(self) -> VoiceEvent:
        value = snapshot_event(self.state)
        value.payload["recent_turns"] = [record.to_payload() for record in self.history.records]
        return value

    async def start_listening(self) -> None:
        async with self._session_lock:
            if self.controller is None:
                await self.bus.publish(event("error", code="RUNTIME_NOT_INITIALIZED", message="Voice Runtime 尚未初始化。", recoverable=False))
                return
            await self.controller.start_listening()

    async def stop_listening(self) -> None:
        async with self._session_lock:
            if self.controller is not None:
                await self.controller.stop_listening()

    async def close(self) -> None:
        if self.controller is not None:
            await self.controller.close()
        if self.tts is not None:
            await self.tts.close()
        if self.audio_processor is not None:
            self.audio_processor.close()

    def health_payload(self) -> dict[str, Any]:
        return {
            "state": self.state.state.value,
            "listening": self.state.listening,
            "can_start": self.state.can_start,
            "services": dict(self.state.services),
            "startup_errors": list(self.startup_errors),
            "wake_enabled": self.config.wake.enabled,
            "wake_keyword": self.config.wake.keyword,
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


def create_app(runtime: VoiceRuntime, *, lifespan: Any = None) -> Any:
    if FastAPI is None:
        raise RuntimeError("缺少 fastapi，请先运行 scripts\\setup.ps1。")

    app = FastAPI(title="Voice Terminal Server", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return runtime.health_payload()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        logger.info("websocket.connected peer=%s", websocket.client)
        sequencer = ProtocolSequencer()
        subscription = runtime.bus.subscribe()
        event_task = asyncio.create_task(_forward_events(websocket, subscription, sequencer), name="websocket-events")
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
                else:
                    await _send_error(websocket, sequencer, "UNKNOWN_COMMAND", f"不支持的命令：{command}", None, True)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.info("WebSocket closed: %s", exc)
        finally:
            logger.info("websocket.disconnected peer=%s", websocket.client)
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)

    return app


async def _forward_events(websocket: Any, subscription: Any, sequencer: ProtocolSequencer) -> None:
    try:
        async for value in subscription:
            logger.debug("websocket.event type=%s turn_id=%s", value.type, value.turn_id)
            await websocket.send_json(sequencer.envelope(value))
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            await websocket.close(code=1013)
        except Exception:
            pass
        return


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

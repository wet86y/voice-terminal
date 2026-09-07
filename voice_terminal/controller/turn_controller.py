from __future__ import annotations

import asyncio
from typing import Any

from ..asr.client import AsrWorkerClient
from ..audio.capture import AudioCapture
from ..audio.player import PcmPlayer, PlaybackSnapshot
from ..audio.processing import AudioPreprocessor
from ..core.event_bus import EventBus
from ..core.events import VoiceEvent, event
from ..core.state import RuntimeState, TerminalState
from ..core.turn import InputManager
from ..kws.detector import KwsDetector
from ..providers.deepseek import DeepSeekClient
from ..providers.minimax_tts import MiniMaxTtsClient
from ..vad.barge_in import BargeInGate
from ..vad.detector import FsmnVadDetector


class InputController:
    """V0.4 shared device/input lifecycle, without an answer pipeline."""

    def __init__(self, *, state: RuntimeState, bus: EventBus, history: Any,
                 capture: AudioCapture | None, kws: KwsDetector | None,
                 vad: FsmnVadDetector | None, asr: AsrWorkerClient | None,
                 llm: DeepSeekClient | None, tts: MiniMaxTtsClient | None,
                 player: PcmPlayer | None, wake_enabled: bool, wake_keyword: str,
                 wake_preroll_ms: int, vad_min_speech_ms: int,
                 vad_end_silence_ms: int, max_turn_ms: int, bridge_min_chars: int,
                 bridge_max_chars: int, bridge_max_wait_ms: int, bridge_breaks: str,
                 audio_processor: AudioPreprocessor | None = None,
                 barge_in_gate: BargeInGate | None = None) -> None:
        self.state, self.bus, self.history = state, bus, history
        self.capture, self.kws, self.vad, self.asr = capture, kws, vad, asr
        self.llm, self.tts, self.player = llm, tts, player
        self.audio_processor = audio_processor
        self.barge_in_gate = barge_in_gate or BargeInGate(min_speech_ms=max(250, vad_min_speech_ms), enabled=True)
        self.wake_enabled, self.wake_keyword, self.wake_preroll_ms = wake_enabled, wake_keyword, wake_preroll_ms
        self.vad_min_speech_ms, self.vad_end_silence_ms, self.max_turn_ms = vad_min_speech_ms, vad_end_silence_ms, max_turn_ms
        self.bridge_options = {"min_chars": bridge_min_chars, "max_chars": bridge_max_chars,
                               "max_wait_ms": bridge_max_wait_ms, "preferred_breaks": bridge_breaks}
        self.inputs = InputManager()
        self.turns = self.inputs  # internal implementation alias; wire identity is input_id.
        self._listen_task: asyncio.Task | None = None
        self._completion_task: asyncio.Task | None = None
        self._turn_cancel: asyncio.Event | None = None
        self._active_input = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        if self.asr is not None:
            self.asr.on_result = self._on_asr_result
        if self.player is not None:
            self.player.on_playback_started = self._on_playback_started

    async def _publish(self, value: VoiceEvent) -> None:
        await self.bus.publish(value)

    async def _state_event(self, state: TerminalState, *, listening: bool | None = None,
                           turn_id: str | None = None) -> None:
        self.state.set_state(state, listening=listening)
        await self._publish(event("session.state", turn_id, state=state.value,
                                  listening=self.state.listening, can_start=self.state.can_start))
        snapshot = self.state.snapshot()
        snapshot["recent_inputs"] = [record.to_payload() for record in self.history.records]
        await self._publish(VoiceEvent(type="runtime.snapshot", turn_id=None, payload=snapshot))

    async def initialize(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.capture is not None and self.state.services.get("audio") != "ERROR":
            self.state.set_service("audio", "READY")
        if self.state.services.get("kws") != "ERROR":
            self.state.set_service("kws", "READY" if self.wake_enabled and self.kws is not None else "DISABLED")
        if self.llm is not None and self.state.services.get("llm") != "ERROR":
            self.state.set_service("llm", "READY")
        if self.tts is not None and self.state.services.get("tts") != "ERROR":
            self.state.set_service("tts", "READY")
        self.state.set_state(TerminalState.IDLE, listening=False)
        snapshot = self.state.snapshot()
        snapshot["recent_inputs"] = [record.to_payload() for record in self.history.records]
        await self._publish(VoiceEvent(type="runtime.snapshot", turn_id=None, payload=snapshot))

    async def start_listening(self) -> None:
        if self.state.listening:
            return
        required = ("audio", "kws", "asr", "llm", "tts")
        missing = [name for name in required if self.state.services.get(name) not in {"READY", "RUNNING", "STREAMING", "CONNECTED"}
                   and not (name == "kws" and self.state.services.get(name) == "DISABLED")]
        if missing:
            await self._error("RUNTIME_NOT_READY", f"后端服务尚未就绪：{', '.join(missing)}", recoverable=True)
            return
        if self.capture is None or self.vad is None or self.asr is None:
            await self._error("RUNTIME_NOT_READY", "真实音频链路尚未初始化。", recoverable=False)
            return
        try:
            if not self.asr.connected:
                await self.asr.connect()
            await self.capture.start()
            if self.audio_processor is not None:
                self.audio_processor.reset()
            self.vad.reset()
            self.state.set_service("audio", "RUNNING")
            self.state.set_service("kws", "RUNNING" if self.wake_enabled else "DISABLED")
            self.state.set_service("asr", "RUNNING")
            self._listen_task = asyncio.create_task(self._listen_loop(), name="voice-input-loop")
            await self._state_event(TerminalState.IDLE, listening=True)
        except Exception as exc:
            self.state.set_service("audio", "ERROR")
            self.state.set_service("asr", "ERROR")
            await self._error("SESSION_START_FAILED", str(exc), recoverable=True)

    async def stop_listening(self) -> None:
        self.state.listening = False
        if self._listen_task is not None:
            self._listen_task.cancel()
            await asyncio.gather(self._listen_task, return_exceptions=True)
            self._listen_task = None
        await self._cancel_turn(reason="stopped")
        if self.capture is not None:
            await self.capture.stop()
        if self.audio_processor is not None:
            self.audio_processor.reset()
        if self.asr is not None:
            await self.asr.close()
        self.state.set_service("audio", "READY" if self.capture is not None else "ERROR")
        self.state.set_service("kws", "READY" if self.wake_enabled and self.kws is not None else "DISABLED")
        self.state.set_service("asr", "READY" if self.asr is not None and self.vad is not None else "ERROR")
        await self._state_event(TerminalState.IDLE, listening=False)

    async def close(self) -> None:
        self._closed = True
        await self.stop_listening()
        if self.player is not None:
            self.player.stop()

    def _process_audio(self, chunk: bytes) -> bytes:
        if self.audio_processor is None:
            return chunk
        processed = self.audio_processor.process(chunk)
        if self.audio_processor.enabled:
            self.state.current["audio_processing_status"] = self.audio_processor.status
            self.state.current["audio_processing_error"] = self.audio_processor.error or ""
        return processed

    def _stop_playback_snapshot(self, key: str | None = None) -> PlaybackSnapshot | None:
        if self.player is None:
            return None
        stop = getattr(self.player, "stop_and_snapshot", None)
        if callable(stop):
            return stop(key)
        block = getattr(self.player, "block", None)
        if callable(block):
            block()
        return None

    @staticmethod
    def _add_playback_delivery_fields(delivery: dict[str, Any], snapshot: PlaybackSnapshot | None) -> dict[str, Any]:
        if snapshot is None:
            return delivery
        delivery.setdefault("played_ms", snapshot.output_estimated_ms)
        delivery.setdefault("callback_played_ms", snapshot.played_ms)
        delivery.setdefault("output_tail_estimated_ms", max(0, snapshot.played_ms - snapshot.output_estimated_ms))
        delivery.setdefault("playback_precision", snapshot.precision)
        return delivery

    def _barge_in_protected_available(self) -> bool:
        if self.player is None or not bool(getattr(self.player, "running", False)):
            return True
        workspace = getattr(self, "workspace", None)
        key = (getattr(workspace, "playback_key", None)
               or getattr(getattr(self, "inputs", None), "current", None)
               or getattr(getattr(self, "turns", None), "current", None))
        started = getattr(self.player, "has_started", None)
        if key is not None and callable(started):
            try:
                if not started(key):
                    return True
            except Exception:
                pass
        return bool(self.audio_processor and self.audio_processor.enabled and self.audio_processor.protected_interruption_available)

    def _near_voice_activity(self) -> bool | None:
        if self.audio_processor is None or not self.audio_processor.enabled:
            return None
        return self.audio_processor.last_voice_activity if self.audio_processor.last_voice_activity is not None else False

    async def _record_interruption(self, *args: Any, **kwargs: Any) -> None:
        # EpisodeSpeechOutput is the only V0.4 delivery settlement owner.
        return

    async def _error(self, code: str, message: str, *, recoverable: bool,
                     turn_id: str | None = None) -> None:
        if not recoverable:
            self.state.set_state(TerminalState.ERROR, listening=False)
        await self._publish(event("error", turn_id, code=code, message=message, recoverable=recoverable))

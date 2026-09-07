from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..asr.client import AsrWorkerClient
from ..asr.protocol import AudioChunk
from ..audio.capture import AudioCapture
from ..audio.player import PcmPlayer, PlaybackSnapshot
from ..audio.processing import AudioPreprocessor
from ..core.event_bus import EventBus
from ..core.events import VoiceEvent, event, utc_now_iso
from ..core.state import RuntimeState, TerminalState
from ..core.turn import TurnManager
from ..kws.detector import KwsDetector
from ..kws.keywords import strip_wake_prefix
from ..providers.deepseek import DeepSeekClient
from ..providers.minimax_tts import MiniMaxTtsClient, MiniMaxTtsSession
from ..server.history import InMemoryHistory
from ..stream.text_bridge import TextBridge
from ..vad.detector import FsmnVadDetector
from ..vad.barge_in import BargeInGate
from ..tts.alignment import DeliveryResolver
from ..tts.text_merger import BidiTextMerger

logger = logging.getLogger(__name__)


@dataclass
class TurnMetrics:
    started_at: float
    listening_started_at: float | None = None
    wake_detected_at: float | None = None
    speech_started_at: float | None = None
    speech_ended_at: float | None = None
    asr_first_text_at: float | None = None
    llm_first_token_at: float | None = None
    first_tts_text_at: float | None = None
    first_tts_audio_at: float | None = None
    playback_started_at: float | None = None
    completed_at: float | None = None
    speech_end_silence_ms: int = 700

    def values(self) -> dict[str, int | None]:
        base = self.speech_started_at or self.started_at
        wake_base = self.listening_started_at or self.started_at
        speech_end = self.speech_ended_at
        playback = self.playback_started_at
        return {
            "wake_detection_ms": self._delta(self.wake_detected_at, wake_base),
            "asr_first_text_ms": self._delta(self.asr_first_text_at, base),
            "speech_end_confirmation_ms": self._delta(
                speech_end,
                speech_end - self.speech_end_silence_ms / 1000 if speech_end else None,
            ),
            "llm_first_token_ms": self._delta(self.llm_first_token_at, speech_end),
            "llm_request_to_first_tts_text_ms": self._delta(self.first_tts_text_at, speech_end),
            "tts_text_to_first_audio_ms": self._delta(self.first_tts_audio_at, self.first_tts_text_at),
            "first_audio_to_playback_ms": self._delta(playback, self.first_tts_audio_at),
            "speech_end_to_playback_ms": self._delta(playback, speech_end),
            "turn_total_ms": self._delta(self.completed_at, base),
        }

    @staticmethod
    def _delta(end: float | None, start: float | None) -> int | None:
        if end is None or start is None:
            return None
        return max(0, int((end - start) * 1000))


class TurnController:
    def __init__(
        self,
        *,
        state: RuntimeState,
        bus: EventBus,
        history: InMemoryHistory,
        capture: AudioCapture | None,
        kws: KwsDetector | None,
        vad: FsmnVadDetector | None,
        asr: AsrWorkerClient | None,
        llm: DeepSeekClient | None,
        tts: MiniMaxTtsClient | None,
        player: PcmPlayer | None,
        wake_enabled: bool,
        wake_keyword: str,
        wake_preroll_ms: int,
        vad_min_speech_ms: int,
        vad_end_silence_ms: int,
        max_turn_ms: int,
        bridge_min_chars: int,
        bridge_max_chars: int,
        bridge_max_wait_ms: int,
        bridge_breaks: str,
        audio_processor: AudioPreprocessor | None = None,
        barge_in_gate: BargeInGate | None = None,
    ) -> None:
        self.state = state
        self.bus = bus
        self.history = history
        self.capture = capture
        self.kws = kws
        self.vad = vad
        self.asr = asr
        self.llm = llm
        self.tts = tts
        self.player = player
        self.audio_processor = audio_processor
        # Keep interruption confirmation independent from the shorter VAD
        # onset threshold.  The product default is a 250 ms near-end voice
        # window even if a caller tunes ordinary turn detection lower.
        self.barge_in_gate = barge_in_gate or BargeInGate(min_speech_ms=max(250, vad_min_speech_ms), enabled=True)
        self.wake_enabled = wake_enabled
        self.wake_keyword = wake_keyword
        self.wake_preroll_ms = wake_preroll_ms
        self.vad_min_speech_ms = vad_min_speech_ms
        self.vad_end_silence_ms = vad_end_silence_ms
        self.max_turn_ms = max_turn_ms
        self.bridge_options = {
            "min_chars": bridge_min_chars,
            "max_chars": bridge_max_chars,
            "max_wait_ms": bridge_max_wait_ms,
            "preferred_breaks": bridge_breaks,
        }
        self.turns = TurnManager()
        self._listen_task: asyncio.Task[None] | None = None
        self._completion_task: asyncio.Task[None] | None = None
        self._turn_cancel: asyncio.Event | None = None
        self._active_input = False
        self._speech_started_at: float | None = None
        self._last_vad_speech_at: float | None = None
        self._barge_in_started_at: float | None = None
        self._asr_final_events: dict[str, asyncio.Event] = {}
        self._asr_final_text: dict[str, str] = {}
        self._asr_latest_text: dict[str, str] = {}
        self._turn_has_valid_speech: dict[str, bool] = {}
        self._metrics: dict[str, TurnMetrics] = {}
        self._tts_session: MiniMaxTtsSession | None = None
        self._tts_cancel_task: asyncio.Task[object] | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listening_started_at: float | None = None
        self._audio_blocks = 0
        self._audio_bytes = 0
        self._last_audio_log_at = 0.0
        # Keep a short processed-microphone pre-roll so a confirmed
        # interruption does not lose the beginning of words such as
        # “等一下”.  The bound includes the requested 200 ms before the
        # candidate plus the complete confirmation window, so the next ASR
        # turn receives every sample from onset through confirmation.
        self._barge_in_preroll_bytes = 16000 * 2 * (200 + self.barge_in_gate.min_speech_ms) // 1000
        self._recent_audio: deque[bytes] = deque()
        self._recent_audio_bytes = 0
        self._delivery_resolvers: dict[str, DeliveryResolver] = {}
        self._interrupted_turns: set[str] = set()

        if self.asr is not None:
            self.asr.on_result = self._on_asr_result
        if self.player is not None:
            self.player.on_playback_started = self._on_playback_started

    async def _publish(self, value: VoiceEvent) -> None:
        await self.bus.publish(value)

    async def _publish_latency(self, turn_id: str, *, complete: bool = False) -> None:
        metrics = self._metrics.get(turn_id)
        if metrics is None or not self.turns.is_current(turn_id):
            return
        values = metrics.values()
        self.state.latency = {"complete": complete, "values": values}
        await self._publish(event("latency.snapshot", turn_id, complete=complete, values=values))

    async def _state_event(self, state: TerminalState, *, listening: bool | None = None, turn_id: str | None = None) -> None:
        self.state.set_state(state, listening=listening)
        logger.info(
            "controller.state state=%s listening=%s turn_id=%s services=%s",
            state.value,
            self.state.listening,
            turn_id,
            self.state.services,
        )
        await self._publish(
            event(
                "session.state",
                turn_id,
                state=state.value,
                listening=self.state.listening,
                can_start=self.state.can_start,
            )
        )
        snapshot = self.state.snapshot()
        snapshot["recent_turns"] = [record.to_payload() for record in self.history.records]
        await self._publish(VoiceEvent(type="runtime.snapshot", turn_id=None, payload=snapshot))

    async def initialize(self) -> None:
        self._loop = asyncio.get_running_loop()
        # SessionRuntime records initialization failures before constructing the
        # controller (notably a required AEC processor or a failed output
        # stream).  Do not turn those errors back into READY merely because a
        # dependency object exists; start_listening must remain blocked until
        # the failing component is recreated successfully.
        if self.capture is not None and self.state.services.get("audio") != "ERROR":
            self.state.set_service("audio", "READY")
        if self.state.services.get("kws") != "ERROR":
            self.state.set_service("kws", "READY" if self.wake_enabled and self.kws is not None else "DISABLED")
        if self.llm is not None and self.state.services.get("llm") != "ERROR":
            self.state.set_service("llm", "READY")
        if self.tts is not None and self.state.services.get("tts") != "ERROR":
            self.state.set_service("tts", "READY")
        self.state.set_state(TerminalState.IDLE, listening=False)
        logger.info(
            "controller.initialized wake_enabled=%s audio=%s kws=%s vad=%s asr=%s llm=%s tts=%s",
            self.wake_enabled,
            self.capture is not None,
            self.kws is not None,
            self.vad is not None,
            self.asr is not None,
            self.llm is not None,
            self.tts is not None,
        )
        snapshot = self.state.snapshot()
        snapshot["recent_turns"] = [record.to_payload() for record in self.history.records]
        await self._publish(VoiceEvent(type="runtime.snapshot", turn_id=None, payload=snapshot))

    async def start_listening(self) -> None:
        if self.state.listening:
            logger.info("controller.start_ignored reason=already_listening")
            return
        required_services = ("audio", "kws", "asr", "llm", "tts")
        missing = [
            name
            for name in required_services
            if self.state.services.get(name) not in {"READY", "RUNNING", "STREAMING", "CONNECTED"}
            and not (name == "kws" and self.state.services.get(name) == "DISABLED")
        ]
        if missing:
            logger.error("controller.start_blocked missing_services=%s", missing)
            await self._error("RUNTIME_NOT_READY", f"后端服务尚未就绪：{', '.join(missing)}", recoverable=True)
            return
        if self.capture is None or self.vad is None or self.asr is None:
            logger.error(
                "controller.start_blocked audio_chain capture=%s vad=%s asr=%s",
                self.capture is not None,
                self.vad is not None,
                self.asr is not None,
            )
            await self._error("RUNTIME_NOT_READY", "真实音频链路尚未初始化。", recoverable=False)
            return
        try:
            logger.info("controller.start mode=%s", "wake" if self.wake_enabled else "manual")
            if not self.asr.connected:
                logger.info("controller.start connecting_asr")
                await self.asr.connect()
            await self.capture.start()
            if self.audio_processor is not None:
                self.audio_processor.reset()
            self._recent_audio.clear()
            self._recent_audio_bytes = 0
            self._listening_started_at = time.monotonic()
            self.state.set_service("audio", "RUNNING")
            self.state.set_service("kws", "RUNNING" if self.wake_enabled else "DISABLED")
            self.state.set_service("asr", "RUNNING")
            self.vad.reset()
            if self.wake_enabled:
                self.state.set_state(TerminalState.IDLE, listening=True)
                self._listen_task = asyncio.create_task(self._listen_loop(), name="voice-audio-loop")
                await self._state_event(TerminalState.IDLE, listening=True)
            else:
                # Manual mode means that no wake word is required; it does
                # not mean that the first microphone frame is a turn.  Wait
                # for VAD speech before creating a turn so speaker echo and
                # background noise cannot recursively start new LLM calls.
                self._listen_task = asyncio.create_task(self._listen_loop(), name="voice-audio-loop")
                await self._state_event(TerminalState.IDLE, listening=True)
        except Exception as exc:
            logger.exception("controller.start_failed")
            self.state.set_service("audio", "ERROR")
            self.state.set_service("asr", "ERROR")
            await self._error("SESSION_START_FAILED", str(exc), recoverable=True)

    async def stop_listening(self) -> None:
        logger.info("controller.stop requested turn_id=%s", self.turns.current)
        self.state.listening = False
        if self._listen_task is not None:
            self._listen_task.cancel()
            await asyncio.gather(self._listen_task, return_exceptions=True)
            self._listen_task = None
        await self._cancel_turn()
        if self.capture is not None:
            await self.capture.stop()
        if self.audio_processor is not None:
            self.audio_processor.reset()
        self._recent_audio.clear()
        self._recent_audio_bytes = 0
        if self.asr is not None:
            await self.asr.close()
        self.state.set_service("audio", "READY" if self.capture is not None else "ERROR")
        self.state.set_service("kws", "READY" if self.wake_enabled and self.kws is not None else "DISABLED")
        self.state.set_service("asr", "READY" if self.asr is not None and self.vad is not None else "ERROR")
        self._listening_started_at = None
        await self._state_event(TerminalState.IDLE, listening=False)
        logger.info("controller.stop completed")

    async def close(self) -> None:
        self._closed = True
        await self.stop_listening()
        if self._tts_cancel_task is not None:
            await asyncio.gather(self._tts_cancel_task, return_exceptions=True)
            self._tts_cancel_task = None
        if self.player is not None:
            self.player.stop()

    async def _listen_loop(self) -> None:
        assert self.capture is not None
        logger.info("controller.audio_loop started")
        while self.state.listening and not self._closed:
            try:
                chunk = await self.capture.read()
                await self._handle_audio(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("controller.audio_loop_failed")
                # A failed ASR/VAD/audio operation must not leave the old
                # turn active while the loop keeps feeding the same broken
                # dependency.  Release the turn and stop this session so a
                # later session.start can establish a fresh transport.
                await self._cancel_turn(reason="failed")
                self.state.listening = False
                try:
                    await self.capture.stop()
                except Exception:
                    logger.exception("controller.audio_loop_stop_capture_failed")
                if self.asr is not None:
                    try:
                        await self.asr.close()
                    except Exception:
                        logger.exception("controller.audio_loop_close_asr_failed")
                self.state.set_service("audio", "READY" if self.capture is not None else "ERROR")
                self.state.set_service("kws", "READY" if self.wake_enabled and self.kws is not None else "DISABLED")
                self.state.set_service("asr", "READY" if self.asr is not None and self.vad is not None else "ERROR")
                await self._error("AUDIO_PIPELINE_FAILED", str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=False)
                break
        logger.info("controller.audio_loop stopped")

    async def _handle_audio(self, chunk: bytes) -> None:
        if self.audio_processor is not None and self.audio_processor.enabled:
            chunk = await asyncio.to_thread(self._process_audio, chunk)
        self._remember_audio(chunk)
        self._audio_blocks += 1
        self._audio_bytes += len(chunk)
        now = time.monotonic()
        if not self._last_audio_log_at or now - self._last_audio_log_at >= 1.0:
            logger.info(
                "controller.audio_progress blocks=%d bytes=%d chunk_bytes=%d state=%s turn_id=%s active_input=%s capture_error=%s",
                self._audio_blocks,
                self._audio_bytes,
                len(chunk),
                self.state.state.value,
                self.turns.current,
                self._active_input,
                self.capture.last_error if self.capture is not None else None,
            )
            self._last_audio_log_at = now
        turn_id = self.turns.current
        if turn_id is None:
            if not self.wake_enabled:
                if self.vad is None:
                    logger.error("controller.audio_no_turn vad_unavailable")
                    return
                activity = await asyncio.to_thread(self.vad.feed, chunk)
                if not (activity.speech_started or activity.is_speech):
                    return
                logger.info(
                    "controller.manual_speech_detected seed_bytes=%d speech_started=%s",
                    len(chunk),
                    activity.speech_started,
                )
                await self._begin_turn(
                    seed_chunk=chunk,
                    reset_vad=False,
                    initial_speech=True,
                )
                return
            if self.kws is None:
                logger.error("controller.audio_no_turn kws_unavailable")
                return
            detected = await asyncio.to_thread(self.kws.accept_pcm, chunk)
            if detected:
                logger.info("controller.kws_detected result=%s", detected)
                await self._begin_turn()
            return

        if self._active_input:
            if self.vad is None or self.asr is None:
                return
            activity = await asyncio.to_thread(self.vad.feed, chunk)
            now = time.monotonic()
            if activity.speech_started and self._speech_started_at is None:
                self._speech_started_at = now
                self._metrics[turn_id].speech_started_at = now
                logger.info("controller.vad_speech_started turn_id=%s", turn_id)
            if activity.is_speech:
                self._last_vad_speech_at = now
            if activity.speech_ended:
                logger.info("controller.vad_speech_ended turn_id=%s", turn_id)
            silence_elapsed = (
                (now - self._last_vad_speech_at) * 1000
                if self._last_vad_speech_at is not None
                else 0
            )
            should_finish = activity.speech_ended or (
                self._speech_started_at is not None and silence_elapsed >= self.vad_end_silence_ms
            ) or (now - self._metrics[turn_id].started_at) * 1000 >= self.max_turn_ms
            if should_finish:
                reason = "vad_end" if activity.speech_ended else (
                    "silence_timeout"
                    if self._speech_started_at is not None and silence_elapsed >= self.vad_end_silence_ms
                    else "max_turn"
                )
                logger.info(
                    "controller.vad_finish turn_id=%s reason=%s silence_elapsed_ms=%.0f speech_started=%s",
                    turn_id,
                    reason,
                    silence_elapsed,
                    self._speech_started_at is not None,
                )
            await self.asr.send_audio(AudioChunk(turn_id=turn_id, pcm=chunk, is_final=should_finish))
            if should_finish:
                speech_duration_ms = (
                    max(0.0, (self._last_vad_speech_at - self._speech_started_at) * 1000)
                    if self._speech_started_at is not None and self._last_vad_speech_at is not None
                    else 0.0
                )
                has_valid_speech = (
                    self._speech_started_at is not None
                    and speech_duration_ms >= self.vad_min_speech_ms
                )
                self._turn_has_valid_speech[turn_id] = has_valid_speech
                self._active_input = False
                self._metrics[turn_id].speech_ended_at = now
                await self._state_event(TerminalState.FINALIZING, turn_id=turn_id)
                await self.asr.send_finish(turn_id)
                self.vad.reset()
                self._completion_task = asyncio.create_task(
                    self._complete_turn(turn_id),
                    name=f"complete-{turn_id}",
                )
                logger.info(
                    "controller.asr_final_wait_started turn_id=%s timeout_s=5 speech_duration_ms=%.0f valid_speech=%s",
                    turn_id,
                    speech_duration_ms,
                    has_valid_speech,
                )
            return

        if self.state.state == TerminalState.SPEAKING and self.vad is not None:
            activity = await asyncio.to_thread(self.vad.feed, chunk)
            gate_now = time.monotonic()
            decision = self.barge_in_gate.feed(
                activity,
                now=gate_now,
                near_voice=self._near_voice_activity(),
                protected_available=self._barge_in_protected_available(),
            )
            if decision.candidate:
                logger.info("controller.barge_in_candidate turn_id=%s duration_ms=%d reason=%s", turn_id, decision.duration_ms, decision.reason)
            if decision.confirmed:
                logger.info("controller.interruption_detected turn_id=%s", turn_id)
                await self._interrupt_and_restart(
                    chunk,
                    speech_onset_at=self.barge_in_gate.candidate_started_at,
                    interrupt_confirmed_at=gate_now,
                )

    async def _begin_turn(
        self,
        seed_chunk: bytes | None = None,
        *,
        reset_vad: bool = True,
        initial_speech: bool = False,
    ) -> None:
        if self._completion_task is not None and not self._completion_task.done():
            await self._cancel_turn()
        if reset_vad and self.vad is not None:
            self.vad.reset()
        turn_id = self.turns.begin()
        self.state.clear_current()
        detected_at = time.monotonic()
        self._metrics[turn_id] = TurnMetrics(
            started_at=detected_at,
            listening_started_at=self._listening_started_at,
            wake_detected_at=detected_at if self.wake_enabled else None,
            speech_end_silence_ms=self.vad_end_silence_ms,
        )
        self._asr_final_events[turn_id] = asyncio.Event()
        self._asr_final_text[turn_id] = ""
        self._asr_latest_text[turn_id] = ""
        self._turn_has_valid_speech[turn_id] = False
        self._turn_cancel = asyncio.Event()
        self._active_input = True
        self._speech_started_at = detected_at if initial_speech else None
        self._last_vad_speech_at = detected_at if initial_speech else None
        self._barge_in_started_at = None
        self.barge_in_gate.reset()
        self._delivery_resolvers[turn_id] = DeliveryResolver()
        self.state.current["turn_id"] = turn_id
        # ``stop_and_snapshot`` closes the player gate for an interrupted or
        # manually stopped generation. Re-open it for every new input so
        # a later response is not silently discarded after an interruption.
        if self.player is not None:
            self.player.allow(turn_id)
        if initial_speech:
            self._metrics[turn_id].speech_started_at = detected_at
        logger.info(
            "controller.turn_started turn_id=%s mode=%s seed_bytes=%d",
            turn_id,
            "wake" if self.wake_enabled else "manual",
            len(seed_chunk or b""),
        )
        if initial_speech:
            logger.info("controller.vad_speech_started turn_id=%s source=pre_turn_gate", turn_id)
        await self._state_event(TerminalState.LISTENING, listening=True, turn_id=turn_id)
        preroll = (
            self.capture.preroll(self.wake_preroll_ms)
            if self.wake_enabled and self.capture is not None
            else b""
        )
        if self.asr is not None:
            if preroll:
                await self.asr.send_audio(AudioChunk(turn_id=turn_id, pcm=preroll, is_final=False))
            if seed_chunk:
                await self.asr.send_audio(AudioChunk(turn_id=turn_id, pcm=seed_chunk, is_final=False))

    async def _interrupt_and_restart(
        self,
        chunk: bytes,
        *,
        speech_onset_at: float | None = None,
        interrupt_confirmed_at: float | None = None,
    ) -> None:
        old_turn = self.turns.current
        seed = b"".join(self._recent_audio) or chunk
        logger.info("controller.turn_interrupt old_turn=%s seed_bytes=%d preroll_bytes=%d", old_turn, len(seed), self._recent_audio_bytes)
        # Invalidate the old generation before the first await.  A streaming
        # LLM callback can otherwise pass its current-turn check while the
        # interruption event is being published and enqueue more stale text.
        if old_turn is not None:
            self.turns.invalidate()
        snapshot = self._stop_playback_snapshot(self._playback_key(old_turn)) if old_turn is not None else None
        await self._state_event(TerminalState.INTERRUPTING, listening=True, turn_id=old_turn)
        if old_turn is not None:
            await self._record_interruption(
                old_turn,
                snapshot,
                speech_onset_at=speech_onset_at,
                interrupt_confirmed_at=interrupt_confirmed_at,
            )
        await self._cancel_turn(reason="interrupted", snapshot=snapshot)
        await self._begin_turn(seed_chunk=seed, reset_vad=False, initial_speech=True)

    def _remember_audio(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._recent_audio.append(bytes(chunk))
        self._recent_audio_bytes += len(chunk)
        while self._recent_audio and self._recent_audio_bytes > self._barge_in_preroll_bytes:
            removed = self._recent_audio.popleft()
            self._recent_audio_bytes -= len(removed)

    def _playback_key(self, turn_id: str | None) -> str | None:
        return turn_id

    def _stop_playback_snapshot(self, key: str | None = None) -> PlaybackSnapshot | None:
        """Use the atomic player API while tolerating legacy test doubles."""

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
        """Attach callback/device progress without changing delivery status."""

        if snapshot is None:
            return delivery
        delivery.setdefault("played_ms", snapshot.output_estimated_ms)
        delivery.setdefault("callback_played_ms", snapshot.played_ms)
        delivery.setdefault(
            "output_tail_estimated_ms",
            # ``played_ms`` counts bytes copied by the callback, while
            # ``output_estimated_ms`` conservatively subtracts the device
            # tail.  The physical tail is therefore the callback position
            # minus the estimated device position.
            max(0, snapshot.played_ms - snapshot.output_estimated_ms),
        )
        delivery.setdefault("playback_precision", snapshot.precision)
        return delivery

    def _process_audio(self, chunk: bytes) -> bytes:
        if self.audio_processor is None:
            return chunk
        processed = self.audio_processor.process(chunk)
        if self.audio_processor.enabled:
            self.state.current["audio_processing_status"] = self.audio_processor.status
            self.state.current["audio_processing_error"] = self.audio_processor.error or ""
        return processed

    def _barge_in_protected_available(self) -> bool:
        # A raw FSMN-VAD result is not enough to claim that a sound is the
        # near-end user's voice while the assistant may be playing.  The
        # protected automatic path therefore requires the configured audio
        # processing chain while a real output stream is active.  A player
        # that has no running output stream cannot be producing speaker echo
        # (this is also how deterministic in-memory players are exercised),
        # so its raw VAD path remains a safe same-turn continuation.  Once a
        # device stream is running, a disabled/degraded processor disables
        # protected automatic interruption instead of silently falling back
        # to microphone energy.
        if self.player is None or not bool(getattr(self.player, "running", False)):
            # With no active output stream there is no assistant echo to
            # cancel, so ordinary processed/raw VAD can continue to detect a
            # new turn.  A running output stream always requires a fresh
            # reverse reference from the protected processor below.
            return True

        # ``PcmPlayer.running`` describes the lifetime of the PortAudio
        # stream, not whether this turn has submitted its first non-silent
        # buffer.  The stream is opened once when the runtime starts, so the
        # first few capture frames of every response can arrive before the
        # first playback callback has produced a reverse reference.  There is
        # no assistant audio to cancel in that interval; requiring a reverse
        # reference there would make the beginning of every response
        # effectively non-interruptible.  Use the per-turn playback marker to
        # keep the protected gate open until output really starts.
        current_workspace = getattr(self, "workspace", None)
        current_key = (
            getattr(current_workspace, "playback_key", None)
            if current_workspace is not None
            else self._playback_key(self.turns.current)
        )
        has_started = getattr(self.player, "has_started", None)
        if current_key is not None and callable(has_started):
            try:
                if not bool(has_started(current_key)):
                    return True
            except Exception:
                # A legacy player/test double may expose a partial playback
                # API.  Fall through to the conservative protected check.
                pass
        if self.audio_processor is not None and self.audio_processor.enabled:
            return bool(self.audio_processor.protected_interruption_available)
        return False

    def _near_voice_activity(self) -> bool | None:
        if self.audio_processor is None or not self.audio_processor.enabled:
            return None
        # Once processing is enabled, an unavailable WebRTC voice decision is
        # treated as silence.  Falling back to raw FSMN-VAD would make an
        # external noise source look like a protected near-end voice.
        return self.audio_processor.last_voice_activity if self.audio_processor.last_voice_activity is not None else False

    async def _record_interruption(
        self,
        turn_id: str,
        snapshot: PlaybackSnapshot | None,
        *,
        speech_onset_at: float | None = None,
        interrupt_confirmed_at: float | None = None,
        termination: str = "interrupted",
        publish_interrupted: bool | None = None,
    ) -> None:
        if turn_id in self._interrupted_turns:
            return
        self._interrupted_turns.add(turn_id)
        response = self.state.current.get("assistant_text", "")
        resolver = self._delivery_resolvers.get(turn_id)
        # The receive task may have appended a subtitle snapshot just before
        # the capture loop confirmed the interruption.  Re-ingest the
        # provider's immutable event ledger before resolving the snapshot;
        # DeliveryResolver deduplicates cumulative Bidi word snapshots.
        active_session = self._tts_session
        if resolver is not None and active_session is not None:
            for alignment in getattr(active_session, "alignment_events", ()):
                resolver.add_provider_alignment(alignment)
        overlap_start_ms = (
            self.player.output_position_at(speech_onset_at)
            if self.player is not None and snapshot is not None and speech_onset_at is not None
            else None
        )
        delivery = resolver.resolve(
            snapshot or {"started": False},
            response,
            overlap_start_ms=overlap_start_ms,
            overlap_stop_ms=snapshot.output_estimated_ms if snapshot is not None else None,
        ) if resolver else {
            "status": "unknown", "heard_text": "", "uncertain_text": response,
            "unheard_text": "", "precision": "unknown", "played_ms": 0,
        }
        delivery["termination"] = termination
        self._add_playback_delivery_fields(delivery, snapshot)
        self.state.current["delivery"] = dict(delivery)
        metrics = self._metrics.get(turn_id)
        if metrics is not None:
            base = metrics.started_at
            if speech_onset_at is not None:
                delivery["speech_onset_ms"] = max(0, round((speech_onset_at - base) * 1000))
            if interrupt_confirmed_at is not None:
                delivery["interrupt_confirmed_ms"] = max(0, round((interrupt_confirmed_at - base) * 1000))
            if speech_onset_at is not None and interrupt_confirmed_at is not None:
                delivery["speech_onset_to_confirm_ms"] = max(
                    0,
                    round((interrupt_confirmed_at - speech_onset_at) * 1000),
                )
            if snapshot is not None:
                delivery["playback_stop_ms"] = max(0, round((snapshot.captured_at - base) * 1000))
                if interrupt_confirmed_at is not None:
                    delivery["confirm_to_stop_ms"] = max(0, round((snapshot.captured_at - interrupt_confirmed_at) * 1000))
        current_workspace = getattr(self, "workspace", None)
        if current_workspace is not None and getattr(current_workspace, "turn_id", None) == turn_id:
            current_workspace.delivery = dict(delivery)
            current_workspace.interruption_recorded = True
        transcript = self.state.current.get("final_text", "")
        if transcript:
            self.history.append_exchange(transcript, delivery.get("heard_text", ""), delivery=delivery)
        latency = metrics.values() if metrics else {}
        self.history.append_record(
            turn_id=turn_id,
            transcript=transcript,
            response=response,
            latency=latency,
            delivery=delivery,
        )
        if publish_interrupted is None:
            publish_interrupted = termination == "interrupted"
        # ``turn.interrupted`` is an immediate state notification.  History
        # remains written exactly once by the following terminal event.
        if publish_interrupted:
            await self._publish(event(
                "turn.interrupted", turn_id,
                transcript=transcript,
                response=response,
                delivery=delivery,
            ))
        await self._publish(event(
            "turn.completed", turn_id,
            transcript=transcript,
            response=response,
            completed_at=utc_now_iso(),
            latency=latency,
            status=termination,
            delivery=delivery,
        ))

    async def _cancel_bidi_in_background(self, session: Any, cancel: Callable[..., Awaitable[object]]) -> bool:
        """Cross the Bidi cancel barrier, closing a timed-out connection.

        The interruption path must return to ASR immediately, so controllers
        schedule this coroutine instead of awaiting the provider.  A timeout
        is still actionable: keeping a socket whose server-side generation
        was not cancelled would let stale audio race the next turn.  Closing
        it makes ``MiniMaxTtsClient.session()`` create a fresh connection for
        the next generation.
        """

        started_at = time.monotonic()
        try:
            timeout = float(
                getattr(getattr(session, "client", None), "cancel_timeout_seconds", 2.0)
            )
            result = bool(await cancel(timeout_seconds=timeout))
            logger.info(
                "controller.tts_cancel_confirmed elapsed_ms=%d confirmed=%s",
                max(0, round((time.monotonic() - started_at) * 1000)),
                result,
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "controller.tts_cancel_timeout_close elapsed_ms=%d",
                max(0, round((time.monotonic() - started_at) * 1000)),
                exc_info=True,
            )
            try:
                await session.close()
            finally:
                if self._tts_session is session:
                    self._tts_session = None
            return False

    async def _await_tts_cancel_barrier(self) -> None:
        """Wait for an old Bidi generation before sending new text.

        The interruption path deliberately schedules ``task_cancel`` so ASR
        can consume the user's pre-roll immediately.  A subsequent TTS
        sender must still cross that protocol barrier before it reuses the
        persistent socket; otherwise a late ``task_canceled`` could race a
        new ``task_continue`` and mix generations on one connection.
        """

        task = self._tts_cancel_task
        if task is None or task is asyncio.current_task():
            return
        await asyncio.gather(task, return_exceptions=True)
        if self._tts_cancel_task is task:
            self._tts_cancel_task = None

    async def _cancel_turn(
        self,
        *,
        reason: str = "stopped",
        snapshot: PlaybackSnapshot | None = None,
        record: bool = True,
    ) -> None:
        old_turn = self.turns.current
        self._active_input = False
        self._barge_in_started_at = None
        self.barge_in_gate.reset()
        if record and old_turn is not None and reason != "interrupted" and old_turn not in self._interrupted_turns:
            stop_snapshot = snapshot
            if stop_snapshot is None and self.player is not None:
                stop_snapshot = self._stop_playback_snapshot(old_turn)
            snapshot = stop_snapshot
            await self._record_interruption(
                old_turn,
                stop_snapshot,
                termination=reason,
                publish_interrupted=False,
            )
        # Invalidate before cancelling asynchronous producers.  Their late
        # callbacks then fail the generation check and cannot enter a new
        # turn.  Stop the local queue under the same key before network work.
        self.turns.invalidate()
        if self.player is not None and snapshot is None:
            self._stop_playback_snapshot(old_turn)
        if self._turn_cancel is not None:
            self._turn_cancel.set()
        active_tts = self._tts_session
        keep_tts_session = False
        if active_tts is not None:
            cancel = getattr(active_tts, "cancel", None)
            if getattr(active_tts, "bidi", False) and cancel is not None:
                idle_generation = bool(getattr(active_tts, "generation_finished", False)) and not bool(
                    getattr(active_tts, "generation_active", True)
                )
                if idle_generation:
                    # A previous flush already ended this generation.  A
                    # second task_cancel is unnecessary and can make a
                    # reusable connection look like it has a live task.
                    keep_tts_session = True
                elif self._tts_cancel_task is None or self._tts_cancel_task.done():
                    self._tts_cancel_task = asyncio.create_task(
                        self._cancel_bidi_in_background(active_tts, cancel),
                        name=f"tts-cancel-{old_turn or 'idle'}",
                    )
                    keep_tts_session = True
                else:
                    keep_tts_session = True
            elif cancel is not None:
                try:
                    keep_tts_session = bool(await cancel(timeout_seconds=2.0))
                except Exception:
                    logger.warning("controller.tts_cancel_degraded turn_id=%s", old_turn, exc_info=True)
        task, self._completion_task = self._completion_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if active_tts is not None and not (keep_tts_session and getattr(active_tts, "reusable", False)):
            await active_tts.close()
        if self._tts_session is active_tts:
            self._tts_session = active_tts if keep_tts_session and getattr(active_tts, "reusable", False) else None
        logger.info("controller.turn_cancelled turn_id=%s", old_turn)

    async def _complete_turn(self, turn_id: str) -> None:
        final_event = self._asr_final_events[turn_id]
        if not self._turn_has_valid_speech.get(turn_id, False):
            logger.info(
                "controller.no_valid_speech turn_id=%s action=skip_llm latest_text=%r",
                turn_id,
                self._asr_latest_text.get(turn_id, "")[:120],
            )
            if self.turns.is_current(turn_id):
                self.state.current["final_text"] = ""
                self.state.current["partial_text"] = ""
                await self._publish(event("transcript.final", turn_id, text=""))
                await self._finish_turn(turn_id, "")
            return
        logger.info("controller.turn_waiting_for_asr_final turn_id=%s timeout_s=5", turn_id)
        try:
            try:
                await asyncio.wait_for(final_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.error(
                    "controller.asr_final_timeout turn_id=%s latest_text_len=%d latest_text=%r",
                    turn_id,
                    len(self._asr_latest_text.get(turn_id, "")),
                    self._asr_latest_text.get(turn_id, "")[:120],
                )
                await self._error("ASR_FINAL_TIMEOUT", "ASR 未在 5 秒内返回最终结果。", recoverable=True, turn_id=turn_id)
                # The timeout is recoverable, but the finalized turn still
                # owns the controller until it is explicitly invalidated.
                # Leaving it in FINALIZING makes the next session feed a
                # dead turn and prevents normal restart.
                await self._cancel_turn(reason="failed")
                await self._state_event(TerminalState.IDLE, listening=self.state.listening)
                return
            if not self.turns.is_current(turn_id):
                return
            raw_transcript = self._asr_final_text.get(turn_id, "")
            transcript = (
                strip_wake_prefix(raw_transcript, self.wake_keyword)
                if self.wake_enabled
                else raw_transcript.strip()
            )
            logger.info(
                "controller.asr_final turn_id=%s raw_len=%d text_len=%d text=%r",
                turn_id,
                len(raw_transcript),
                len(transcript),
                transcript[:160],
            )
            self.state.current["final_text"] = transcript
            self.state.current["partial_text"] = ""
            await self._publish(event("transcript.final", turn_id, text=transcript))
            if not transcript:
                logger.warning("controller.empty_transcript turn_id=%s", turn_id)
                await self._finish_turn(turn_id, "")
                return
            await self._state_event(TerminalState.THINKING, listening=True, turn_id=turn_id)
            logger.info("controller.llm_pipeline_start turn_id=%s transcript_len=%d", turn_id, len(transcript))
            response = await self._run_llm_and_tts(turn_id, transcript)
            if self.turns.is_current(turn_id):
                await self._finish_turn(turn_id, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("controller.turn_completion_failed turn_id=%s", turn_id)
            await self._error("TURN_FAILED", str(exc), recoverable=True, turn_id=turn_id)
        finally:
            self._asr_final_events.pop(turn_id, None)
            self._asr_final_text.pop(turn_id, None)
            self._asr_latest_text.pop(turn_id, None)
            self._turn_has_valid_speech.pop(turn_id, None)

    async def _run_llm_and_tts(self, turn_id: str, transcript: str) -> str:
        if self.llm is None:
            raise RuntimeError("DeepSeek provider 未初始化")
        request_messages = self.history.messages_for_user(transcript)
        logger.info(
            "controller.llm_start turn_id=%s committed_messages=%d request_messages=%d transcript=%r",
            turn_id,
            len(self.history.messages),
            len(request_messages),
            transcript[:160],
        )
        logger.debug(
            "controller.llm_context turn_id=%s messages=%s",
            turn_id,
            json.dumps(request_messages, ensure_ascii=False),
        )
        await self._publish(
            event(
                "llm.context",
                turn_id,
                messages=request_messages,
                message_count=len(request_messages),
            )
        )
        response_parts: list[str] = []
        bridge = (
            BidiTextMerger(max_chars=128, max_wait_ms=30)
            if getattr(self.tts, "transport", "websocket") == "websocket_bidi"
            else TextBridge(**self.bridge_options)
        )
        resolver = self._delivery_resolvers.setdefault(turn_id, DeliveryResolver())
        tts_session: MiniMaxTtsSession | None = None
        tts_reader: asyncio.Task[None] | None = None
        tts_failed: Exception | None = None
        completed_markers = 0
        alignment_cursor = 0

        async def consume_audio(session: MiniMaxTtsSession) -> None:
            nonlocal completed_markers, alignment_cursor
            audio_chunks = 0
            audio_bytes = 0
            async for pcm in session.audio_chunks():
                if not self.turns.is_current(turn_id):
                    logger.info("controller.tts_audio_dropped_old_turn turn_id=%s bytes=%d", turn_id, len(pcm))
                    return
                if self.player is None:
                    continue
                audio_chunks += 1
                audio_bytes += len(pcm)
                logger.debug(
                    "controller.tts_audio turn_id=%s chunk=%d bytes=%d total_bytes=%d",
                    turn_id,
                    audio_chunks,
                    len(pcm),
                    audio_bytes,
                )
                now = time.monotonic()
                metrics = self._metrics[turn_id]
                if metrics.first_tts_audio_at is None:
                    metrics.first_tts_audio_at = now
                self.player.enqueue(turn_id, pcm)
                # A Bidi ``is_final``/``sentence_end`` event has no text
                # payload and therefore cannot be mapped to one of the
                # client-side chunks.  Only the legacy request/segment path
                # may create local sentence fallbacks here; Bidi remains
                # conservative until a real subtitle span arrives.
                current_markers = (
                    int(getattr(session, "completed_segments", 0))
                    if not getattr(session, "bidi", False)
                    else 0
                )
                while completed_markers < current_markers:
                    completed_markers += 1
                    resolver.mark_boundary(self.player.enqueued_ms)
                alignment_events = getattr(session, "alignment_events", ())
                for alignment in alignment_events[alignment_cursor:]:
                    if alignment is not None:
                        resolver.add_provider_alignment(alignment)
                alignment_cursor = len(alignment_events)
                self.state.current["audio_buffer_ms"] = self.player.buffered_ms
                self.state.current["tts_status"] = "STREAMING"
                await self._publish_latency(turn_id)
                await self._publish(
                    event(
                        "tts.status",
                        turn_id,
                        provider="minimax",
                        status="STREAMING",
                        waiting_text=self.state.current["tts_text"],
                        audio_buffer_ms=self.player.buffered_ms,
                    )
                )
                if self.state.state != TerminalState.SPEAKING:
                    await self._state_event(TerminalState.SPEAKING, listening=True, turn_id=turn_id)

        async def send_segments(segments: list[str]) -> None:
            nonlocal tts_session, tts_reader, tts_failed
            if not segments or self.tts is None or tts_failed is not None:
                return
            try:
                if tts_session is None:
                    logger.info("controller.tts_start turn_id=%s", turn_id)
                    await self._await_tts_cancel_barrier()
                    tts_session = self.tts.session()
                    await tts_session.start()
                    self._tts_session = tts_session
                    tts_reader = asyncio.create_task(consume_audio(tts_session), name=f"tts-audio-{turn_id}")
                for segment in segments:
                    if not self.turns.is_current(turn_id):
                        return
                    logger.info("controller.tts_segment turn_id=%s chars=%d text=%r", turn_id, len(segment), segment[:120])
                    self.state.current["tts_text"] = segment
                    self._metrics[turn_id].first_tts_text_at = self._metrics[turn_id].first_tts_text_at or time.monotonic()
                    resolver.add_text(segment, self.player.enqueued_ms if self.player else 0)
                    await tts_session.send_text(segment)
                    await self._publish_latency(turn_id)
                    await self._publish(
                        event(
                            "tts.status",
                            turn_id,
                            provider="minimax",
                            status="QUEUED",
                            waiting_text=segment,
                            audio_buffer_ms=self.player.buffered_ms if self.player else 0,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tts_failed = exc
                logger.warning("controller.tts_degraded turn_id=%s error=%s", turn_id, exc)

        try:
            async for delta in self.llm.stream_chat(request_messages, cancel_event=self._turn_cancel):
                if not self.turns.is_current(turn_id):
                    return ""
                now = time.monotonic()
                self._metrics[turn_id].llm_first_token_at = self._metrics[turn_id].llm_first_token_at or now
                response_parts.append(delta)
                logger.debug(
                    "controller.llm_delta turn_id=%s delta_chars=%d response_chars=%d text=%r",
                    turn_id,
                    len(delta),
                    sum(len(part) for part in response_parts),
                    delta[:120],
                )
                self.state.current["assistant_text"] = "".join(response_parts)
                await self._publish(event("assistant.delta", turn_id, text_delta=delta))
                await self._publish_latency(turn_id)
                await send_segments(bridge.flush_due(now=now))
                await send_segments(bridge.feed(delta))
            await send_segments(bridge.finish())
            response = "".join(response_parts).strip()
            if self.player is not None:
                # Once the provider has fully finished, any local fallback
                # spans can be closed at the final PCM enqueue position.
                if tts_session is not None and tts_session.server_finished:
                    resolver.complete_pending(self.player.enqueued_ms)
            if response and self.turns.is_current(turn_id):
                # Commit only after the LLM stream completed.  A failed or
                # cancelled request must not leave an orphan user message in
                # the next request's context.  TTS failure after this point
                # does not erase a valid LLM exchange.
                logger.info(
                    "controller.history_committed turn_id=%s messages=%d response_len=%d",
                    turn_id,
                    len(self.history.messages),
                    len(response),
                )
            if tts_session is not None:
                try:
                    flush = getattr(tts_session, "flush", None)
                    if flush is not None and getattr(tts_session, "bidi", False):
                        await flush()
                    elif not getattr(tts_session, "bidi", False):
                        await tts_session.finish()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tts_failed = tts_failed or exc
                if tts_reader is not None:
                    try:
                        await tts_reader
                        alignment_events = getattr(tts_session, "alignment_events", ())
                        for alignment in alignment_events[alignment_cursor:]:
                            if alignment is not None:
                                resolver.add_provider_alignment(alignment)
                        alignment_cursor = len(alignment_events)
                        # Bidi can finish a flush without exposing a reliable
                        # sentence boundary (for example when subtitle data
                        # is unavailable).  Close the local fallback spans at
                        # the final returned PCM position so delivery stays
                        # conservative without waiting for a guessed ratio.
                        if getattr(tts_session, "generation_finished", False) and self.player is not None:
                            resolver.complete_pending(self.player.enqueued_ms)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        tts_failed = tts_failed or exc
                if self.player is not None and self.turns.is_current(turn_id):
                    try:
                        await self.player.wait_until_output_complete(
                            timeout_seconds=max(120.0, self.player.buffered_ms / 1000 + 30.0)
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        tts_failed = tts_failed or exc
            if self.tts is not None and self.turns.is_current(turn_id):
                self.state.current["audio_buffer_ms"] = self.player.buffered_ms if self.player else 0
                status = "DEGRADED" if tts_failed else "FINISHED"
                self.state.current["tts_status"] = status
                await self._publish(event(
                    "tts.status", turn_id, provider="minimax", status=status,
                    waiting_text="", audio_buffer_ms=self.state.current["audio_buffer_ms"],
                    error=str(tts_failed) if tts_failed else None,
                ))
        finally:
            if tts_reader is not None:
                if not tts_reader.done():
                    tts_reader.cancel()
                await asyncio.gather(tts_reader, return_exceptions=True)
            if tts_session is not None and not (getattr(tts_session, "reusable", False) and tts_failed is None):
                await tts_session.close()
            if self._tts_session is tts_session:
                self._tts_session = tts_session if getattr(tts_session, "reusable", False) and tts_failed is None else None
        logger.info("controller.llm_pipeline_done turn_id=%s response_len=%d response=%r", turn_id, len(response), response[:160])
        return response

    async def _finish_turn(self, turn_id: str, response: str) -> None:
        if not self.turns.is_current(turn_id):
            return
        metrics = self._metrics[turn_id]
        metrics.completed_at = time.monotonic()
        latency = metrics.values()
        delivery = {
            "status": "full" if response else "none",
            "termination": "completed",
            "heard_text": response,
            "uncertain_text": "",
            "unheard_text": "",
            "precision": "complete",
        }
        if self.player is not None:
            progress_reader = getattr(self.player, "progress", None)
            if callable(progress_reader):
                progress = progress_reader(turn_id)
                delivery["played_ms"] = int(progress.get("output_estimated_ms", progress.get("played_ms", 0)) or 0)
                delivery["callback_played_ms"] = int(progress.get("played_ms", 0) or 0)
                delivery["output_tail_estimated_ms"] = max(
                    0,
                    delivery["callback_played_ms"] - delivery["played_ms"],
                )
                delivery["playback_precision"] = "device_time" if progress.get("device_output_time") is not None else "callback"
        if self.state.current.get("tts_status") == "DEGRADED":
            snapshot = self._stop_playback_snapshot(turn_id)
            resolver = self._delivery_resolvers.get(turn_id)
            if resolver is not None:
                delivery = resolver.resolve(snapshot or {"started": False}, response)
            else:
                delivery = {
                    "status": "unknown",
                    "heard_text": "",
                    "uncertain_text": response,
                    "unheard_text": "",
                    "precision": "unknown",
                    "played_ms": snapshot.output_estimated_ms if snapshot else 0,
                }
            delivery["termination"] = "failed"
            self._add_playback_delivery_fields(delivery, snapshot)
        self.state.current["delivery"] = dict(delivery)
        self._interrupted_turns.add(turn_id)
        self.history.append_exchange(self.state.current["final_text"], delivery.get("heard_text", ""), delivery=delivery)
        self.history.append_record(
            turn_id=turn_id,
            transcript=self.state.current["final_text"],
            response=response,
            latency=latency,
            delivery=delivery,
        )
        await self._publish(
            event(
                "latency.snapshot",
                turn_id,
                complete=True,
                values=latency,
            )
        )
        self.state.latency = {"complete": True, "values": latency}
        await self._publish(
            event(
                "turn.completed",
                turn_id,
                transcript=self.state.current["final_text"],
                response=response,
                completed_at=utc_now_iso(),
                latency=latency,
                status=delivery.get("termination", "completed"),
                delivery=delivery,
            )
        )
        self.state.current["audio_buffer_ms"] = self.player.buffered_ms if self.player else 0
        self._active_input = False
        self.turns.invalidate()
        await self._state_event(TerminalState.IDLE, listening=self.state.listening)
        logger.info(
            "controller.turn_completed turn_id=%s transcript_len=%d response_len=%d listening=%s",
            turn_id,
            len(self.state.current["final_text"]),
            len(response),
            self.state.listening,
        )

    async def _on_asr_result(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        turn_id = message.get("turn_id")
        text = message.get("text")
        if not isinstance(turn_id, str) or not isinstance(text, str) or not self.turns.is_current(turn_id):
            if message_type == "error":
                logger.error("controller.asr_error turn_id=%s code=%s message=%s", turn_id, message.get("code"), message.get("message"))
                await self._error(
                    str(message.get("code", "ASR_WORKER_ERROR")),
                    str(message.get("message", "ASR Worker 错误")),
                    recoverable=False,
                    turn_id=turn_id if isinstance(turn_id, str) else None,
                )
            return
        if turn_id not in self._metrics or turn_id not in self._asr_final_events:
            logger.warning("controller.late_asr_result_ignored type=%s turn_id=%s", message_type, turn_id)
            return
        logger.info(
            "controller.asr_result type=%s turn_id=%s text_len=%d text=%r",
            message_type,
            turn_id,
            len(text),
            text[:160],
        )
        metrics = self._metrics[turn_id]
        metrics.asr_first_text_at = metrics.asr_first_text_at or time.monotonic()
        if message_type == "asr.partial":
            self._asr_latest_text[turn_id] = text
            self.state.current["partial_text"] = text
            await self._publish(event("transcript.partial", turn_id, text=text))
            await self._publish_latency(turn_id)
        elif message_type == "asr.final":
            self._asr_final_text[turn_id] = text or self._asr_latest_text.get(turn_id, "")
            self._asr_final_events[turn_id].set()

    def _on_playback_started(self, turn_id: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def publish() -> None:
            if not self.turns.is_current(turn_id):
                return
            metrics = self._metrics[turn_id]
            metrics.playback_started_at = metrics.playback_started_at or time.monotonic()
            self.state.current["audio_buffer_ms"] = self.player.buffered_ms if self.player else 0
            await self._publish(
                event(
                    "tts.status",
                    turn_id,
                    provider="minimax",
                    status="PLAYING",
                    waiting_text=self.state.current["tts_text"],
                    audio_buffer_ms=self.state.current["audio_buffer_ms"],
                )
            )

        loop.call_soon_threadsafe(lambda: asyncio.create_task(publish()))

    async def _error(self, code: str, message: str, *, recoverable: bool, turn_id: str | None = None) -> None:
        logger.error(
            "controller.error code=%s recoverable=%s turn_id=%s message=%s",
            code,
            recoverable,
            turn_id,
            message,
        )
        await self._publish(event("error", turn_id, code=code, message=message, recoverable=recoverable))
        if not recoverable:
            self.state.set_state(TerminalState.ERROR, listening=self.state.listening)
            await self._publish(
                event(
                    "session.state",
                    turn_id,
                    state=TerminalState.ERROR.value,
                    listening=self.state.listening,
                    can_start=False,
                )
            )

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..asr.client import AsrWorkerClient
from ..asr.protocol import AudioChunk
from ..audio.capture import AudioCapture
from ..audio.player import PcmPlayer
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
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listening_started_at: float | None = None
        self._audio_blocks = 0
        self._audio_bytes = 0
        self._last_audio_log_at = 0.0

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
        await self._publish(VoiceEvent(type="runtime.snapshot", turn_id=None, payload=self.state.snapshot()))

    async def initialize(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.capture is not None:
            self.state.set_service("audio", "READY")
        self.state.set_service("kws", "READY" if self.wake_enabled and self.kws is not None else "DISABLED")
        if self.llm is not None:
            self.state.set_service("llm", "READY")
        if self.tts is not None:
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
        await self._publish(VoiceEvent(type="runtime.snapshot", turn_id=None, payload=self.state.snapshot()))

    async def start_listening(self) -> None:
        if self.state.listening:
            logger.info("controller.start_ignored reason=already_listening")
            return
        missing = [
            name
            for name, status in self.state.services.items()
            if status not in {"READY", "RUNNING", "STREAMING", "CONNECTED"}
            and not (name == "kws" and status == "DISABLED")
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
                await self._cancel_turn()
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
            if activity.speech_started:
                self._barge_in_started_at = time.monotonic()
                logger.info("controller.barge_in_candidate turn_id=%s", turn_id)
            if activity.speech_ended:
                self._barge_in_started_at = None
            if (
                activity.is_speech
                and self._barge_in_started_at is not None
                and (time.monotonic() - self._barge_in_started_at) * 1000 >= self.vad_min_speech_ms
            ):
                logger.info("controller.interruption_detected turn_id=%s", turn_id)
                await self._interrupt_and_restart(chunk)

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

    async def _interrupt_and_restart(self, chunk: bytes) -> None:
        old_turn = self.turns.current
        logger.info("controller.turn_interrupt old_turn=%s seed_bytes=%d", old_turn, len(chunk))
        await self._state_event(TerminalState.INTERRUPTING, listening=True, turn_id=old_turn)
        await self._cancel_turn()
        if self.player is not None:
            self.player.clear()
        await self._begin_turn(seed_chunk=chunk, reset_vad=False, initial_speech=True)

    async def _cancel_turn(self) -> None:
        old_turn = self.turns.current
        self._active_input = False
        self._barge_in_started_at = None
        if self._turn_cancel is not None:
            self._turn_cancel.set()
        task, self._completion_task = self._completion_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._tts_session is not None:
            await self._tts_session.close()
            self._tts_session = None
        if self.player is not None:
            self.player.clear()
        self.turns.invalidate()
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
                await self._cancel_turn()
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
        bridge = TextBridge(**self.bridge_options)
        tts_session: MiniMaxTtsSession | None = None
        tts_reader: asyncio.Task[None] | None = None

        async def consume_audio(session: MiniMaxTtsSession) -> None:
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
            nonlocal tts_session, tts_reader
            if not segments or self.tts is None:
                return
            if tts_session is None:
                logger.info("controller.tts_start turn_id=%s", turn_id)
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
                await send_segments(bridge.feed(delta))
            await send_segments(bridge.finish())
            response = "".join(response_parts).strip()
            if response and self.turns.is_current(turn_id):
                # Commit only after the LLM stream completed.  A failed or
                # cancelled request must not leave an orphan user message in
                # the next request's context.  TTS failure after this point
                # does not erase a valid LLM exchange.
                self.history.append_exchange(transcript, response)
                logger.info(
                    "controller.history_committed turn_id=%s messages=%d response_len=%d",
                    turn_id,
                    len(self.history.messages),
                    len(response),
                )
            if tts_session is not None:
                logger.info("controller.tts_finish turn_id=%s", turn_id)
                await tts_session.finish()
                if tts_reader is not None:
                    await tts_reader
                if self.player is not None and self.turns.is_current(turn_id):
                    await self.player.wait_until_empty()
                    self.state.current["audio_buffer_ms"] = self.player.buffered_ms
                    self.state.current["tts_status"] = "FINISHED"
                    await self._publish(
                        event(
                            "tts.status",
                            turn_id,
                            provider="minimax",
                            status="FINISHED",
                            waiting_text="",
                            audio_buffer_ms=self.player.buffered_ms,
                        )
                    )
        finally:
            if tts_session is not None:
                await tts_session.close()
            if self._tts_session is tts_session:
                self._tts_session = None
        logger.info("controller.llm_pipeline_done turn_id=%s response_len=%d response=%r", turn_id, len(response), response[:160])
        return response

    async def _finish_turn(self, turn_id: str, response: str) -> None:
        if not self.turns.is_current(turn_id):
            return
        metrics = self._metrics[turn_id]
        metrics.completed_at = time.monotonic()
        latency = metrics.values()
        self.history.append_record(turn_id=turn_id, transcript=self.state.current["final_text"], response=response, latency=latency)
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

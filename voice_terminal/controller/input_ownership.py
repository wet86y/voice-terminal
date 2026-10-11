"""KWS/ASR input ownership and continuity operations."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from uuid import uuid4

from .turn_workspace import AudioFrame
from ..asr.protocol import AudioChunk
from ..core.state import TerminalState
from ..kws.service import KwsAudioFrame

logger = logging.getLogger(__name__)


class InputOwnershipMixin:
    async def set_wake_settings(self, worker, settings) -> None:
        """set_wake_settings（热更新唤醒）：保留正在处理的语句，关闭待机时交给识别。"""
        previously_enabled = self.wake_always_on
        if (not settings.enabled and self.state.listening and self.state.input_owner == "kws"):
            self.state.input_owner = "none"
            self._activation_generation += 1
            await self._reset_owner_boundary()
            self.state.input_owner = "asr"
            self.state.set_service("asr", "RUNNING")
        elif worker is not self.kws and self.state.input_owner == "kws":
            # 旧检测结果在换入后不能再触发输入归属切换。
            self._activation_generation += 1
        self.kws = worker
        self.wake_enabled = bool(settings.enabled)
        self.wake_always_on = bool(settings.enabled)
        self.state.wake_always_on = bool(settings.enabled)
        self.wake_keyword = settings.keyword
        self.wake_keywords = settings.keywords
        episode = getattr(self, "episode", None)
        if episode is not None:
            episode.defer_silence_until_output = self.wake_always_on
        # 开启关键词时保留正在进行的识别；空闲输入立即转入待机。
        if (settings.enabled and not previously_enabled and self.state.listening and self.state.input_owner == "asr"
                and self.workspace is None and not self._output_is_busy()
                and getattr(episode, "user_speaking", False) is not True):
            await self._return_to_kws(self._activation_generation)

    async def start_listening(self) -> None:
        # Analysis is optional and never becomes a mandatory start prerequisite.
        status = self.state.services.pop("analysis", "DISABLED")
        try:
            await super().start_listening()
        finally:
            self.state.services["analysis"] = status
        if self.state.listening and (self._asr_sender is None or self._asr_sender.done()):
            self._asr_sender = asyncio.create_task(self._send_asr_loop())
        # 手动开始直接进入 ASR（语音识别）；KWS（关键词唤醒）由静默调度重新接管。

    async def _reset_owner_boundary(self) -> None:
        self._drain_asr_queue()
        self._asr_queue_overflow = False
        self._asr_queue_overflow_at = None
        self._recent.clear()
        self._last_frame = None
        self._vad_origin = None
        self._speech_start_sample = None
        self._speech_end_sample = None
        self._speech_samples = 0
        self._resume_started = None
        self._vad_generation += 1
        self._reset_vad_pending = False
        if self.vad is not None:
            await asyncio.to_thread(self.vad.reset)

    async def _fail_kws_closed(self, message: str) -> None:
        self._activation_generation += 1
        self.state.input_owner = "none"
        self.state.listening = False
        self.state.set_service("kws", "ERROR")
        if self.workspace is not None:
            await self._cancel_turn(reason="failed", record=False)
        if self.capture is not None:
            await self.capture.stop()
            self.state.set_service("audio", "READY")
        await self._error("KWS_WORKER_FAILED", message, recoverable=True)
        await self._state_event(TerminalState.IDLE, listening=False)

    async def _on_kws_activated(self, source_id: str, generation: int) -> bool:
        return True

    async def _activate_kws_detection(self) -> None:
        if not self.wake_always_on or self.state.input_owner != "kws":
            return
        expected_generation = self._activation_generation
        if self.kws is None or self.kws.error is not None or not self.kws.running:
            await self._fail_kws_closed(
                self.kws.error if self.kws is not None and self.kws.error else "KWS worker unavailable"
            )
            return
        try:
            if self.asr is None:
                raise RuntimeError("ASR Worker 客户端未初始化。")
            if not self.asr.connected:
                await self.asr.connect()
            health = getattr(self.asr, "health", None)
            if callable(health):
                await health()
        except Exception as exc:
            self.state.set_service("asr", "ERROR")
            self.kws.reset()
            await self._error("ASR_WORKER_NOT_READY", str(exc), recoverable=True)
            await self._state_event(TerminalState.IDLE, listening=True)
            return
        if (not self.state.listening or self.state.input_owner != "kws"
                or expected_generation != self._activation_generation):
            return
        self._activation_generation += 1
        generation = self._activation_generation
        source_id = f"wake-{uuid4().hex}"
        self.kws.reset()
        await self._reset_owner_boundary()
        self.state.input_owner = "asr"
        self.state.set_service("kws", "READY")
        self.state.set_service("asr", "RUNNING")
        if not await self._on_kws_activated(source_id, generation):
            await self._return_to_kws(generation)
            return
        await self._state_event(TerminalState.IDLE, listening=True)

    async def activate_asr_for_model_speech(self) -> bool:
        """Rotate KWS→ASR when a model speech transaction is accepted.

        Returns ``True`` only when this call really moved input ownership, so
        the caller can arm the matching silence window.
        """
        if not self.wake_always_on or not self.state.listening or self.state.input_owner != "kws":
            return False
        generation = self._activation_generation
        try:
            if self.asr is None:
                raise RuntimeError("ASR Worker 客户端未初始化。")
            if not self.asr.connected:
                await self.asr.connect()
            health = getattr(self.asr, "health", None)
            if callable(health):
                await health()
        except Exception as exc:
            self.state.set_service("asr", "ERROR")
            await self._error("ASR_WORKER_NOT_READY", str(exc), recoverable=True)
            return False
        if not self.state.listening or self.state.input_owner != "kws" or generation != self._activation_generation:
            return False
        self._activation_generation += 1
        if self.kws is not None:
            self.kws.reset()
        await self._reset_owner_boundary()
        self.state.input_owner = "asr"
        self.state.set_service("kws", "READY")
        self.state.set_service("asr", "RUNNING")
        await self._state_event(TerminalState.IDLE, listening=True)
        return True

    async def _return_to_kws(self, generation: int | None = None) -> bool:
        if (not self.wake_always_on or not self.state.listening
                or self.state.input_owner != "asr"):
            return False
        if generation is not None and generation != self._activation_generation:
            return False
        if self.kws is None or self.kws.error is not None or not self.kws.running:
            await self._fail_kws_closed(
                self.kws.error if self.kws is not None and self.kws.error else "KWS worker unavailable"
            )
            return False
        self.state.input_owner = "none"
        self._activation_generation += 1
        transition_generation = self._activation_generation
        self.kws.reset()
        try:
            await self._reset_owner_boundary()
        except Exception as exc:
            await self._fail_kws_closed(f"KWS rearm failed: {exc}")
            return False
        if (not self.state.listening
                or transition_generation != self._activation_generation):
            return False
        self.state.input_owner = "kws"
        self.state.set_service("kws", "RUNNING")
        self.state.set_service(
            "asr", "READY" if self.asr is not None and self.asr.connected else "ERROR",
        )
        await self._state_event(TerminalState.IDLE, listening=True)
        return True

    async def _ensure_asr_ready(self, *, preserve_listening: bool = False) -> bool:
        """Reconnect and perform an application-level ASR readiness check."""

        if self.asr is None:
            self.state.services["asr"] = "ERROR"
            await self._error("ASR_WORKER_UNAVAILABLE", "ASR Worker 客户端未初始化。", recoverable=True)
            await self._state_event(
                TerminalState.IDLE,
                listening=self.state.listening if preserve_listening else False,
            )
            return False
        try:
            if not self.asr.connected:
                await self.asr.connect()
            health = getattr(self.asr, "health", None)
            if health is not None:
                await health()
            self.state.services["asr"] = "READY"
            return True
        except Exception as exc:
            code = getattr(exc, "code", "ASR_WORKER_NOT_READY")
            self.state.services["asr"] = "ERROR"
            await self._error(str(code), str(exc), recoverable=True)
            await self._state_event(
                TerminalState.IDLE,
                listening=self.state.listening if preserve_listening else False,
            )
            return False

    async def _handle_asr_disconnect(self) -> None:
        """Quiesce a broken input transport and schedule one safe reconnect."""

        self.state.services["asr"] = "ERROR"
        if self.wake_always_on and self.state.listening:
            sender = self._asr_sender
            if sender is not None and sender is not asyncio.current_task() and not sender.done():
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            self._asr_sender = None
            self._drain_asr_queue()
            if self.kws is None or self.kws.error is not None or not self.kws.running:
                await self._fail_kws_closed(
                    self.kws.error if self.kws is not None and self.kws.error else "KWS worker unavailable"
                )
                return
            self.state.input_owner = "none"
            self._activation_generation += 1
            transition_generation = self._activation_generation
            self.kws.reset()
            try:
                await self._reset_owner_boundary()
            except Exception as exc:
                await self._fail_kws_closed(f"KWS recovery failed: {exc}")
                return
            if (not self.state.listening
                    or transition_generation != self._activation_generation):
                return
            self.state.input_owner = "kws"
            self.state.services["kws"] = "RUNNING"
            self.state.services["audio"] = "RUNNING"
            if (not self._closed and
                    (self._asr_recovery_task is None or self._asr_recovery_task.done())):
                self._asr_recovery_task = asyncio.create_task(
                    self._recover_asr_session(), name="asr-session-recovery",
                )
                self._track(self._asr_recovery_task)
            return
        self.state.listening = False
        listen_task = self._listen_task
        if listen_task is not None and listen_task is not asyncio.current_task() and not listen_task.done():
            listen_task.cancel()
            await asyncio.gather(listen_task, return_exceptions=True)
        self._listen_task = None
        sender = self._asr_sender
        if sender is not None and sender is not asyncio.current_task() and not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        self._asr_sender = None
        while True:
            try:
                self._asr_queue.get_nowait()
                self._asr_queue.task_done()
            except asyncio.QueueEmpty:
                break
        if self.capture is not None:
            try:
                await self.capture.stop()
            except Exception:
                # The transport failure is the actionable error; a concurrent
                # capture shutdown failure must not hide it.
                pass
            self.state.services["audio"] = "READY"
        self.state.services["kws"] = "READY" if self.wake_enabled and self.kws is not None else "DISABLED"
        if (not self._closed and
                (self._asr_recovery_task is None or self._asr_recovery_task.done())):
            self._asr_recovery_task = asyncio.create_task(
                self._recover_asr_session(),
                name="asr-session-recovery",
            )
            self._track(self._asr_recovery_task)

    async def _recover_asr_session(self) -> None:
        """Attempt one delayed restart after a transient ASR worker loss.

        The failed sender has already been detached before this task is
        created.  Retrying after a short delay keeps the normal readiness
        handshake as the sole authority and avoids a reconnect storm when the
        worker is genuinely down; a failed retry leaves the visible runtime in
        a restartable idle/error state for the user.
        """

        try:
            await asyncio.sleep(0.5)
            if self._closed:
                return
            if self.wake_always_on and self.state.listening and self.state.input_owner == "kws":
                logger.info("controller.asr_recovery_attempt")
                if await self._ensure_asr_ready(preserve_listening=True):
                    self.state.services["asr"] = "READY"
                    if self._asr_sender is None or self._asr_sender.done():
                        self._asr_sender = asyncio.create_task(self._send_asr_loop())
                    await self._state_event(TerminalState.IDLE, listening=True)
                    logger.info("controller.asr_recovery_succeeded")
                return
            if self.state.listening:
                return
            logger.info("controller.asr_recovery_attempt")
            await self.start_listening()
            if self.state.listening:
                logger.info("controller.asr_recovery_succeeded")
            else:
                logger.warning("controller.asr_recovery_not_ready")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("controller.asr_recovery_failed")

    def _queue_asr(self, item) -> None:
        # No awaited network writes on the capture path.  A full queue means
        # ASR can no longer be given a contiguous input stream.  Do not throw
        # from this helper: that used to stop the entire microphone session.
        # The next capture frame performs a controlled resynchronisation.
        try:
            self._asr_queue.put_nowait(item)
        except asyncio.QueueFull:
            self._asr_queue_overflow = True
            self._asr_queue_overflow_at = time.monotonic()
            logger.warning(
                "controller.asr_queue_overflow queue_capacity=%d item_type=%s turn_id=%s",
                self._asr_queue.maxsize,
                type(item).__name__,
                getattr(item, "turn_id", item),
            )

    def _drain_asr_queue(self) -> None:
        while True:
            try:
                self._asr_queue.get_nowait()
                self._asr_queue.task_done()
            except asyncio.QueueEmpty:
                return

    async def _send_asr_loop(self) -> None:
        while True:
            item = await self._asr_queue.get()
            try:
                if isinstance(item, AudioChunk):
                    if item.turn_id in self._sub_parts:
                        await self.asr.send_audio(item)
                else:
                    await self.asr.send_finish(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._cancel_turn(reason="failed", record=False)
                code = getattr(exc, "code", None)
                if not isinstance(code, str):
                    code = "ASR_WORKER_DISCONNECTED" if "ConnectionClosed" in type(exc).__name__ else "ASR_SEND_FAILED"
                # Any failed write leaves the sender's transport unusable.
                # Keep the recovery path identical for protocol disconnects
                # and other send failures so the capture loop cannot continue
                # feeding a dead ASR connection.
                await self._handle_asr_disconnect()
                await self._error(code, str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=self.state.listening)
                return
            finally:
                self._asr_queue.task_done()

    async def _recover_input_continuity(
        self,
        frame: AudioFrame,
        *,
        reason: str,
        expected_start_sample: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Discard only an unverifiable input transaction and keep listening.

        PCM must never be joined across a capture or ASR backpressure hole.
        The old path correctly rejected the corrupt turn but escalated it to a
        terminal input-loop failure.  Output ownership deliberately remains
        separate: abandoning an input cannot silence an already authorised
        speech transaction.
        """

        if not hasattr(self, "_vad_generation"):
            self._vad_generation = 0
        ws = self.workspace
        capture_diagnostics = getattr(self.capture, "diagnostics", {}) if self.capture is not None else {}
        detail = {
            "reason": reason,
            "expected_start_sample": expected_start_sample,
            "actual_start_sample": frame.start_sample,
            "dropped_chunks": capture_diagnostics.get("dropped_chunks"),
            "asr_queue_depth": self._asr_queue.qsize(),
        }
        if error is not None:
            # A per-frame controller exception is just as unsafe to append to
            # the in-flight ASR segment as a physical sample gap.  Preserve
            # its class/message for diagnosis, but never turn it into a
            # user-visible stop request or silently lose the cause.
            detail["error_type"] = type(error).__name__
            detail["error"] = str(error)[:300]
        logger.warning(
            "controller.input_continuity_recovered reason=%s turn_id=%s expected_start_sample=%s "
            "actual_start_sample=%s asr_queue_depth=%d",
            reason,
            ws.turn_id if ws else None,
            expected_start_sample,
            frame.start_sample,
            self._asr_queue.qsize(),
        )
        self._drain_asr_queue()
        self._asr_queue_overflow = False
        self._asr_queue_overflow_at = None
        if ws is not None:
            await self._cancel_turn(reason="capture_gap", record=True)
        self._recent.clear()
        self._last_frame = frame
        self._vad_origin = frame.start_sample
        self._speech_start_sample = None
        self._speech_end_sample = None
        self._speech_samples = 0
        self._resume_started = None
        self.barge_in_gate.reset()
        self._vad_generation += 1
        if self.vad is not None:
            await asyncio.to_thread(self.vad.reset)
        self._reset_vad_pending = False
        self.state.current.update(capture_diagnostics=detail)
        error_code = "AUDIO_CAPTURE_GAP" if error is None else "AUDIO_INPUT_FRAME_FAILED"
        error_message = (
            "音频采集出现缺口，已放弃当前未完成输入并继续监听。"
            if error is None
            else "音频帧处理异常，已放弃当前未完成输入并继续监听。"
        )
        await self._error(
            error_code,
            error_message,
            recoverable=True,
            turn_id=ws.turn_id if ws else None,
        )
        await self._state_event(TerminalState.LISTENING, listening=True)

    async def _listen_loop(self) -> None:
        while self.state.listening and not self._closed:
            try:
                frame = await self.capture.read_frame()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Failure to read the physical capture source means there is
                # no trustworthy next frame and the session must be settled.
                # Keep this separate from below: a controller/VAD/ASR queue
                # exception while handling an otherwise valid frame is
                # recoverable and must not make TTS end the user's listening
                # session.
                logger.exception("controller.input_capture_read_failed")
                await self._cancel_turn(reason="failed", record=False)
                self.state.listening = False
                try:
                    await self.capture.stop()
                except Exception:
                    logger.exception("controller.input_audio_loop_stop_capture_failed")
                sender = self._asr_sender
                if sender is not None and sender is not asyncio.current_task() and not sender.done():
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                self._asr_sender = None
                while True:
                    try:
                        self._asr_queue.get_nowait()
                        self._asr_queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                if self.asr is not None:
                    try:
                        await self.asr.close()
                    except Exception:
                        logger.exception("controller.input_audio_loop_close_asr_failed")
                self.state.services["audio"] = "READY" if self.capture is not None else "ERROR"
                self.state.services["kws"] = "READY" if self.wake_enabled and self.kws is not None else "DISABLED"
                self.state.services["asr"] = "READY" if self.asr is not None and self.vad is not None else "ERROR"
                await self._error("AUDIO_PIPELINE_FAILED", str(exc), recoverable=True)
                await self._state_event(TerminalState.IDLE, listening=self.state.listening)
                break
            try:
                await self.handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "controller.input_frame_failed reason=frame_processing_error start_sample=%s",
                    frame.start_sample,
                )
                try:
                    await self._recover_input_continuity(
                        frame,
                        reason="frame_processing_error",
                        expected_start_sample=self._last_frame.end_sample if self._last_frame else None,
                        error=exc,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Do not hide the original error, and do not spin a
                    # broken listener indefinitely.  This is now the only
                    # path where frame-level recovery itself failed.
                    logger.exception("controller.input_frame_recovery_failed")
                    await self._cancel_turn(reason="failed", record=False)
                    self.state.listening = False
                    try:
                        await self.capture.stop()
                    except Exception:
                        logger.exception("controller.input_frame_recovery_stop_capture_failed")
                    if self.asr is not None:
                        try:
                            await self.asr.close()
                        except Exception:
                            logger.exception("controller.input_frame_recovery_close_asr_failed")
                    self.state.services["audio"] = "READY" if self.capture is not None else "ERROR"
                    self.state.services["asr"] = "READY" if self.asr is not None and self.vad is not None else "ERROR"
                    await self._error("AUDIO_PIPELINE_FAILED", "音频帧恢复失败，请重新开始监听。", recoverable=True)
                    await self._state_event(TerminalState.IDLE, listening=False)
                    break

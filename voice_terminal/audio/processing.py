from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class AudioProcessingError(RuntimeError):
    pass


class AudioPreprocessor:
    """Optional WebRTC-style near-end processing for VAD and ASR.

    The dependency is imported lazily so deterministic tests remain
    usable on machines without the native wheel.  When protection is required,
    callers must check ``available`` before enabling automatic barge-in.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        reverse_sample_rate: int = 32000,
        enabled: bool = False,
        required: bool = False,
        enable_aec: bool = True,
        enable_ns: bool = True,
        enable_agc: bool = False,
        vad_aggressiveness: int = 2,
    ) -> None:
        if vad_aggressiveness < 0 or vad_aggressiveness > 3:
            raise ValueError("vad_aggressiveness must be between 0 and 3")
        self.sample_rate = sample_rate
        self.channels = channels
        self.reverse_sample_rate = reverse_sample_rate
        self.enabled = enabled
        self.required = required
        self.vad_aggressiveness = vad_aggressiveness
        self._enable_aec = enable_aec
        self._enable_ns = enable_ns
        self._enable_agc = enable_agc
        self._processor: Any = None
        self._near_end_vad: Any = None
        self._input_remainder = bytearray()
        self._reverse_remainder = bytearray()
        self._reverse_queue: queue.Queue[bytes | None] | None = None
        self._reverse_thread: threading.Thread | None = None
        self._reverse_stop = threading.Event()
        self._processor_lock = threading.RLock()
        self.last_voice_activity: bool | None = None
        self._last_reverse_at: float | None = None
        self.recovery_count = 0
        self.last_recovery_ms: int | None = None
        self.status = "DISABLED" if not enabled else "DEGRADED"
        self.error: str | None = None
        if enabled:
            self._start_processor(initial=True)

    def _start_processor(self, *, initial: bool) -> bool:
        """(Re)create the native AEC chain.  No capture samples are dropped here."""
        try:
            from aec_audio_processing import AudioProcessor  # type: ignore
            import webrtcvad  # type: ignore

            try:
                self._processor = AudioProcessor(
                    enable_aec=self._enable_aec,
                    enable_ns=self._enable_ns,
                    enable_agc=self._enable_agc,
                    enable_vad=True,
                )
            except TypeError:
                self._processor = AudioProcessor(
                    echo_cancellation=self._enable_aec,
                    noise_suppression=self._enable_ns,
                    auto_gain_control=self._enable_agc,
                )
            self._configure_format()
            # ``aec-audio-processing==1.0.1`` exposes ``has_voice`` but its
            # distributed Windows wrapper leaves the native VAD object
            # uninitialised (the method consequently stays false).  Use the
            # maintained WebRTC VAD binding on the *processed* 10 ms frames
            # for near-end voice confirmation.  FSMN-VAD still supplies the
            # speech candidate; this detector is the independent protection
            # gate used while the assistant is playing.
            self._near_end_vad = webrtcvad.Vad(self.vad_aggressiveness)
            self._reverse_queue = queue.Queue(maxsize=128)
            self._reverse_thread = threading.Thread(
                target=self._reverse_loop,
                name="audio-aec-reverse",
                daemon=True,
            )
            self._reverse_thread.start()
            self.status = "READY" if initial else "RECOVERED"
            self.error = None
            logger.info(
                "audio.processing_ready sample_rate=%d channels=%d reverse_sample_rate=%d "
                "aec=%s ns=%s agc=%s near_vad=webrtcvad mode=%d",
                self.sample_rate,
                self.channels,
                self.reverse_sample_rate,
                self._enable_aec,
                self._enable_ns,
                self._enable_agc,
                self.vad_aggressiveness,
            )
        except Exception as exc:
            self.error = str(exc) or "audio processor unavailable"
            self.status = "ERROR" if self.required and initial else "DEGRADED"
            if self.required and initial:
                raise AudioProcessingError(f"音频前处理初始化失败：{self.error}") from exc
            logger.warning("audio.processing_unavailable error=%s", self.error)
            return False
        return True

    @property
    def available(self) -> bool:
        return self._processor is not None and self.status in {"READY", "RECOVERED"}

    @property
    def protected_interruption_available(self) -> bool:
        if not self.available or self._reverse_thread is None:
            return False
        # A healthy processor without a current player reference must not be
        # advertised as protected.  The callback may stop while the native
        # object remains alive (device unplug, stream restart, or backend
        # fault); after a short grace period that is an explicit degradation,
        # never a silent fallback to raw microphone VAD.
        if self._last_reverse_at is None:
            return False
        if time.monotonic() - self._last_reverse_at > 0.5:
            self._degrade(RuntimeError("AEC reverse reference stream stalled"))
            return False
        return True

    def _configure_format(self) -> None:
        if self._processor is None:
            return
        for name in ("set_stream_format", "set_format"):
            method = getattr(self._processor, name, None)
            if method is None:
                continue
            try:
                method(self.sample_rate, self.channels)
            except TypeError:
                method(
                    sample_rate_in=self.sample_rate,
                    channel_count_in=self.channels,
                    sample_rate_out=self.sample_rate,
                    channel_count_out=self.channels,
                )
            break
        reverse = getattr(self._processor, "set_reverse_stream_format", None)
        if reverse is not None:
            try:
                # The processor operates at the microphone rate.  The
                # player's 32 kHz reverse reference is downsampled before it
                # reaches this 16 kHz reverse stream.
                reverse(self.sample_rate, self.channels)
            except TypeError:
                pass

    def push_reverse(self, pcm: bytes) -> None:
        """Queue the player reference without doing DSP in PortAudio's callback."""

        if not pcm or not self.available or self._reverse_queue is None:
            return
        self._last_reverse_at = time.monotonic()
        try:
            self._reverse_queue.put_nowait(bytes(pcm))
        except queue.Full:
            self._degrade(RuntimeError("AEC reverse reference queue is full"))

    def _reverse_loop(self) -> None:
        assert self._reverse_queue is not None
        while not self._reverse_stop.is_set():
            try:
                pcm = self._reverse_queue.get(timeout=.1)
            except queue.Empty:
                continue
            if pcm is None:
                return
            if self.reverse_sample_rate != self.sample_rate:
                pcm = self._downsample_pcm(pcm, self.reverse_sample_rate, self.sample_rate)
            self._process_reverse_frames(pcm)

    def _process_reverse_frames(self, pcm: bytes) -> None:
        with self._processor_lock:
            frame_bytes = max(2, self.sample_rate // 100 * self.channels * 2)
            self._reverse_remainder.extend(pcm)
            while len(self._reverse_remainder) >= frame_bytes:
                frame = bytes(self._reverse_remainder[:frame_bytes])
                del self._reverse_remainder[:frame_bytes]
                for name in ("process_reverse_stream", "analyze_reverse_stream", "set_reverse_stream"):
                    method = getattr(self._processor, name, None)
                    if method is None:
                        continue
                    try:
                        method(frame)
                    except Exception as exc:
                        self._degrade(exc)
                    break

    @staticmethod
    def _downsample_pcm(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
        if source_rate <= target_rate or len(pcm) < 4 or len(pcm) % 2:
            return pcm
        stride = max(1, round(source_rate / target_rate))
        samples = memoryview(pcm).cast("h")
        return samples[::stride].tobytes()

    @staticmethod
    def _fit_frame(pcm: bytes, expected_bytes: int) -> bytes:
        """Keep the capture stream sample aligned across native bindings.

        The WebRTC wrapper normally returns exactly one frame, but a binding
        error or a future implementation may return a short/long buffer.  A
        short output would create a hole in the sample-indexed stream while a
        long output would shift all later VAD offsets.  Padding/truncating at
        this boundary keeps the 10 ms contract explicit; the original input
        is still used by the caller when processing is unavailable.
        """

        if len(pcm) < expected_bytes:
            return pcm + b"\x00" * (expected_bytes - len(pcm))
        return pcm[:expected_bytes]

    def process(self, pcm: bytes) -> bytes:
        if not pcm or not self.available:
            return pcm
        with self._processor_lock:
            frame_bytes = max(2, self.sample_rate // 100 * self.channels * 2)
            input_before = bytes(self._input_remainder)
            self._input_remainder.extend(pcm)
            output_frames: list[bytes] = []
            voice_in_call: bool | None = None
            while len(self._input_remainder) >= frame_bytes:
                frame = bytes(self._input_remainder[:frame_bytes])
                del self._input_remainder[:frame_bytes]
                processed: bytes | None = None
                for name in ("process_stream", "process"):
                    method = getattr(self._processor, name, None)
                    if method is None:
                        continue
                    try:
                        output = method(frame)
                        if isinstance(output, bytes):
                            processed = output
                        elif hasattr(output, "tobytes"):
                            processed = output.tobytes()
                        else:
                            processed = frame
                        break
                    except Exception as exc:
                        self._degrade(exc)
                        self._input_remainder.clear()
                        if self.required:
                            raise AudioProcessingError(f"音频前处理失败：{exc}") from exc
                        # Return every byte from this call, including a
                        # partial frame carried over from the previous call;
                        # dropping that remainder would shift all subsequent
                        # VAD/ASR sample offsets.
                        return input_before + pcm
                output_frames.append(self._fit_frame(processed if processed is not None else frame, frame_bytes))
                frame_voice = self._update_near_end_voice(output_frames[-1])
                if frame_voice is not None:
                    voice_in_call = bool(voice_in_call) or frame_voice
            # PortAudio normally gives us a 20 ms block, but test sources and
            # some devices can produce a short final block.  Do not return an
            # empty PCM frame: doing so would move the next frame backwards
            # in the sample-indexed capture stream and make the ASR/VAD path
            # report a false capture gap.  Pad the tail only for the native
            # processor, then trim the processed result back to the exact
            # number of input bytes.  This preserves both sample continuity
            # and the 10 ms processing contract.
            if self._input_remainder:
                tail = bytes(self._input_remainder)
                self._input_remainder.clear()
                padded = tail + b"\x00" * (frame_bytes - len(tail))
                processed_tail: bytes | None = None
                for name in ("process_stream", "process"):
                    method = getattr(self._processor, name, None)
                    if method is None:
                        continue
                    try:
                        output = method(padded)
                        if isinstance(output, bytes):
                            processed_tail = output
                        elif hasattr(output, "tobytes"):
                            processed_tail = output.tobytes()
                        else:
                            processed_tail = padded
                    except Exception as exc:
                        self._degrade(exc)
                        if self.required:
                            raise AudioProcessingError(f"音频前处理失败：{exc}") from exc
                        processed_tail = tail
                    break
                output_frames.append((
                    self._fit_frame(processed_tail if processed_tail is not None else padded, frame_bytes)
                )[:len(tail)])
                # The VAD binding accepts only a complete 10 ms frame.  The
                # padded frame is the exact stream presented to the native
                # processor; use it for the activity decision and trim only
                # the returned PCM exposed to the caller.
                frame_voice = self._update_near_end_voice(padded)
                if frame_voice is not None:
                    voice_in_call = bool(voice_in_call) or frame_voice
            # Capture callbacks normally contain two 10 ms frames.  Keep an
            # OR over the complete callback instead of exposing only the last
            # subframe: otherwise a voiced onset in the first 10 ms followed
            # by a quiet second subframe would reset the interruption gate.
            if voice_in_call is not None:
                self.last_voice_activity = voice_in_call
        return b"".join(output_frames) if output_frames else pcm

    def _update_near_end_voice(self, pcm: bytes) -> bool | None:
        """Update the independent WebRTC voice decision for one 10 ms frame."""

        detector = self._near_end_vad
        if detector is None:
            self.last_voice_activity = None
            return None
        try:
            return bool(detector.is_speech(pcm, self.sample_rate))
        except Exception as exc:
            self.last_voice_activity = None
            self._degrade(exc)
            if self.required:
                raise AudioProcessingError(f"近端语音 VAD 失败：{exc}") from exc
            return None

    def reset(self) -> None:
        self._input_remainder.clear()
        self._reverse_remainder.clear()
        self.last_voice_activity = None
        self._last_reverse_at = None
        if self._reverse_queue is not None:
            while True:
                try:
                    self._reverse_queue.get_nowait()
                except queue.Empty:
                    break
        if self._processor is not None:
            with self._processor_lock:
                method = getattr(self._processor, "reset", None)
                if method is not None:
                    method()

    def recover(self) -> bool:
        """Rebuild a degraded processor and wait for a new reverse reference.

        The caller keeps playback paused during this method.  A successful
        rebuild is deliberately not advertised as protected until the player
        has supplied a fresh callback reference.
        """
        if not self.enabled:
            return False
        started = time.monotonic()
        self.status = "RECOVERING"
        self.close()
        self._processor = None
        self._near_end_vad = None
        self._reverse_stop = threading.Event()
        self._reverse_queue = None
        self._reverse_thread = None
        self._input_remainder.clear()
        self._reverse_remainder.clear()
        self._last_reverse_at = None
        self.last_voice_activity = None
        self.recovery_count += 1
        ok = self._start_processor(initial=False)
        self.last_recovery_ms = int((time.monotonic() - started) * 1000)
        if not ok:
            self.status = "FAILED"
        logger.info("audio.processing_recovery ok=%s count=%d elapsed_ms=%d error=%s",
                    ok, self.recovery_count, self.last_recovery_ms, self.error)
        return ok

    def close(self) -> None:
        self._reverse_stop.set()
        if self._reverse_queue is not None:
            try:
                self._reverse_queue.put_nowait(None)
            except queue.Full:
                pass
        if self._reverse_thread is not None and self._reverse_thread is not threading.current_thread():
            self._reverse_thread.join(timeout=1)
        self._reverse_thread = None

    def _degrade(self, exc: Exception) -> None:
        self.error = str(exc) or "audio processing failed"
        self.status = "DEGRADED"
        logger.warning("audio.processing_degraded error=%s", self.error)

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable

from .devices import resolve_device

logger = logging.getLogger(__name__)


class AudioPlayerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaybackSnapshot:
    """Immutable playback state captured before a generation is stopped."""

    key: str | None
    enqueued_ms: int
    played_ms: int
    buffered_ms: int
    output_estimated_ms: int
    started: bool
    captured_at: float
    precision: str = "callback"
    device_output_time: float | None = None

    @property
    def remaining_ms(self) -> int:
        return max(0, self.enqueued_ms - self.output_estimated_ms)

    def to_dict(self) -> dict[str, int | bool | str | None]:
        return {
            "key": self.key,
            "enqueued_ms": self.enqueued_ms,
            "played_ms": self.played_ms,
            "buffered_ms": self.buffered_ms,
            "output_estimated_ms": self.output_estimated_ms,
            "remaining_ms": self.remaining_ms,
            "started": self.started,
            "captured_at": self.captured_at,
            "precision": self.precision,
            "device_output_time": self.device_output_time,
        }


class PcmPlayer:
    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        output_device: str | int | None,
        on_playback_started: Callable[[str], None] | None = None,
        device_latency_ms: int = 0,
        on_audio_rendered: Callable[[bytes], None] | None = None,
        on_audio_frame: Callable[[bytes], None] | None = None,  # on_audio_frame（实际输出采样），独立于回声消除。
        playback_permission: Callable[[], bool] | None = None,
        on_policy_blocked: Callable[[str], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.output_device = output_device
        self.on_playback_started = on_playback_started
        self.on_audio_rendered = on_audio_rendered
        self.on_audio_frame = on_audio_frame
        self.playback_permission = playback_permission  # playback_permission（播放器出口策略）。
        self.on_policy_blocked = on_policy_blocked  # on_policy_blocked（被策略阻止时通知事务所有者）。
        self._policy_blocked_key: str | None = None
        self.device_latency_ms = max(0, int(device_latency_ms))
        # The callback can consume only part of a TTS chunk.  A queue.Queue
        # cannot put that remainder at the front, so using it directly would
        # reorder audio as ``chunk-1 prefix, chunk-2, chunk-1 remainder``.
        # Keep a deque and append partial chunks back with appendleft().
        self._chunks: deque[bytes] = deque()
        self._queue_lock = threading.Lock()
        self._stream: Any = None
        self._active_turn: str | None = None
        self._started_for_turn: str | None = None
        self._buffered_bytes = 0
        self._enqueued_bytes = 0
        self._played_bytes = 0
        self._last_callback_played_bytes = 0
        self._allowed_key: str | None = None
        self._held_key: str | None = None
        self._paused_key: str | None = None
        self._gated = False
        from .wake_cue import make_wake_cue
        self._wake_cue_pcm = make_wake_cue(self.sample_rate, self.channels)  # 缓存唤醒音频。
        self._last_callback_at: float | None = None
        self._last_device_output_time: float | None = None
        self._render_points: deque[tuple[float, int]] = deque(maxlen=256)
        self._last_stop_snapshot: PlaybackSnapshot | None = None
        self._resolved_device: str | int | None = None
        self._last_callback_status: str | None = None
        self._first_rendered_at: float | None = None
        self._first_rendered_key: str | None = None
        self._stream_buffer_key: str | None = None
        self._stream_start_bytes = 0  # 首次播放前的连续音频储备。
        self._stream_resume_bytes = 0  # 欠载后恢复播放所需的连续音频储备。
        self._stream_waiting = False
        self._stream_complete = False
        self._underrun_callbacks = 0  # underrun_callbacks（已起播后的欠载回调数）。
        self._underrun_ms = 0.0  # underrun_ms（已起播后因欠载静音的总毫秒）。
        from ..resources import registry
        registry.observe(self, "playback_pcm", lambda value: value._buffered_bytes)

    @property
    def diagnostics(self) -> dict[str, int | float | str | None]:
        """Playback facts that distinguish provider PCM from device submission."""

        with self._queue_lock:
            bytes_per_second = self.sample_rate * self.channels * 2
            buffered_ms = int(self._buffered_bytes * 1000 / bytes_per_second) if bytes_per_second else 0
            return {
                "configured_device": self.output_device,
                "resolved_device": self._resolved_device,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "active_key": self._active_turn,
                "held_key": self._held_key,
                "first_rendered_key": self._first_rendered_key,
                "first_rendered_at": self._first_rendered_at,
                "callback_status": self._last_callback_status,
                "buffered_ms": buffered_ms,
                "underrun_callbacks": self._underrun_callbacks,
                "underrun_ms": round(self._underrun_ms, 1),
            }

    def allow(self, key: str) -> None:
        with self._queue_lock:
            self._render_points.clear()
            self._clear_unlocked()
            self._gated = True
            self._allowed_key = key

    def hold(self, key: str) -> bool:
        """Atomically pause an unstarted playback generation without clearing it.

        The audio callback runs on another thread. Holding under the same queue
        lock makes the check against ``_started_for_turn`` and the callback's
        consumption decision one atomic boundary. PCM already admitted before
        the hold remains in the queue; the speech pipeline stops pulling
        additional provider PCM until ``resume``.
        """
        if not key:
            return False
        with self._queue_lock:
            if self._started_for_turn == key:
                return False
            if self._active_turn not in (None, key):
                return False
            self._gated = True
            self._allowed_key = key
            self._held_key = key
            return True

    def configure_stream_buffer(self, key: str, *, startup_ms: int, resume_ms: int) -> None:
        """configure_stream_buffer（流式播放储备）：合成抖动不直接变成逐块断音。"""
        with self._queue_lock:
            if self._allowed_key != key:
                return
            if self._active_turn is None:
                self._active_turn = key
            self._stream_buffer_key = key
            bytes_per_ms = self.sample_rate * self.channels * 2 / 1000
            self._stream_start_bytes = int(max(0, startup_ms) * bytes_per_ms)
            self._stream_resume_bytes = int(max(0, resume_ms) * bytes_per_ms)
            self._stream_waiting = True
            self._stream_complete = False

    def finish_stream(self, key: str) -> None:
        """finish_stream（输入音频结束）：不足储备的短回答及末尾也必须完整播放。"""
        with self._queue_lock:
            if self._stream_buffer_key == key:
                self._stream_complete = True

    def resume(self, key: str) -> bool:
        """Release a held playback generation without resetting its queue."""
        if not key:
            return False
        with self._queue_lock:
            if self._held_key == key:
                self._held_key = None
            elif self._paused_key == key:
                self._paused_key = None
            else:
                return False
            self._gated = True
            self._allowed_key = key
            return True

    def pause(self, key: str) -> bool:
        """Pause an already audible generation without discarding queued PCM."""
        if not key:
            return False
        with self._queue_lock:
            if self._active_turn != key:
                return False
            self._paused_key = key
            return True

    def block(self) -> bool:
        """Atomically close playback and report whether any audio already played."""
        snapshot = self.stop_and_snapshot()
        return snapshot.started

    def stop_and_snapshot(self, key: str | None = None) -> PlaybackSnapshot:
        """Stop one generation and return its state before clearing the queue.

        The check and clear happen under the same lock, so a late provider
        callback cannot enqueue audio between the snapshot and the gate.
        """

        with self._queue_lock:
            if key is not None and self._active_turn not in (None, key):
                if self._held_key == key:
                    self._held_key = None
                snapshot = self._empty_snapshot_unlocked(key)
                self._last_stop_snapshot = snapshot
                return snapshot
            snapshot = self._snapshot_unlocked()
            self._gated = True
            self._allowed_key = None
            self._held_key = None
            self._paused_key = None
            self._clear_unlocked()
            self._last_stop_snapshot = snapshot
            return snapshot

    @property
    def last_stop_snapshot(self) -> PlaybackSnapshot | None:
        with self._queue_lock:
            return self._last_stop_snapshot

    def has_started(self, key: str) -> bool:
        with self._queue_lock:
            return self._started_for_turn == key

    @property
    def buffered_ms(self) -> int:
        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            buffered_bytes = self._buffered_bytes
        return int(buffered_bytes * 1000 / bytes_per_second) if bytes_per_second else 0

    @property
    def enqueued_ms(self) -> int:
        """Total PCM accepted for the active playback key, in milliseconds."""

        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            value = self._enqueued_bytes
        return int(value * 1000 / bytes_per_second) if bytes_per_second else 0

    @property
    def played_ms(self) -> int:
        """PCM consumed by the PortAudio callback for the active key."""

        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            value = self._played_bytes
        return int(value * 1000 / bytes_per_second) if bytes_per_second else 0

    def progress(self, key: str | None = None) -> dict[str, int | bool | str | None]:
        """Return a thread-safe snapshot of the active playback queue.

        ``buffered_ms`` is the audio still held by the player.  ``played_ms``
        is based on bytes copied by the audio callback, which is the closest
        local signal to the Windows output device without adding another
        playback backend.  A key mismatch returns an empty snapshot so a
        previous turn cannot be mistaken for the current one.
        """

        bytes_per_second = self.sample_rate * self.channels * 2
        with self._queue_lock:
            active = self._active_turn
            if key is not None and active != key:
                return {
                    "key": active,
                    "enqueued_ms": 0,
                    "played_ms": 0,
                    "buffered_ms": 0,
                    "started": False,
                    "output_estimated_ms": 0,
                    "device_output_time": None,
                }
            enqueued = self._enqueued_bytes
            played = self._played_bytes
            buffered = self._buffered_bytes
            started = self._started_for_turn == active and active is not None
            output_estimated = self._output_estimated_bytes_unlocked(played)
            device_output_time = self._last_device_output_time
        if not bytes_per_second:
            enqueued_ms = played_ms = buffered_ms = 0
        else:
            enqueued_ms = int(enqueued * 1000 / bytes_per_second)
            played_ms = int(played * 1000 / bytes_per_second)
            buffered_ms = int(buffered * 1000 / bytes_per_second)
        return {
            "key": active,
            "enqueued_ms": enqueued_ms,
            "played_ms": played_ms,
            "buffered_ms": buffered_ms,
            "started": started,
            "output_estimated_ms": int(output_estimated * 1000 / bytes_per_second) if bytes_per_second else 0,
            "device_output_time": device_output_time,
        }

    def _output_estimated_bytes_unlocked(self, played: int) -> int:
        if not self.device_latency_ms or self._last_callback_at is None:
            return played
        # Callback consumption means that bytes have been submitted to the
        # device, not that they have reached the listener.  Keep a conservative
        # lag for the configured hardware tail and let wall-clock time drain
        # it even after the PCM queue becomes empty.
        bytes_per_ms = self.sample_rate * self.channels * 2 / 1000
        # Each callback submits a new buffer, so while callbacks continue the
        # device position trails the latest submitted position by the full
        # configured latency.  Once callbacks stop, wall-clock time advances
        # that position toward the submitted end.  Keeping the anchor at the
        # latest callback avoids subtracting ``latency - callback_interval``
        # and overstating what has reached the device on every callback.
        elapsed_ms = max(0.0, (time.monotonic() - self._last_callback_at) * 1000)
        submitted = self._last_callback_played_bytes or played
        output = submitted - self.device_latency_ms * bytes_per_ms + elapsed_ms * bytes_per_ms
        return max(0, min(self._enqueued_bytes, int(output)))

    def _empty_snapshot_unlocked(self, key: str | None) -> PlaybackSnapshot:
        return PlaybackSnapshot(key, 0, 0, 0, 0, False, time.monotonic(), "unknown", None)

    def _snapshot_unlocked(self) -> PlaybackSnapshot:
        bps = self.sample_rate * self.channels * 2
        if not bps:
            return self._empty_snapshot_unlocked(self._active_turn)
        output = self._output_estimated_bytes_unlocked(self._played_bytes)
        return PlaybackSnapshot(
            key=self._active_turn,
            enqueued_ms=int(self._enqueued_bytes * 1000 / bps),
            played_ms=int(self._played_bytes * 1000 / bps),
            buffered_ms=int(self._buffered_bytes * 1000 / bps),
            output_estimated_ms=int(output * 1000 / bps),
            started=self._started_for_turn == self._active_turn and self._active_turn is not None,
            captured_at=time.monotonic(),
            precision="device_time" if self._last_device_output_time is not None else "callback",
            device_output_time=self._last_device_output_time,
        )

    def output_position_at(self, captured_at: float) -> int | None:
        """Estimate the callback-consumed position at a monotonic timestamp."""

        with self._queue_lock:
            if not self._render_points:
                return None
            points = list(self._render_points)
            latency_ms = self.device_latency_ms
            bytes_per_ms = self.sample_rate * self.channels * 2 / 1000

        def device_position(value: float, reference_time: float) -> int:
            if not latency_ms or bytes_per_ms <= 0:
                return max(0, int(value * 1000 / (self.sample_rate * self.channels * 2)))
            elapsed_ms = max(0.0, (captured_at - reference_time) * 1000)
            remaining_latency_ms = max(0.0, latency_ms - elapsed_ms)
            value = max(0.0, value - remaining_latency_ms * bytes_per_ms)
            return max(0, int(value * 1000 / (self.sample_rate * self.channels * 2)))

        if captured_at <= points[0][0]:
            return device_position(points[0][1], points[0][0])
        if captured_at >= points[-1][0]:
            return device_position(points[-1][1], points[-1][0])
        for (left_time, left_bytes), (right_time, right_bytes) in zip(points, points[1:]):
            if left_time <= captured_at <= right_time:
                span = right_time - left_time
                ratio = (captured_at - left_time) / span if span > 0 else 1.0
                value = left_bytes + (right_bytes - left_bytes) * ratio
                return device_position(value, left_time)
        return None

    @property
    def running(self) -> bool:
        return self._stream is not None

    def _callback(self, outdata: Any, frames: int, _time: Any, _status: Any) -> None:
        target = memoryview(outdata).cast("B")
        target[:] = b"\x00" * len(target)
        remaining = len(target)
        offset = 0
        playback_turn: str | None = None
        first_rendered_turn: str | None = None
        blocked_key: str | None = None
        permitted = self.playback_permission is None or self.playback_permission()

        if _status:
            status_text = str(_status)
            with self._queue_lock:
                if status_text != self._last_callback_status:
                    self._last_callback_status = status_text
                    logger.warning("audio.player_callback_status status=%s", status_text)

        with self._queue_lock:
            if not permitted and self._active_turn is not None and self._policy_blocked_key != self._active_turn:
                self._policy_blocked_key = self._active_turn
                blocked_key = self._active_turn
            callback_at = time.monotonic()
            device_time = getattr(_time, "outputBufferDacTime", None)
            if isinstance(device_time, (int, float)):
                self._last_device_output_time = float(device_time)
            can_play = (self._held_key != self._active_turn and self._paused_key != self._active_turn
                        and self._policy_blocked_key != self._active_turn)
            if can_play and self._stream_buffer_key == self._active_turn and not self._stream_complete:
                started = self._started_for_turn == self._active_turn and self._active_turn is not None
                if self._buffered_bytes < len(target):
                    self._stream_waiting = True
                threshold = self._stream_resume_bytes if started else self._stream_start_bytes
                if self._stream_waiting and self._buffered_bytes < threshold:
                    can_play = False
                    if started:
                        self._underrun_callbacks += 1
                        self._underrun_ms += frames * 1000 / self.sample_rate
                else:
                    self._stream_waiting = False
            if can_play:
                while remaining > 0 and self._chunks:
                    chunk = self._chunks.popleft()
                    take = min(remaining, len(chunk))
                    target[offset : offset + take] = chunk[:take]
                    offset += take
                    remaining -= take
                    self._buffered_bytes = max(0, self._buffered_bytes - take)
                    self._played_bytes += take
                    if take < len(chunk):
                        # Preserve exact PCM order for the next callback.
                        self._chunks.appendleft(chunk[take:])
                        break

            if offset > 0 and self._active_turn and self._started_for_turn != self._active_turn:
                self._started_for_turn = self._active_turn
                playback_turn = self._active_turn
            if offset > 0 and self._active_turn and self._first_rendered_key != self._active_turn:
                self._first_rendered_key = self._active_turn
                self._first_rendered_at = callback_at
                first_rendered_turn = self._active_turn
            if offset > 0:
                # Keep the wall-clock anchor at the last non-silent buffer
                # submitted to the device.  PortAudio continues invoking the
                # callback during an underrun; moving this anchor on every
                # silent callback would make the estimated hardware tail
                # stop advancing and ``wait_until_output_complete`` could
                # wait forever after the PCM queue became empty.
                self._last_callback_at = callback_at
                self._render_points.append((callback_at, self._played_bytes))
            self._last_callback_played_bytes = self._played_bytes

        if blocked_key and self.on_policy_blocked is not None:
            self.on_policy_blocked(blocked_key)

        if self.on_audio_frame:
            self.on_audio_frame(bytes(target))

        if self.on_audio_rendered and len(target) > 0:
            # The callback receives the complete buffer submitted to the
            # device, including zero-filled underrun samples.  AEC needs
            # those real silence positions to keep its reverse stream aligned
            # with the microphone clock; feeding only non-zero PCM would
            # compress time whenever the player underruns.
            try:
                self.on_audio_rendered(bytes(target))
            except Exception:
                logger.exception("audio.player_render_reference_failed")
        if playback_turn and self.on_playback_started:
            self.on_playback_started(playback_turn)
        if first_rendered_turn:
            logger.info(
                "audio.player_first_pcm_submitted key=%s resolved_device=%s sample_rate=%d channels=%d",
                first_rendered_turn,
                self._resolved_device,
                self.sample_rate,
                self.channels,
            )

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd  # type: ignore

            device = resolve_device(self.output_device, input_device=False)
            self._resolved_device = device
            self._stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=device,
                callback=self._callback,
                blocksize=max(1, self.sample_rate // 50),
            )
            self._stream.start()
            logger.info(
                "audio.player_started device=%s resolved_device=%s sample_rate=%d channels=%d",
                self.output_device,
                device,
                self.sample_rate,
                self.channels,
            )
        except Exception as exc:
            self._stream = None
            logger.exception("audio.player_start_failed")
            raise AudioPlayerError(f"扬声器播放启动失败：{exc}") from exc

    def play_wake_cue(self, key: str) -> None:
        """播放本地提示音；调用方负责代次校验和正式播报的衔接。"""
        self.start()
        with self._queue_lock:
            if self._enqueued_bytes > self._output_estimated_bytes_unlocked(self._played_bytes):
                raise AudioPlayerError("唤醒提示音不能覆盖正在播放的音频或设备尾音。")
            self._render_points.clear()
            self._clear_unlocked()
            self._gated = True
            self._allowed_key = key
        self.enqueue(key, self._wake_cue_pcm)

    def enqueue(self, turn_id: str, pcm: bytes) -> None:
        if not pcm:
            return
        with self._queue_lock:
            if self._gated and turn_id != self._allowed_key:
                return
            if self._active_turn != turn_id:
                held_for_key = self._held_key == turn_id
                self._render_points.clear()
                self._clear_unlocked()
                self._active_turn = turn_id
                self._started_for_turn = None
                if held_for_key:
                    self._held_key = turn_id
            if not self._render_points:
                # Establish a zero-output anchor before the first PortAudio
                # callback.  An interruption can begin after enqueue but
                # before the callback runs; mapping that onset to the first
                # non-zero render point would overstate the heard prefix.
                self._render_points.append((time.monotonic(), self._played_bytes))
            self._chunks.append(bytes(pcm))
            self._buffered_bytes += len(pcm)
            self._enqueued_bytes += len(pcm)
        logger.debug("audio.player_enqueue turn_id=%s bytes=%d buffered_ms=%d", turn_id, len(pcm), self.buffered_ms)

    def clear(self, turn_id: str | None = None) -> None:
        with self._queue_lock:
            if turn_id is not None and self._active_turn not in (None, turn_id):
                return
            self._clear_unlocked()
        logger.info("audio.player_cleared turn_id=%s", turn_id)

    def _clear_unlocked(self) -> None:
        self._chunks.clear()
        self._stream_buffer_key = None
        self._stream_waiting = False
        self._stream_complete = False
        self._underrun_callbacks = 0
        self._underrun_ms = 0.0
        self._buffered_bytes = 0
        self._enqueued_bytes = 0
        self._played_bytes = 0
        self._last_callback_played_bytes = 0
        self._active_turn = None
        self._started_for_turn = None
        self._last_callback_at = None
        self._last_device_output_time = None
        self._held_key = None
        self._paused_key = None

    async def wait_until_empty(self, timeout_seconds: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while self.buffered_ms > 0 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        if self.buffered_ms > 0:
            raise TimeoutError("PCM playback did not drain before its deadline")
        with self._queue_lock:
            key, enqueued, played = self._active_turn, self._enqueued_bytes, self._played_bytes
        logger.info("audio.player_drained key=%s enqueued_bytes=%d played_bytes=%d", key, enqueued, played)

    async def wait_until_output_complete(self, timeout_seconds: float = 10.0) -> None:
        """Wait for callback data and configured device tail to be output."""
        # Keep ``wait_until_empty`` as the compatibility seam used by older
        # providers/tests, then account for the hardware tail when one is
        # configured.  The queue becoming empty alone is still not treated as
        # the device having finished.
        await self.wait_until_empty(timeout_seconds)
        if not self.device_latency_ms:
            return
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            with self._queue_lock:
                remaining = self._enqueued_bytes - self._output_estimated_bytes_unlocked(self._played_bytes)
            if remaining <= 0:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("PCM playback did not reach the estimated device output position")

    def stop(self) -> None:
        self.clear()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        logger.info("audio.player_stopped")

    def reclock(self, sample_rate: int, *, on_rate_changed: Callable[[int], None] | None = None) -> bool:
        """reclock（原位置换设备时钟）：改变下行采样率但保留播放器实例。

        会话控制器、事务输出与语音管线在初始化时各自持有播放器引用，替换实例
        会让它们指向已停止的旧对象（音频入旧队列、进度读旧计数）。本方法只重开
        设备流，实例身份不变，因此不需要重新接线。

        队列里的字节按旧时钟采集，换时钟后无法继续按正确速度播放，因此会清空；
        调用方须先确认播放器空闲。未运行中的播放器只更新采样率，下次 start 生效。
        """
        rate = int(sample_rate)
        if rate <= 0:
            return False
        if rate == self.sample_rate:
            return False
        was_running = self._stream is not None
        if was_running:
            self.stop()
        else:
            self.clear()
        self.sample_rate = rate
        from .wake_cue import make_wake_cue
        self._wake_cue_pcm = make_wake_cue(rate, self.channels)
        if on_rate_changed is not None:
            on_rate_changed(rate)  # 在新设备回调开始前同步回声参考时钟。
        logger.info("audio.player_reclocked sample_rate=%d was_running=%s", rate, was_running)
        if was_running:
            self.start()
        return True

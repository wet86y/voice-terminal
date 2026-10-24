"""ReplacementTransition（替换过渡）：仅播放器锁内访问的有限旧音收尾。"""
from __future__ import annotations

from dataclasses import replace
import time

import numpy as np


class ReplacementTransition:
    """隔离已撤销事务的采样，不再接收合成服务的任何追加。

    sample_rate（采样率）、channels（声道数）、max_ms（过渡硬上限）固定于受理时。
    duration_frames（正常包络帧数）、deadline（硬期限）、expires_at（有限源期限）
    共同约束 pcm（旧采样缓存）；所有写入均由播放器队列锁保护。
    """

    def __init__(self, snapshot, pcm: bytes, *, sample_rate: int, channels: int,
                 max_ms: int, started_at: float):
        self.initial = snapshot  # initial（撤销前的不可变播放快照）。
        self.old_key = snapshot.key  # old_key（原播放键）。
        self.successor_key = None  # successor_key（获准衔接的后继播放键）。
        self.sample_rate, self.channels = sample_rate, channels
        self.started_at = started_at  # started_at（取消受理单调时钟）。
        self.max_ms = min(600, max(1, int(max_ms)))
        self.duration_frames = max(1, round(min(500, self.max_ms) * sample_rate / 1000))
        self.deadline = started_at + self.max_ms / 1000
        self.pcm = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels)
        self.expires_at = min(self.deadline, started_at + min(len(self.pcm), self.duration_frames) / sample_rate)
        self.position = 0  # position（实际输出的旧采样帧数）。
        self.early_position = None  # early_position（提前收尾的包络位置）。
        self.early_gain = 0.0  # early_gain（提前收尾起始振幅）。
        self.early_frames = max(1, round(sample_rate * .03))
        self.new_frames = 0  # new_frames（后继淡入已输出帧数）。
        self.finished_at = None  # finished_at（旧音软件出口归零时间）。
        self.last_rendered_at = None  # last_rendered_at（最后旧音提交时间）。
        self.reason = None  # reason（旧音结束原因）。
        self.events = {"cancel_accepted": started_at}  # events（关联时序，不含正文）。
        if not len(self.pcm):
            self.finish("no_remaining_audio", started_at)

    @property
    def done(self):
        return self.finished_at is not None

    def gain(self, position):
        fraction = np.asarray(position, dtype=np.float64) / self.duration_frames
        return np.maximum(0, np.where(fraction < .6,
                                     1 - .7 * fraction / .6,
                                     .3 * (1 - fraction) / .4))

    def finish(self, reason: str, now: float):
        if not self.done:
            self.reason, self.finished_at = reason, now
            self.events["old_audio_silenced"] = now
            self.pcm = np.empty((0, self.channels), dtype="<i2")  # 完成后释放底层旧音缓存。

    def render(self, frames: int, *, now: float, successor_ready: bool) -> bytes:
        if self.done:
            return b""
        elapsed = max(self.position, int(max(0, now - self.started_at) * self.sample_rate))
        if now >= self.expires_at or elapsed >= self.duration_frames:
            self.finish("deadline" if now >= self.deadline else "envelope_complete", now)
            return b""
        if successor_ready and self.early_position is None:
            self.events["new_audio_ready"] = now
            self.early_position = elapsed
            self.early_gain = float(self.gain(elapsed))
        count = min(frames, len(self.pcm) - self.position, self.duration_frames - elapsed,
                    max(0, int((self.deadline - now) * self.sample_rate + 1e-6)))
        if self.early_position is not None:
            count = min(count, max(0, self.early_frames - (elapsed - self.early_position)))
        if count <= 0:
            self.finish("early_handoff" if self.early_position is not None else "source_exhausted", now)
            return b""
        positions = elapsed + np.arange(count, dtype=np.float64)
        if self.early_position is None:
            gains = self.gain(positions)
        else:
            gains = self.early_gain * np.maximum(0, 1 - (positions - self.early_position) / self.early_frames)
        samples = self.pcm[self.position:self.position + count].astype(np.float64)
        rendered = np.clip(np.rint(samples * gains[:, None]), -32768, 32767).astype("<i2").tobytes()
        self.events.setdefault("first_attenuated_audio", now)
        self.position += count
        self.last_rendered_at = now + count / self.sample_rate
        end = elapsed + count
        if (self.position >= len(self.pcm) or end >= self.duration_frames
                or self.early_position is not None and end >= self.early_position + self.early_frames):
            self.finish("early_handoff" if self.early_position is not None else
                        "envelope_complete" if end >= self.duration_frames else "source_exhausted",
                        now + count / self.sample_rate)
        return rendered

    def fade_in(self, pcm: bytes, *, now: float) -> bytes:
        self.events.setdefault("new_audio_ready", now)
        self.events.setdefault("new_audio_first_output", now)
        complete_bytes = len(pcm) // (self.channels * 2) * self.channels * 2
        samples = np.frombuffer(pcm[:complete_bytes], dtype="<i2").reshape(-1, self.channels)
        if self.new_frames >= self.early_frames:
            return pcm
        gains = np.minimum(1, (self.new_frames + np.arange(len(samples))) / self.early_frames)
        self.new_frames += len(samples)
        return (np.clip(np.rint(samples * gains[:, None]), -32768, 32767).astype("<i2").tobytes()
                + pcm[complete_bytes:])

    def snapshot(self, *, device_latency_ms: int, now: float | None = None):
        now = time.monotonic() if now is None else now
        rendered_ms = round(self.position * 1000 / self.sample_rate)
        played = self.initial.played_ms + rendered_ms
        # 最后一次提交之后继续排空硬件尾音；不将静音回调当作新的旧音提交。
        anchor = self.last_rendered_at or self.started_at
        lag = max(0, device_latency_ms - max(0, now - anchor) * 1000)
        estimated = max(self.initial.output_estimated_ms, int(played - lag))
        return replace(self.initial, played_ms=played, buffered_ms=0,
                       output_estimated_ms=min(played, estimated), captured_at=now)

    @property
    def attenuated_range_ms(self):
        """attenuated_range_ms（不能确认完整听到的旧源音频区间）。"""
        return (self.initial.played_ms,
                self.initial.played_ms + round(self.position * 1000 / self.sample_rate))

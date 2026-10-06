"""从实际 PCM（脉冲编码音频）采样计算界面的五频段音量。"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from ..core.event_bus import EventBus
from ..core.events import event
from ..core.state import RuntimeState, TerminalState


def pcm_levels(pcm: bytes, sample_rate: int, channels: int = 1) -> list[float]:
    """五频段 RMS（均方根振幅），归一化到 0–1；静音始终为零。"""
    import numpy as np

    samples = np.frombuffer(pcm[:len(pcm) // (2 * channels) * (2 * channels)], dtype="<i2")
    if samples.size < channels * 8:
        return [0.0] * 5
    mono = samples.astype(np.float32).reshape(-1, channels).mean(axis=1) / 32768.0
    spectrum = np.fft.rfft(mono)
    frequencies = np.fft.rfftfreq(mono.size, 1 / sample_rate)
    power = abs(spectrum) ** 2 / mono.size ** 2
    power[1:-1] *= 2
    edges = (0, 250, 500, 1000, 2000, sample_rate / 2 + 1)
    result = []
    for low, high in zip(edges, edges[1:]):
        rms = float(np.sqrt(power[(frequencies >= low) & (frequencies < high)].sum()))
        result.append(round(min(1.0, max(0.0, (20 * float(np.log10(max(rms, 1e-9))) + 60) / 48)), 4))
    return result


class AudioLevelMeter:
    """回调仅覆盖有界的最新帧；计算和发送在事件循环中，每秒至多 20 次。"""

    def __init__(self, state: RuntimeState, bus: EventBus, *, clock: Callable[[], float] = time.monotonic,
                 playback_active: Callable[[], bool] | None = None):
        self.state, self.bus, self.clock = state, bus, clock
        # playback_active（实际播放事务）：正式业务的输入与输出状态相互独立。
        self.playback_active = playback_active or (lambda: self.state.state == TerminalState.SPEAKING)
        # frames（最新采样）：来源、音频轮代、采集时间、采样率、声道数及 PCM。
        self.frames: dict[str, tuple[int, float, int, int, bytes]] = {}
        self.task: asyncio.Task | None = None
        self._last: tuple[int, str, list[float]] | None = None

    def feed(self, source: str, pcm: bytes, sample_rate: int, channels: int = 1) -> None:
        self.frames[source] = (self.state.audio_generation, self.clock(), sample_rate, channels,
                               pcm[-sample_rate * channels * 2 // 5:])

    async def sample(self) -> None:
        generation = self.state.audio_generation
        playing = self.playback_active() and self.state.state not in {
            TerminalState.STARTING, TerminalState.ERROR, TerminalState.INTERRUPTING}
        source = "playback" if playing else "input"
        enabled = playing or (
            self.state.state == TerminalState.LISTENING and self.state.listening)
        frame = self.frames.get(source)
        levels = [0.0] * 5
        if enabled and frame and frame[0] == generation and self.clock() - frame[1] <= 0.3:
            levels = pcm_levels(frame[4], frame[2], frame[3])
        current = (generation, source, levels)
        # 有效帧即使音量相同也继续发送，前端才能区分稳态声音与采样中断。
        if (enabled and frame and frame[0] == generation and self.clock() - frame[1] <= 0.3) or current != self._last:
            self._last = current
            await self.bus.publish(event("audio.levels", source=source, levels=levels, generation=generation))

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="actual-audio-levels")

    async def _run(self) -> None:
        while True:
            await self.sample()
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.frames.clear()

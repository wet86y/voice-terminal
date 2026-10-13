"""本地唤醒提示音：无需文件或网络的柔和双音 PCM（脉冲编码音频）。"""
from array import array
import math
import sys


def make_wake_cue(sample_rate: int, channels: int) -> bytes:
    """生成 180 毫秒、峰值不超过 15% 的下降双音。"""
    samples = array("h")
    for index in range(round(sample_rate * 0.180)):
        seconds = index / sample_rate
        frequency = 880 if seconds < 0.085 else 660
        local = seconds if seconds < 0.085 else seconds - 0.085
        duration = 0.085 if seconds < 0.085 else 0.095
        envelope = math.sin(math.pi * min(1.0, local / duration)) ** 2
        value = round(32767 * 0.15 * envelope * math.sin(2 * math.pi * frequency * local))
        samples.extend([value] * channels)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()

"""PCM allocation（音频分配）：一次原生调用的转换缓存与明确输入格式。"""
from contextlib import contextmanager
from contextvars import ContextVar

from ..resources import ResourcePolicy, registry

_current = ContextVar("audio_allocation", default=None)


class AudioAllocation:
    """相同样本范围复用转换；不同采样率、幅值约定和精度不得命中同一缓存。"""
    def __init__(self, pcm, sample_rate=16000):
        if len(pcm) % 2 or type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError("音频分配需要完整16位样本和有效采样率")
        self.pcm, self.sample_rate = pcm, sample_rate
        self.converted = {}
        registry.observe(self, "audio_conversions", lambda value: sum(a.nbytes for a in value.converted.values()))

    def samples(self, *, sample_rate, precision, amplitude, start=0, end=None):
        import numpy as np
        end = len(self.pcm) // 2 if end is None else end
        dtype = np.dtype(precision)
        if (sample_rate != self.sample_rate or dtype not in (np.dtype("float32"), np.dtype("float64"))
                or amplitude not in ("int16", "normalized")
                or type(start) is not int or type(end) is not int
                or not 0 <= start <= end <= len(self.pcm) // 2):
            raise ValueError("转换采样率、精度、幅值或范围无效；重采样必须走既有前端")
        key = (sample_rate, dtype.str, amplitude, start, end)
        if key not in self.converted:
            values = np.frombuffer(self.pcm, dtype="<i2", count=end-start, offset=start*2).astype(dtype)
            if amplitude == "normalized":
                values = values / 32768
            self.converted[key] = values
        return self.converted[key]

    def close(self):
        self.converted.clear()


@contextmanager
def conversion_scope(pcm, sample_rate=16000, *, enabled=None):
    """作用域必须覆盖原生调用实际执行期；默认配置保持既有转换路径。"""
    enabled = ResourcePolicy.load().shared_audio if enabled is None else enabled
    if not enabled:
        yield
        return
    allocation = AudioAllocation(pcm, sample_rate)
    token = _current.set(allocation)
    try:
        yield
    finally:
        _current.reset(token)
        allocation.close()


def pcm_samples(pcm, *, sample_rate=16000, precision="float32", amplitude="normalized"):
    if amplitude not in ("int16", "normalized"):
        raise ValueError("音频幅值约定必须明确")
    allocation = _current.get()
    if allocation is not None and allocation.pcm is pcm:
        return allocation.samples(sample_rate=sample_rate, precision=precision, amplitude=amplitude)
    import numpy as np
    values = np.frombuffer(pcm, dtype="<i2").astype(precision)
    return values / 32768 if amplitude == "normalized" else values

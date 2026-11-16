"""Device clock mapping（设备时钟映射）：根据双向测量估计单调时钟偏移与误差。"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ClockEstimate:
    """ClockEstimate（时钟估计）：设备秒数加偏移得到主机秒数。"""

    offset_seconds: float
    uncertainty_seconds: float
    measured_at: float


class DeviceClockMapper:
    """DeviceClockMapper（设备时钟映射器）：偏向低往返延迟的有限样本。

    实例仅属于一个设备连接；重连或设备重启后必须创建新实例。
    不使用消息到达时间代替设备采集时间。
    """

    def __init__(self, *, max_samples: int = 16, max_age_seconds: float = 30.0,
                 max_drift_ppm: float = 100.0) -> None:
        if type(max_samples) is not int or not 1 <= max_samples <= 256:
            raise ValueError("时钟样本数必须为 1 至 256。")
        if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float))
                or not math.isfinite(max_age_seconds) or max_age_seconds <= 0):
            raise ValueError("时钟样本有效期必须为正数。")
        self._samples: deque[ClockEstimate] = deque(maxlen=max_samples)
        self._max_age = float(max_age_seconds)
        if (isinstance(max_drift_ppm, bool) or not isinstance(max_drift_ppm, (int, float))
                or not math.isfinite(max_drift_ppm) or not 0 <= max_drift_ppm <= 10000):
            raise ValueError("时钟漂移上限无效。")
        self._max_drift = float(max_drift_ppm) / 1_000_000
        self._last_host_receive: float | None = None
        self._last_device_send: int | None = None

    def observe(self, *, host_send: float, device_receive_us: int,
                device_send_us: int, host_receive: float) -> ClockEstimate:
        """observe（记录测量）：双方各自使用单调时钟，参数不包含音频正文。"""
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (host_send, host_receive))
                or any(type(v) is not int or not 0 <= v <= (1 << 53) - 1
                       for v in (device_receive_us, device_send_us))
                or host_send < 0 or host_receive < host_send or device_send_us < device_receive_us
                or (self._last_host_receive is not None and host_receive <= self._last_host_receive)
                or (self._last_device_send is not None and device_send_us <= self._last_device_send)):
            raise ValueError("设备时钟测量的时间或顺序无效。")
        device_work = (device_send_us - device_receive_us) / 1_000_000
        network_round_trip = host_receive - host_send - device_work
        if network_round_trip < -1e-6:
            raise ValueError("设备处理时间超过主机测得的往返时间。")
        offset = ((host_send - device_receive_us / 1_000_000)
                  + (host_receive - device_send_us / 1_000_000)) / 2
        estimate = ClockEstimate(offset, max(0.0, network_round_trip) / 2, float(host_receive))
        self._samples.append(estimate)
        self._last_host_receive = float(host_receive)
        self._last_device_send = device_send_us
        return estimate

    def estimate(self, *, now: float) -> ClockEstimate:
        """estimate（选取估计）：过期或不存在样本时不猜测设备时间。"""
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("主机当前时间无效。")
        valid = [sample for sample in self._samples if 0 <= now - sample.measured_at <= self._max_age]
        if not valid:
            raise ValueError("设备时钟尚未同步或测量已过期。")
        return min(valid, key=lambda sample: (sample.uncertainty_seconds, -sample.measured_at))

    def map_time(self, device_time_us: int, *, now: float) -> tuple[float, float]:
        """map_time（映射时间）：返回主机采集时间及按配置漂移上限估计的误差，单位为秒。"""
        if type(device_time_us) is not int or not 0 <= device_time_us <= (1 << 53) - 1:
            raise ValueError("设备采集时间无效。")
        estimate = self.estimate(now=now)
        mapped = device_time_us / 1_000_000 + estimate.offset_seconds
        elapsed = max(abs(mapped - estimate.measured_at), now - estimate.measured_at)
        return mapped, estimate.uncertainty_seconds + elapsed * self._max_drift

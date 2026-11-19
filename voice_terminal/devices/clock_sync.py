"""Device clock mapping（设备时钟映射）：根据双向测量估计单调时钟偏移与误差。"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from uuid import uuid4

# Windows（操作系统）的 time.monotonic 由 GetTickCount64 提供，实际步进 15.625 毫秒。
# 五轮往返测量需要亚毫秒时间差，否则同一刻度内的往返会被读成 0 或相等值；
# 媒体时钟改用 QPC（高性能计数器）并锚定回 time.monotonic 时间轴：锚点常量
# 只平移绝对值，成对差值保持亚微秒精度，因此无需改动其他主机时间比较。
_MEDIA_CLOCK_ORIGIN = time.monotonic() - time.perf_counter()

# ROUND_TRIP_TOLERANCE_SECONDS（往返抖动容忍）：设备侧统计与主机读数之间的调度抖动上限。
ROUND_TRIP_TOLERANCE_SECONDS = 0.01


def media_clock() -> float:
    """media_clock（媒体时钟）：亚微秒单调时钟，数值仍落在 time.monotonic 时间轴上。"""
    return _MEDIA_CLOCK_ORIGIN + time.perf_counter()


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
        self._last_mapped: tuple[int, float] | None = None  # _last_mapped（最大设备时刻及其主机映射）：续测不能使新采样倒退。

    def observe(self, *, host_send: float, device_receive_us: int,
                device_send_us: int, host_receive: float) -> ClockEstimate:
        """observe（记录测量）：双方各自使用单调时钟，参数不包含音频正文。

        只校验本次测量自身成立（主机收晚于发、设备发不早于收）；跨轮次序、
        与主机时钟粒度的比较都交给样本选择，不再据此拒绝整条媒体通道。
        """
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in (host_send, host_receive))
                or any(type(v) is not int or not 0 <= v <= (1 << 53) - 1
                       for v in (device_receive_us, device_send_us))
                or host_send < 0 or host_receive < host_send or device_send_us < device_receive_us):
            raise ValueError("设备时钟测量的时间或顺序无效。")
        device_work = (device_send_us - device_receive_us) / 1_000_000
        network_round_trip = host_receive - host_send - device_work
        # 调度抖动、中断延迟都可能让设备侧统计略超主机读到的区间；只拒绝超出抖动容忍的样本。
        if network_round_trip < -ROUND_TRIP_TOLERANCE_SECONDS:
            raise ValueError("设备处理时间超过主机测得的往返时间。")
        offset = ((host_send - device_receive_us / 1_000_000)
                  + (host_receive - device_send_us / 1_000_000)) / 2
        # 负残差只是容忍了测量量化误差，不代表零误差；不能让它比正常低延迟样本更优。
        uncertainty = (network_round_trip / 2 if network_round_trip >= 0
                       else ROUND_TRIP_TOLERANCE_SECONDS + abs(network_round_trip) / 2)
        estimate = ClockEstimate(offset, uncertainty, float(host_receive))
        self._samples.append(estimate)
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
        correction = 0.0
        previous = self._last_mapped
        if previous is not None and device_time_us > previous[0] and mapped <= previous[1]:
            # 只修正续测偏移造成的倒退；设备时钟/帧序倒退仍由输入流拒绝。
            corrected = previous[1] + (device_time_us - previous[0]) / 1_000_000
            correction = corrected - mapped
            mapped = corrected
        if previous is None or device_time_us > previous[0]:
            self._last_mapped = (device_time_us, mapped)
        elapsed = max(abs(mapped - estimate.measured_at), now - estimate.measured_at)
        return mapped, estimate.uncertainty_seconds + elapsed * self._max_drift + correction


class MediaClockSync:
    """MediaClockSync（媒体续测状态）：有界在途测量，迟到回执不影响音频或新样本。"""

    INTERVAL_SECONDS = 10.0  # INTERVAL_SECONDS（正常续测间隔）。
    TIMEOUT_SECONDS = 3.0  # TIMEOUT_SECONDS（单次测量等待上限）。

    def __init__(self, mapper: DeviceClockMapper) -> None:
        self.mapper = mapper  # mapper（本连接的时钟映射器）。
        self.pending: dict[str, float] = {}  # pending（测量编号到主机发出时刻）。
        self.next_request_at = 0.0  # next_request_at（下次允许续测的主机时刻）。

    def request(self, now: float, *, force: bool = False) -> str | None:
        """request（登记测量）：初始握手可强制，运行期按间隔发送。"""
        if not force and (now < self.next_request_at or self.pending):
            return None
        sync_id = uuid4().hex
        self.pending[sync_id] = now
        self.next_request_at = now + self.INTERVAL_SECONDS
        return sync_id

    def expire(self, now: float) -> list[str]:
        """expire（结束超时样本）：只丢样本，下一秒重测，不立即关闭仍有可信映射的流。"""
        expired = [key for key, sent in self.pending.items() if now - sent >= self.TIMEOUT_SECONDS]
        for key in expired:
            self.pending.pop(key, None)
        if expired:
            self.next_request_at = min(self.next_request_at, now + 1.0)
        return expired

    def observe(self, sync_id: str, *, device_receive_us: int, device_send_us: int,
                host_receive: float) -> bool:
        """observe（接纳当前测量）：已超时或重复回执不再参与估计。"""
        host_send = self.pending.pop(sync_id, None)
        if host_send is None:
            return False
        self.mapper.observe(host_send=host_send, device_receive_us=device_receive_us,
                            device_send_us=device_send_us, host_receive=host_receive)
        return True

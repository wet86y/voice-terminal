"""设备与主机的单调时钟映射及失效边界。"""
import pytest

from voice_terminal.devices.clock_sync import DeviceClockMapper


def test_clock_mapping_uses_low_latency_sample_and_tracks_uncertainty():
    mapper = DeviceClockMapper(max_drift_ppm=100)
    mapper.observe(host_send=10.0, device_receive_us=2_010_000, device_send_us=2_020_000, host_receive=10.03)
    mapper.observe(host_send=11.0, device_receive_us=3_100_000, device_send_us=3_110_000, host_receive=11.21)
    mapped, uncertainty = mapper.map_time(2_500_000, now=11.21)
    assert mapped == pytest.approx(10.5)
    assert uncertainty == pytest.approx(.010118)


def test_clock_requires_fresh_ordered_measurements():
    mapper = DeviceClockMapper(max_age_seconds=1)
    with pytest.raises(ValueError, match="尚未同步"):
        mapper.map_time(1, now=2)
    mapper.observe(host_send=10, device_receive_us=2_000_000, device_send_us=2_010_000, host_receive=10.02)
    with pytest.raises(ValueError, match="过期"):
        mapper.map_time(1, now=12)
    with pytest.raises(ValueError, match="顺序"):
        mapper.observe(host_send=11, device_receive_us=1_000_000, device_send_us=1_010_000, host_receive=11.02)
    with pytest.raises(ValueError, match="处理时间"):
        mapper.observe(host_send=11, device_receive_us=3_000_000, device_send_us=4_000_000, host_receive=11.1)

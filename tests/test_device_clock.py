"""设备与主机的单调时钟映射及失效边界。"""
import time

import pytest

from voice_terminal.devices.clock_sync import DeviceClockMapper, MediaClockSync, media_clock


def test_clock_mapping_uses_low_latency_sample_and_tracks_uncertainty():
    mapper = DeviceClockMapper(max_drift_ppm=100)
    mapper.observe(host_send=10.0, device_receive_us=2_010_000, device_send_us=2_020_000, host_receive=10.03)
    mapper.observe(host_send=11.0, device_receive_us=3_100_000, device_send_us=3_110_000, host_receive=11.21)
    mapped, uncertainty = mapper.map_time(2_500_000, now=11.21)
    assert mapped == pytest.approx(10.5)
    assert uncertainty == pytest.approx(.010118)


def test_clock_rejects_only_self_inconsistent_measurements():
    mapper = DeviceClockMapper(max_age_seconds=1)
    with pytest.raises(ValueError, match="尚未同步"):
        mapper.map_time(1, now=2)
    mapper.observe(host_send=10, device_receive_us=2_000_000, device_send_us=2_010_000, host_receive=10.02)
    with pytest.raises(ValueError, match="过期"):
        mapper.map_time(1, now=12)
    # 单次测量内部次序颠倒仍然拒绝。
    with pytest.raises(ValueError, match="顺序"):
        mapper.observe(
            host_send=11, device_receive_us=5_000_000,
            device_send_us=4_999_000, host_receive=11.02,
        )
    # 跨轮样本先后次序不再拒绝整条通道：旧样本只是不参与低延迟选择。
    mapper.observe(host_send=11, device_receive_us=1_000_000, device_send_us=1_010_000, host_receive=11.02)
    with pytest.raises(ValueError, match="处理时间"):
        mapper.observe(host_send=12, device_receive_us=3_000_000, device_send_us=4_000_000, host_receive=12.1)


def test_media_clock_keeps_the_monotonic_axis_with_sub_millisecond_steps():
    # Windows 的 time.monotonic 粒度 15.625 毫秒；媒体时钟必须仍在同一时间轴上但更精细。
    assert abs(media_clock() - time.monotonic()) < 0.05
    steps = []
    previous = media_clock()
    for _ in range(4000):
        current = media_clock()
        if current > previous:
            steps.append(current - previous)
            previous = current
        if len(steps) >= 20:
            break
    assert steps and min(steps) < 0.001


def test_jitter_inside_tolerance_is_accepted_and_oversized_device_work_is_rejected():
    """主机时钟粒度造成的零跨度往返不再拒绝；明显超过往返的设备处理仍然拒绝。"""
    mapper = DeviceClockMapper()
    host_send = 100.0
    # 同一 15.625 毫秒刻度内的往返：设备处理 1 毫秒落在抖动容忍内。
    mapper.observe(
        host_send=host_send,
        device_receive_us=1_000_000,
        device_send_us=1_001_000,
        host_receive=host_send,
    )
    with pytest.raises(ValueError, match="处理时间"):
        mapper.observe(
            host_send=host_send + 1,
            device_receive_us=2_000_000,
            device_send_us=2_050_000,
            host_receive=host_send + 1.02,
        )


def test_media_clock_completes_five_rounds_for_a_low_latency_connection():
    mapper = DeviceClockMapper()
    device_receive_us = 5_000_000
    for _ in range(5):
        host_send = media_clock()
        time.sleep(0.002)
        device_receive_us += 1_000
        host_receive = media_clock()
        mapper.observe(
            host_send=host_send,
            device_receive_us=device_receive_us,
            device_send_us=device_receive_us + 200,
            host_receive=host_receive,
        )
    estimate = mapper.estimate(now=media_clock())
    assert estimate.uncertainty_seconds >= 0
    mapped, uncertainty = mapper.map_time(device_receive_us, now=media_clock())
    assert mapped > 0 and uncertainty >= estimate.uncertainty_seconds


def test_runtime_clock_refresh_keeps_ten_minutes_of_audio_mapped_without_expiry():
    mapper = DeviceClockMapper()
    sync = MediaClockSync(mapper)
    previous = 0.0
    for step in range(61):
        now = 100.0 + step * 10
        sync_id = sync.request(now)
        assert sync_id is not None
        device_time = int((now - 90) * 1_000_000)
        assert sync.observe(sync_id, device_receive_us=device_time, device_send_us=device_time + 100,
                            host_receive=now + .002)
        mapped, uncertainty = mapper.map_time(device_time + 20_000, now=now + .02)
        assert mapped > previous and 0 < uncertainty < .01
        assert sync.request(now + 1) is None
        previous = mapped


def test_clock_timeout_retries_and_late_reply_does_not_replace_a_new_sample():
    sync = MediaClockSync(DeviceClockMapper())
    old = sync.request(100)
    assert sync.expire(103) == [old]
    assert sync.request(103.5) is None
    current = sync.request(104)
    assert current is not None
    assert not sync.observe(old, device_receive_us=1, device_send_us=2, host_receive=104.01)
    assert sync.observe(current, device_receive_us=1_000_000, device_send_us=1_000_100, host_receive=104.002)
    assert not sync.observe(current, device_receive_us=1, device_send_us=2, host_receive=104.003)


def test_negative_round_trip_keeps_uncertainty_and_cannot_outrank_a_valid_sample():
    mapper = DeviceClockMapper()
    valid = mapper.observe(host_send=100, device_receive_us=1_000_000, device_send_us=1_000_000, host_receive=100.002)
    noisy = mapper.observe(host_send=100.01, device_receive_us=2_000_000, device_send_us=2_008_000, host_receive=100.01)
    assert noisy.uncertainty_seconds >= .01
    assert mapper.estimate(now=100.02) == valid


def test_refreshed_clock_cannot_move_new_audio_backwards_and_reports_the_correction():
    mapper = DeviceClockMapper()
    mapper.observe(host_send=10, device_receive_us=1_000_000, device_send_us=1_000_000, host_receive=10.002)
    previous, _ = mapper.map_time(1_000_000, now=10.002)
    mapper.observe(host_send=10.003, device_receive_us=1_100_000, device_send_us=1_100_000, host_receive=10.003)
    current, uncertainty = mapper.map_time(1_020_000, now=10.004)
    assert current > previous and uncertainty > .09

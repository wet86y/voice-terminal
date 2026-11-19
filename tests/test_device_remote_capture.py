"""远端 PCM 采集端口的队列、预录和租约回归。"""
import asyncio
from dataclasses import replace
import pytest

from voice_terminal.devices.media_protocol import MediaFrame, MediaProtocolError
from voice_terminal.devices.pcm_stream import PcmInputStream
from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.devices.remote_capture import RemoteAudioCapture, RemoteAudioCaptureError


def _capture(*, on_audio_frame=None, restartable=False):
    registry = DeviceRegistry()
    registry.connect("device-remote", capabilities=("audio_stream",))
    lease = registry.prepare_switch("device-remote", registry.generation)
    assert registry.commit_switch(lease)
    def make_stream(stream_id):
        return PcmInputStream(stream_id=stream_id, core_session_id="0123456789abcdef0123456789abcdef",
                              lease=lease, registry=registry,
                              map_device_time=lambda value: (20.0 + value / 1_000_000, 0.004))
    stream = make_stream("stream-remote")
    return RemoteAudioCapture(stream, ring_buffer_ms=1_000, on_audio_frame=on_audio_frame,
                              restart_stream=make_stream if restartable else None), stream


def _frame(sequence: int, *, device_id: str = "device-remote") -> MediaFrame:
    samples = 320
    return MediaFrame(
        stream_id="stream-remote",
        sequence=sequence,
        start_sample=sequence * samples,
        sample_count=samples,
        device_time_us=(sequence + 1) * 20_000,
        payload=bytes([sequence & 0xFF, 0]) * samples,
        device_id=device_id,
        core_session_id="0123456789abcdef0123456789abcdef",
        connection_generation=1,
        executor_generation=1,
        interaction_generation=1,
    )


def test_remote_capture_uses_six_frame_bound_and_preserves_gap_for_controller():
    observed = []
    capture, _ = _capture(on_audio_frame=observed.append)

    async def run():
        await capture.start()
        await capture.start()  # 重复启动保持幂等，不重置有效流。

    asyncio.run(run())
    for sequence in range(7):
        accepted = capture.accept_frame(_frame(sequence))
        assert accepted.start_sample == sequence * 320

    assert capture.pending_frame_count() == 6
    assert capture.diagnostics["blocks"] == 7
    assert capture.diagnostics["bytes"] == 7 * 640
    assert capture.diagnostics["dropped_chunks"] == 1
    assert len(observed) == 7
    pending = [capture.read_pending_frame() for _ in range(6)]
    assert [frame.start_sample for frame in pending] == [320, 640, 960, 1280, 1600, 1920]
    assert pending[-1].captured_at == 20.14
    assert pending[-1].clock_uncertainty_seconds == 0.004
    assert capture.preroll(40) == bytes([5, 0]) * 320 + bytes([6, 0]) * 320


def test_remote_capture_refuses_unstarted_stopped_and_old_lease_frames():
    capture, stream = _capture()
    with pytest.raises(RemoteAudioCaptureError, match="尚未启动"):
        capture.accept_frame(_frame(0))

    asyncio.run(capture.start())
    with pytest.raises(MediaProtocolError, match="代次不匹配"):
        capture.accept_frame(_frame(0, device_id="device-other"))
    assert capture.running is False
    assert stream.closed is True
    assert capture.pending_frame_count() == 0

    with pytest.raises(RemoteAudioCaptureError, match="必须为新连接"):
        asyncio.run(capture.start())


def test_capture_recovers_gaps_and_drops_late_audio_without_ending_media():
    capture, stream = _capture()
    asyncio.run(capture.start())
    assert capture.recording_phase == "starting"
    assert capture.accept_frame(_frame(0)) is not None
    assert capture.recording_phase == "recording"
    assert capture.accept_frame(_frame(0)) is None
    assert capture.accept_frame(replace(_frame(1), device_time_us=0)) is None
    recovered = capture.accept_frame(_frame(3))
    assert recovered.discontinuity_before and recovered.start_sample == 960
    assert capture.pending_frame_count() == 1  # 整理缺口前积压帧和预录，不拼进下一段。
    assert capture.preroll(80) == _frame(3).payload
    assert capture.accept_frame(_frame(2)) is None
    assert not capture.accept_frame(_frame(4)).discontinuity_before
    assert capture.running and not stream.closed


def test_remote_capture_drops_duplicate_frame_without_duplicating_preroll():
    observed = []
    capture, stream = _capture(on_audio_frame=observed.append)
    asyncio.run(capture.start())
    frame = _frame(0)
    capture.accept_frame(frame)
    assert capture.preroll(20) == frame.payload

    assert capture.accept_frame(frame) is None
    assert capture.running and not stream.closed
    assert capture.pending_frame_count() == 1
    assert capture.preroll(20) == frame.payload
    assert observed == [frame.payload]
    assert capture.diagnostics["blocks"] == 1


def test_remote_capture_stop_clears_unconsumed_audio_and_preroll():
    capture, stream = _capture()
    asyncio.run(capture.start())
    capture.accept_frame(_frame(0))
    assert capture.preroll(20) == bytes([0, 0]) * 320

    asyncio.run(capture.stop())
    assert capture.running is False
    assert stream.closed is True
    assert capture.pending_frame_count() == 0
    assert capture.preroll(20) == bytes([0, 0]) * 320
    with pytest.raises(RemoteAudioCaptureError, match="已停止"):
        asyncio.run(capture.read_frame())


def test_remote_capture_restarts_ten_times_with_new_input_and_discards_only_its_old_frames():
    capture, original = _capture(restartable=True)

    async def run():
        retired = []
        for iteration in range(10):
            await capture.start()
            frame = replace(_frame(0), stream_id=capture.stream_id, device_time_us=(iteration + 1) * 20_000)
            for old in retired[-2:]:
                assert capture.accept_frame(old) is None
            assert capture.accept_frame(frame) is not None
            assert capture.pending_frame_count() == 1
            retired.append(frame)
            await capture.stop()
            assert capture.accept_frame(frame) is None
            assert capture.pending_frame_count() == 0
        assert len({frame.stream_id for frame in retired}) == 10
        assert original.closed
        await capture.start()
        with pytest.raises(MediaProtocolError, match="代次不匹配"):
            capture.accept_frame(replace(retired[-1], device_id="device-forged"))
        with pytest.raises(RemoteAudioCaptureError, match="必须为新连接"):
            await capture.start()

    asyncio.run(run())


def test_remote_capture_observer_failure_does_not_discard_valid_pcm():
    def fail_observer(_pcm):
        raise RuntimeError("meter unavailable")

    capture, _ = _capture(on_audio_frame=fail_observer)
    asyncio.run(capture.start())
    accepted = capture.accept_frame(_frame(0))
    assert accepted.pcm == bytes([0, 0]) * 320
    assert capture.pending_frame_count() == 1
    assert capture.diagnostics["last_error"] is None


def test_remote_capture_stop_wakes_a_blocked_core_reader():
    async def run():
        capture, _ = _capture()
        await capture.start()
        reader = asyncio.create_task(capture.read_frame())
        await asyncio.sleep(0)

        await capture.stop()

        with pytest.raises(RemoteAudioCaptureError, match="已停止"):
            await asyncio.wait_for(reader, timeout=1)

    asyncio.run(run())

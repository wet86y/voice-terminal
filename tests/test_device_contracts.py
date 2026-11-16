"""设备音频封装和交互租约的边界回归。"""
import json
import struct
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from voice_terminal.devices.media_protocol import (
    MAX_HEADER_BYTES, MAX_PAYLOAD_BYTES, MediaFrame, MediaProtocolError, decode_frame, encode_frame,
)
from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.devices.pcm_stream import PcmInputStream


def _message(header: bytes, payload: bytes = b"\0\0") -> bytes:
    return struct.pack("!I", len(header)) + header + payload


def _frame(stream, sequence, start, count, timestamp, payload):
    return MediaFrame(stream, sequence, start, count, timestamp, payload, "B", "core", 1, 1, 1)


def test_audio_frame_round_trip_preserves_binary_and_sample_clock():
    frame = _frame("stream-设备", 2, 640, 128, 123456, bytes(range(256)))
    assert decode_frame(encode_frame(frame)) == frame


def test_audio_frame_rejects_duplicate_fields_and_invalid_lengths():
    header = b'{"protocol_version":1,"protocol_version":1,"stream_id":"s","sequence":0,"start_sample":0,"sample_count":1,"device_time_us":0,"device_id":"B","core_session_id":"core","connection_generation":1,"executor_generation":1,"interaction_generation":1,"discontinuity_before":false}'
    with pytest.raises(MediaProtocolError, match="重复"):
        decode_frame(_message(header))
    for message in (b"", struct.pack("!I", MAX_HEADER_BYTES + 1), _message(b"{}", b""),
                    _message(b"{}", b"x" * (MAX_PAYLOAD_BYTES + 1))):
        with pytest.raises(MediaProtocolError):
            decode_frame(message)


@pytest.mark.parametrize("field,value", [("protocol_version", True), ("sequence", True),
                                        ("start_sample", -1), ("sample_count", 0),
                                        ("device_time_us", 1 << 53), ("stream_id", " "),
                                        ("discontinuity_before", 1)])
def test_audio_frame_rejects_invalid_metadata(field, value):
    header = {"protocol_version": 1, "stream_id": "s", "sequence": 0,
              "start_sample": 0, "sample_count": 320, "device_time_us": 0, "device_id": "B",
              "core_session_id": "core", "connection_generation": 1, "executor_generation": 1,
              "interaction_generation": 1, "discontinuity_before": False}
    header[field] = value
    with pytest.raises(MediaProtocolError):
        decode_frame(_message(json.dumps(header).encode()))


def test_switch_requires_prepare_and_rejects_late_or_disconnected_leases():
    registry = DeviceRegistry()
    a = registry.connect("A")
    b = registry.connect("B")
    first = registry.prepare_switch("A", registry.generation)
    assert not registry.accepts(first)
    assert registry.commit_switch(first)
    second = registry.prepare_switch("B", registry.generation)
    assert not registry.accepts(first)
    assert registry.disconnect("B", b.connection_generation)
    assert not registry.commit_switch(second)
    reconnected = registry.connect("B")
    assert not registry.disconnect("B", b.connection_generation)
    assert reconnected.connection_generation > b.connection_generation
    assert registry.active is None
    assert registry.disconnect("A", a.connection_generation)


def test_executor_restart_invalidates_active_and_pending_leases():
    registry = DeviceRegistry()
    connected = registry.connect("A")
    active = registry.prepare_switch("A", registry.generation)
    assert registry.commit_switch(active)
    restarted = registry.restart_executor("A", connected.connection_generation)
    assert restarted.executor_generation > connected.executor_generation
    assert not registry.accepts(active)
    pending = registry.prepare_switch("A", registry.generation)
    registry.restart_executor("A", connected.connection_generation)
    assert not registry.commit_switch(pending)


def test_competing_switches_cannot_both_obtain_the_same_generation():
    registry = DeviceRegistry()
    registry.connect("A")
    registry.connect("B")
    generation = registry.generation

    def prepare(device):
        try:
            return registry.prepare_switch(device, generation)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        candidates = list(executor.map(prepare, ("A", "B")))
    leases = [value for value in candidates if value is not None]
    assert len(leases) == 1
    assert registry.commit_switch(leases[0])
    assert registry.accepts(leases[0])


def test_switch_cannot_overtake_unsettled_switch_even_with_latest_generation():
    registry = DeviceRegistry()
    registry.connect("A")
    registry.connect("B")
    pending = registry.prepare_switch("A", registry.generation)
    with pytest.raises(ValueError, match="尚未完成"):
        registry.prepare_switch("B", registry.generation)
    assert registry.commit_switch(pending)
    next_lease = registry.prepare_switch("B", registry.generation)
    assert registry.commit_switch(next_lease)


def test_aborted_switch_never_restores_old_audio_lease_without_an_explicit_new_choice():
    registry = DeviceRegistry()
    old_connection = registry.connect("B", capabilities=("audio_stream",))
    registry.connect("A", capabilities=("audio_stream",))
    old_lease = registry.prepare_switch("B", registry.generation)
    assert registry.commit_switch(old_lease)
    old_stream = PcmInputStream(stream_id="old", core_session_id="core", lease=old_lease,
                                registry=registry, map_device_time=float)
    old_stream.accept(_frame("old", 0, 0, 320, 20_000, b"\0\0" * 320))

    pending = registry.prepare_switch("A", registry.generation)
    assert registry.active is None
    assert not registry.accepts(old_lease)
    assert registry.abort_switch(pending)
    assert registry.active is None
    with pytest.raises(MediaProtocolError, match="租约"):
        old_stream.accept(_frame("old", 1, 320, 320, 40_000, b"\0\0" * 320))
    assert old_stream.closed

    # A user must explicitly select again; the new lease uses a fresh interaction generation.
    fresh = registry.prepare_switch("B", registry.generation)
    assert fresh.connection_generation == old_connection.connection_generation
    assert fresh.interaction_generation > old_lease.interaction_generation
    assert registry.commit_switch(fresh)
    frame = replace(_frame("fresh", 0, 0, 320, 20_000, b"\0\0" * 320),
                    interaction_generation=fresh.interaction_generation)
    fresh_stream = PcmInputStream(stream_id="fresh", core_session_id="core", lease=fresh,
                                  registry=registry, map_device_time=float)
    assert fresh_stream.accept(frame).start_sample == 0


def test_pcm_stream_requires_contiguous_samples_and_new_stream_after_gap():
    registry = DeviceRegistry()
    registry.connect("B")
    lease = registry.prepare_switch("B", registry.generation)
    assert registry.commit_switch(lease)
    stream = PcmInputStream(stream_id="s", core_session_id="core", lease=lease, registry=registry,
                            map_device_time=lambda value: 100.0 + value / 1_000_000)
    first = stream.accept(_frame("s", 0, 640, 320, 20000, b"\0\0" * 320))
    assert first.start_sample == 640 and first.captured_at == 100.02
    with pytest.raises(MediaProtocolError, match="采样位置"):
        stream.accept(_frame("s", 1, 1280, 320, 40000, b"\0\0" * 320))
    with pytest.raises(MediaProtocolError, match="关闭"):
        stream.accept(_frame("s", 1, 960, 320, 40000, b"\0\0" * 320))


def test_pcm_stream_closes_immediately_when_capture_marks_a_discontinuity():
    registry = DeviceRegistry()
    registry.connect("B")
    lease = registry.prepare_switch("B", registry.generation)
    assert registry.commit_switch(lease)
    stream = PcmInputStream(stream_id="s", core_session_id="core", lease=lease, registry=registry,
                            map_device_time=float)
    frame = replace(_frame("s", 0, 0, 320, 20_000, b"\0\0" * 320), discontinuity_before=True)
    with pytest.raises(MediaProtocolError, match="重建音频流"):
        stream.accept(frame)
    assert stream.closed


def test_pcm_stream_retains_clock_mapping_uncertainty():
    registry = DeviceRegistry()
    registry.connect("B")
    lease = registry.prepare_switch("B", registry.generation)
    assert registry.commit_switch(lease)
    stream = PcmInputStream(stream_id="s", core_session_id="core", lease=lease, registry=registry,
                            map_device_time=lambda _: (10.0, .012))
    frame = stream.accept(_frame("s", 0, 0, 320, 20000, b"\0\0" * 320))
    assert frame.captured_at == 10.0 and frame.clock_uncertainty_seconds == .012
    assert stream.clock_uncertainty_seconds == .012


def test_pcm_stream_rejects_old_lease_and_unreliable_clock():
    registry = DeviceRegistry()
    connection = registry.connect("B")
    lease = registry.prepare_switch("B", registry.generation)
    assert registry.commit_switch(lease)
    stream = PcmInputStream(stream_id="s", core_session_id="core", lease=lease, registry=registry,
                            map_device_time=lambda _value: float("nan"))
    with pytest.raises(MediaProtocolError, match="时钟"):
        stream.accept(_frame("s", 0, 0, 320, 20000, b"\0\0" * 320))
    old = PcmInputStream(stream_id="old", core_session_id="core", lease=lease, registry=registry, map_device_time=float)
    registry.disconnect("B", connection.connection_generation)
    registry.connect("B")
    with pytest.raises(MediaProtocolError, match="租约"):
        old.accept(_frame("old", 0, 0, 320, 20000, b"\0\0" * 320))


@pytest.mark.parametrize("field,value", [("device_id", "C"), ("core_session_id", "old"),
                                        ("connection_generation", 2), ("executor_generation", 2),
                                        ("interaction_generation", 2), ("interaction_generation", True)])
def test_pcm_stream_rejects_other_device_core_or_generation(field, value):
    registry = DeviceRegistry()
    registry.connect("B")
    lease = registry.prepare_switch("B", registry.generation)
    assert registry.commit_switch(lease)
    stream = PcmInputStream(stream_id="s", core_session_id="core", lease=lease,
                            registry=registry, map_device_time=float)
    with pytest.raises(MediaProtocolError, match="代次不匹配"):
        stream.accept(replace(_frame("s", 0, 0, 320, 0, b"\0\0" * 320), **{field: value}))
    assert stream.closed

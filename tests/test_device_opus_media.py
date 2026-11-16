"""Signed media negotiation and Opus uplink decode（签名媒体协商及 Opus 上行解码）。"""
from __future__ import annotations

from array import array
import math
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from voice_terminal.devices.media_protocol import MediaFrame, MediaProtocolError, decode_frame, encode_frame
from voice_terminal.devices.opus_codec import OpusEncoder, find_opus_library
from voice_terminal.devices.pcm_stream import PcmInputStream
from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.server.http_api import _device_media_open_payload


def _active_lease() -> tuple[DeviceRegistry, object]:
    registry = DeviceRegistry()
    registry.connect("opus-device", capabilities=("audio_stream",))
    lease = registry.prepare_switch("opus-device", registry.generation)
    assert registry.commit_switch(lease)
    return registry, lease


def _opus_frame(sequence: int, packet: bytes, *, device_time_us: int | None = None) -> MediaFrame:
    return MediaFrame(
        stream_id="opus-stream",
        sequence=sequence,
        start_sample=sequence * 320,
        sample_count=320,
        device_time_us=(sequence + 1) * 20_000 if device_time_us is None else device_time_us,
        payload=packet,
        device_id="opus-device",
        core_session_id="0123456789abcdef0123456789abcdef",
        connection_generation=1,
        executor_generation=1,
        interaction_generation=1,
        audio_codec="opus",
    )


def test_media_wire_opus_packet_uses_bounded_binary_payload_and_codec_tag() -> None:
    library = find_opus_library()
    if library is None:
        pytest.skip("固定 libopus 运行库未准备")
    with OpusEncoder(16_000, library_path=library) as encoder:
        pcm = array("h", (int(6_000 * math.sin(i * 0.17)) for i in range(320))).tobytes()
        packet = encoder.encode(pcm)
        frame = _opus_frame(0, packet)
    decoded = decode_frame(encode_frame(frame))
    assert decoded.audio_codec == "opus"
    assert decoded.sample_count == 320
    assert decoded.payload == packet


def test_media_handshake_negotiates_uplink_codec_and_encoder_pre_skip() -> None:
    websocket = SimpleNamespace(scope={"query_string": (
        b"protocol_version=1&stream_id=opus-stream&connection_generation=1&executor_generation=1"
        b"&interaction_generation=1&uplink_codec=opus&downlink_codec=pcm&encoder_pre_skip_samples=104"
        b"&downlink_encoder_pre_skip_samples=0"
    )})
    payload = _device_media_open_payload(websocket)
    assert payload["uplink_codec"] == "opus"
    assert payload["downlink_codec"] == "pcm"
    assert payload["encoder_pre_skip_samples"] == 104
    assert payload["downlink_encoder_pre_skip_samples"] == 0


def test_media_handshake_defaults_legacy_clients_to_pcm_and_negotiates_opus_output() -> None:
    legacy = SimpleNamespace(scope={"query_string": (
        b"protocol_version=1&stream_id=pcm-stream&connection_generation=1&executor_generation=1"
        b"&interaction_generation=1"
    )})
    assert "uplink_codec" not in _device_media_open_payload(legacy)

    opus_downlink = SimpleNamespace(scope={"query_string": (
        b"protocol_version=1&stream_id=opus-stream&connection_generation=1&executor_generation=1"
        b"&interaction_generation=1&uplink_codec=opus&downlink_codec=opus&encoder_pre_skip_samples=104"
        b"&downlink_encoder_pre_skip_samples=312"
    )})
    negotiated = _device_media_open_payload(opus_downlink)
    assert negotiated["uplink_codec"] == "opus"
    assert negotiated["downlink_codec"] == "opus"
    assert negotiated["encoder_pre_skip_samples"] == 104
    assert negotiated["downlink_encoder_pre_skip_samples"] == 312


def test_media_handshake_rejects_partial_or_inconsistent_codec_options() -> None:
    base = b"protocol_version=1&stream_id=opus-stream&connection_generation=1&executor_generation=1&interaction_generation=1"
    for extension in (
        b"&uplink_codec=opus&downlink_codec=pcm",
        b"&uplink_codec=opus&downlink_codec=pcm&encoder_pre_skip_samples=0",
        b"&uplink_codec=pcm&downlink_codec=pcm&encoder_pre_skip_samples=1",
        b"&uplink_codec=pcm&downlink_codec=pcm&encoder_pre_skip_samples=0&downlink_encoder_pre_skip_samples=1",
        b"&uplink_codec=opus&downlink_codec=opus&encoder_pre_skip_samples=104&downlink_encoder_pre_skip_samples=0",
        b"&uplink_codec=opus&uplink_codec=opus&downlink_codec=pcm&encoder_pre_skip_samples=104",
        b"&downlink_encoder_pre_skip_samples=0",
    ):
        websocket = SimpleNamespace(scope={"query_string": base + extension})
        with pytest.raises(HTTPException):
            _device_media_open_payload(websocket)


def test_input_stream_discards_opus_pre_skip_and_restores_pcm_frame_cadence() -> None:
    library = find_opus_library()
    if library is None:
        pytest.skip("固定 libopus 运行库未准备")
    registry, lease = _active_lease()
    clock_map = lambda value: (value / 1_000_000, 0.001)
    with OpusEncoder(16_000, 48_000, library_path=library) as encoder:
        stream = PcmInputStream(
            stream_id="opus-stream",
            core_session_id="0123456789abcdef0123456789abcdef",
            lease=lease,
            registry=registry,
            map_device_time=clock_map,
            audio_codec="opus",
            encoder_pre_skip_samples=encoder.lookahead_samples,
        )
        audio_frames = []
        for sequence in range(8):
            pcm = array("h", (
                int(7_000 * math.sin(2 * math.pi * 440 * (sequence * 320 + i) / 16_000))
                for i in range(320)
            )).tobytes()
            audio = stream.accept(_opus_frame(sequence, encoder.encode(pcm)))
            if audio is not None:
                audio_frames.append(audio)

    assert len(audio_frames) == 7
    assert [frame.start_sample for frame in audio_frames] == list(range(0, 7 * 320, 320))
    assert all(len(frame.pcm) == 640 for frame in audio_frames)
    assert audio_frames[0].captured_at == pytest.approx(0.02)
    assert audio_frames[-1].captured_at == pytest.approx(0.14)
    stream.close()


def test_input_stream_rejects_pcm_frame_after_opus_negotiation_and_closes() -> None:
    registry, lease = _active_lease()
    stream = PcmInputStream(
        stream_id="opus-stream",
        core_session_id="0123456789abcdef0123456789abcdef",
        lease=lease,
        registry=registry,
        map_device_time=lambda value: value / 1_000_000,
        audio_codec="opus",
        encoder_pre_skip_samples=0,
    )
    frame = MediaFrame(
        stream_id="opus-stream", sequence=0, start_sample=0, sample_count=320,
        device_time_us=20_000, payload=bytes(640), device_id="opus-device",
        core_session_id="0123456789abcdef0123456789abcdef", connection_generation=1,
        executor_generation=1, interaction_generation=1, audio_codec="pcm",
    )
    with pytest.raises(MediaProtocolError, match="编解码模式"):
        stream.accept(frame)
    assert stream.closed

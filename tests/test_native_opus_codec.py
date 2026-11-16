"""Official libopus stream codec smoke（官方 libopus 流式编解码冒烟）。"""
from __future__ import annotations

from array import array
import math

import pytest

from voice_terminal.devices.opus_codec import (
    MAX_OPUS_PACKET_BYTES,
    OpusDecoder,
    OpusEncoder,
    find_opus_library,
)


@pytest.mark.parametrize(("sample_rate", "bitrate"), [(16_000, 48_000), (48_000, 64_000)])
def test_native_opus_round_trips_stateful_twenty_millisecond_mono_frames(
    sample_rate: int, bitrate: int,
) -> None:
    library = find_opus_library()
    if library is None:
        pytest.skip("固定 libopus 运行库未准备")

    frame_samples = sample_rate // 50
    decoded_frames = []
    with OpusEncoder(sample_rate, bitrate, library_path=library) as encoder, \
            OpusDecoder(sample_rate, library_path=library) as decoder:
        assert encoder.lookahead_samples > 0
        for frame_index in range(8):
            samples = array("h", (
                int(8_000 * math.sin(2 * math.pi * 440 * (frame_index * frame_samples + index) / sample_rate))
                for index in range(frame_samples)
            ))
            packet = encoder.encode(samples.tobytes())
            assert 0 < len(packet) <= MAX_OPUS_PACKET_BYTES
            decoded = decoder.decode(packet)
            assert len(decoded) == frame_samples * 2
            decoded_frames.append(array("h", decoded))

    assert sum(sample != 0 for frame in decoded_frames for sample in frame) > frame_samples


def test_opus_dtx_is_disabled_and_invalid_frame_shapes_are_rejected() -> None:
    library = find_opus_library()
    if library is None:
        pytest.skip("固定 libopus 运行库未准备")

    with OpusEncoder(16_000, library_path=library) as encoder:
        assert encoder.encode(bytes(320 * 2))
        with pytest.raises(ValueError, match="20 ms"):
            encoder.encode(bytes(319 * 2))


def test_opus_codec_rejects_rates_that_need_resampling_before_encoding() -> None:
    with pytest.raises(ValueError, match="32 kHz"):
        OpusEncoder(32_000)

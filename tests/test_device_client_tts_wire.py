from __future__ import annotations

import json
import struct

import pytest

from voice_terminal.devices.media_protocol import (
    ClientTtsAudioFrame,
    MAX_HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
    MediaProtocolError,
    decode_client_tts_audio_frame,
    encode_client_tts_audio_frame,
)


def _frame(**changes) -> ClientTtsAudioFrame:
    fields = {
        "device_id": "device-a",
        "core_session_id": "core-a",
        "connection_generation": 2,
        "executor_generation": 3,
        "interaction_generation": 4,
        "stream_id": "media-a",
        "task_id": "speech-a",
        "request_id": "tts-request-a",
        "sequence": 0,
        "start_sample": 0,
        "sample_count": 320,
        "sample_rate": 16_000,
        "device_time_us": 12_000,
        "payload": b"\x34\x12" * 320,
    }
    fields.update(changes)
    return ClientTtsAudioFrame(**fields)


def test_client_tts_audio_frame_round_trip_preserves_pcm_and_transaction_identity() -> None:
    frame = _frame()

    decoded = decode_client_tts_audio_frame(encode_client_tts_audio_frame(frame))

    assert decoded == frame


@pytest.mark.parametrize(
    "changes",
    [
        {"connection_generation": 0},
        {"interaction_generation": 0},
        {"sample_rate": 44_100},
        {"sample_count": 321},
        {"payload": b"\x00" * 638},
        {"request_id": " bad-id"},
        {"start_sample": (1 << 53) - 1},
    ],
)
def test_client_tts_audio_frame_encoder_rejects_wrong_generation_or_pcm_bounds(changes) -> None:
    with pytest.raises(MediaProtocolError):
        encode_client_tts_audio_frame(_frame(**changes))


def test_client_tts_audio_frame_decoder_rejects_unknown_duplicate_and_oversized_messages() -> None:
    payload = b"\x00\x00" * 320
    unknown = json.dumps({
        "protocol_version": 1,
        "direction": "tts_audio",
        "device_id": "device-a",
        "core_session_id": "core-a",
        "connection_generation": 2,
        "executor_generation": 3,
        "interaction_generation": 4,
        "stream_id": "media-a",
        "task_id": "speech-a",
        "request_id": "tts-request-a",
        "sequence": 0,
        "start_sample": 0,
        "sample_count": 320,
        "sample_rate": 16_000,
        "device_time_us": 12_000,
        "extra": "reject",
    }, separators=(",", ":")).encode("utf-8")
    with pytest.raises(MediaProtocolError):
        decode_client_tts_audio_frame(struct.pack("!I", len(unknown)) + unknown + payload)

    duplicate = (
        b'{"protocol_version":1,"direction":"tts_audio","device_id":"device-a",'
        b'"device_id":"device-b","core_session_id":"core-a","connection_generation":2,'
        b'"executor_generation":3,"interaction_generation":4,"stream_id":"media-a",'
        b'"task_id":"speech-a","request_id":"tts-request-a","sequence":0,"start_sample":0,'
        b'"sample_count":320,"sample_rate":16000,"device_time_us":12000}'
    )
    with pytest.raises(MediaProtocolError):
        decode_client_tts_audio_frame(struct.pack("!I", len(duplicate)) + duplicate + payload)

    with pytest.raises(MediaProtocolError):
        decode_client_tts_audio_frame(b"\x00" * (4 + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES + 1))

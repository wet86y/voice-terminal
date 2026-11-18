"""Client TTS v2 wire（客户端直放第 2 版媒体 JSON）测试。"""
from __future__ import annotations

import json

import pytest

from voice_terminal.devices.media_protocol import (
    ClientTtsAudioFrame,
    MediaProtocolError,
    create_tts_control,
    encode_client_tts_audio_frame,
    parse_tts_control,
)


def _identity() -> dict[str, object]:
    return {
        "device_id": "device-a", "core_session_id": "core-a",
        "connection_generation": 2, "executor_generation": 3,
        "interaction_generation": 4, "stream_id": "media-a",
    }


def test_authorization_control_v2_round_trips_with_field_explanations() -> None:
    control = create_tts_control(
        "media.tts.authorize", _identity(), authorization_id="auth-a", playback_key="speech-a",
        provider="minimax", ttl_ms=60_000, credential="test-only-secret",
    )
    encoded = json.dumps(control, ensure_ascii=True, separators=(",", ":"))
    decoded = parse_tts_control(encoded)
    assert decoded == control
    assert decoded["protocol_version"] == 2
    assert decoded["fields"]["credential"] == "服务端临时授权的供应商密钥"


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol_version": 1},
        {"connection_generation": 0},
        {"ttl_ms": 300_001},
        {"api_key": "must-not-be-an-alternate-secret-field"},
        {"stream_id": "bad stream"},
    ],
)
def test_v2_authorization_rejects_wrong_version_bounds_or_secret_alias(changes) -> None:
    control = create_tts_control(
        "media.tts.authorize", _identity(), authorization_id="auth-a", playback_key="speech-a",
        provider="minimax", ttl_ms=60_000, credential="test-only-secret",
    )
    control.update(changes)
    control["fields"] = {key: "字段中文说明" for key in control if key != "fields"}
    with pytest.raises(MediaProtocolError):
        parse_tts_control(json.dumps(control))


def test_media_v2_refuses_legacy_client_pcm_frames() -> None:
    frame = ClientTtsAudioFrame(
        device_id="device-a", core_session_id="core-a", connection_generation=2,
        executor_generation=3, interaction_generation=4, stream_id="media-a",
        task_id="speech-a", request_id="request-a", sequence=0, start_sample=0,
        sample_count=320, sample_rate=16_000, device_time_us=12_000,
        payload=b"\x00\x00" * 320,
    )
    with pytest.raises(MediaProtocolError, match="禁止客户端合成 PCM 回传"):
        encode_client_tts_audio_frame(frame)

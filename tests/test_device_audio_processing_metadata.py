"""Remote per-frame AEC/VAD evidence（远端逐帧前处理证据）回归。"""
from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.turn_controller import InputController
from voice_terminal.controller.turn_workspace import AudioFrame
from voice_terminal.devices.media_protocol import (
    AudioProcessingStatus,
    MediaFrame,
    MediaProtocolError,
    decode_frame,
    encode_frame,
)
from voice_terminal.devices.pcm_stream import PcmInputStream
from voice_terminal.devices.registry import DeviceRegistry


def _status(
    *,
    state: str = "ready",
    near_end_voice: bool | None = False,
    protected: bool = True,
    reverse_generation: int = 1,
    error: str | None = None,
) -> AudioProcessingStatus:
    if state != "ready":
        near_end_voice = None
        protected = False
    return AudioProcessingStatus(state, near_end_voice, protected, reverse_generation, error)


def _frame(
    *,
    sequence: int = 0,
    status: AudioProcessingStatus | None = None,
    codec: str = "pcm",
    payload: bytes | None = None,
    device_id: str = "device-a",
    connection_generation: int = 1,
    executor_generation: int = 1,
    interaction_generation: int = 1,
    stream_id: str = "stream-a",
) -> MediaFrame:
    return MediaFrame(
        stream_id=stream_id,
        sequence=sequence,
        start_sample=sequence * 320,
        sample_count=320,
        device_time_us=(sequence + 1) * 20_000,
        payload=payload if payload is not None else bytes(640 if codec == "pcm" else 20),
        device_id=device_id,
        core_session_id="core-a",
        connection_generation=connection_generation,
        executor_generation=executor_generation,
        interaction_generation=interaction_generation,
        audio_codec=codec,
        audio_processing=status or _status(),
    )


def _input_stream(*, codec: str = "pcm", registry: DeviceRegistry | None = None, lease=None):
    if registry is None:
        registry = DeviceRegistry()
        registry.connect("device-a", capabilities=("audio_stream",))
        lease = registry.prepare_switch("device-a", registry.generation)
        assert registry.commit_switch(lease)
    stream = PcmInputStream(
        stream_id="stream-a",
        core_session_id="core-a",
        lease=lease,
        registry=registry,
        map_device_time=lambda value: (100.0 + value / 1_000_000, 0.001),
        audio_codec=codec,
    )
    return registry, lease, stream


def _processing_controller() -> tuple[InputController, "_CountingProcessor"]:
    controller = object.__new__(InputController)
    processor = _CountingProcessor()
    controller.audio_processor = processor
    controller.state = SimpleNamespace(current={})
    controller.player = SimpleNamespace(running=True, has_started=lambda _key: True)
    controller.output = None
    controller.workspace = None
    controller._remote_audio_processing = None
    controller._remote_audio_processing_identity = None
    controller._remote_audio_reverse_generation = None
    controller._remote_audio_max_interaction_generation = None
    controller._remote_audio_retired_identities = deque(maxlen=32)
    return controller, processor


class _CountingProcessor:
    enabled = True
    protected_interruption_available = True
    last_voice_activity = False
    status = "READY"
    error = None

    def __init__(self) -> None:
        self.calls = 0

    def process(self, pcm: bytes) -> bytes:
        self.calls += 1
        return b"host-processed:" + pcm


def _wire_with_header(frame: MediaFrame, mutate) -> bytes:
    import json
    import struct

    message = encode_frame(frame)
    header_length = struct.unpack_from("!I", message)[0]
    header = json.loads(message[4:4 + header_length])
    mutate(header)
    encoded = json.dumps(header, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return struct.pack("!I", len(encoded)) + encoded + message[4 + header_length:]


def test_media_v2_requires_complete_health_and_near_voice_metadata() -> None:
    frame = _frame(status=_status(near_end_voice=True, reverse_generation=7))
    decoded = decode_frame(encode_frame(frame))
    assert decoded.audio_processing.near_end_voice is True
    assert decoded.audio_processing.protected_interruption_available is True
    assert decoded.audio_processing.reverse_generation == 7

    missing_object = _wire_with_header(frame, lambda header: header.pop("audio_processing"))
    with pytest.raises(MediaProtocolError, match="audio_processing"):
        decode_frame(missing_object)

    missing_nested_field = _wire_with_header(
        frame,
        lambda header: header["audio_processing"].pop("near_end_voice"),
    )
    with pytest.raises(MediaProtocolError, match="audio_processing"):
        decode_frame(missing_nested_field)

    invalid_boolean = _wire_with_header(
        frame,
        lambda header: header["audio_processing"].update(near_end_voice=1),
    )
    with pytest.raises(MediaProtocolError, match="近端语音"):
        decode_frame(invalid_boolean)


def test_pcm_stream_binds_processing_state_to_the_current_lease() -> None:
    _registry, lease, stream = _input_stream()
    frame = stream.accept(_frame(status=_status(near_end_voice=True, reverse_generation=3)))

    assert frame is not None
    assert frame.audio_processing.near_end_voice is True
    assert frame.audio_processing.reverse_generation == 3
    assert frame.processing_identity == (
        lease.device_id,
        lease.connection_generation,
        lease.executor_generation,
        lease.interaction_generation,
        "stream-a",
    )


def test_opus_stream_preserves_the_processing_state_for_each_decoded_frame(monkeypatch) -> None:
    class _Decoder:
        def __init__(self, _sample_rate: int) -> None:
            pass

        def decode(self, packet: bytes) -> bytes:
            return bytes((packet[0], 0)) * 320

        def close(self) -> None:
            pass

    monkeypatch.setattr("voice_terminal.devices.pcm_stream.OpusDecoder", _Decoder)
    _registry, _lease, stream = _input_stream(codec="opus")
    first = stream.accept(_frame(
        sequence=0, status=_status(near_end_voice=False, reverse_generation=4),
        codec="opus", payload=b"\x11",
    ))
    second = stream.accept(_frame(
        sequence=1, status=_status(near_end_voice=True, reverse_generation=5),
        codec="opus", payload=b"\x22",
    ))

    assert first is not None and first.pcm == b"\x11\x00" * 320
    assert first.audio_processing.reverse_generation == 4
    assert first.audio_processing.near_end_voice is False
    assert second is not None and second.pcm == b"\x22\x00" * 320
    assert second.audio_processing.reverse_generation == 5
    assert second.audio_processing.near_end_voice is True

    with pytest.raises(MediaProtocolError, match="代次倒退"):
        stream.accept(_frame(
            sequence=2, status=_status(reverse_generation=4), codec="opus", payload=b"\x33",
        ))


def test_remote_pcm_bypasses_host_processor_and_uses_frame_barge_in_evidence() -> None:
    controller, processor = _processing_controller()
    identity = ("device-a", 1, 1, 4, "stream-a")
    pcm = b"\x01\x00" * 320

    assert controller._process_audio(
        pcm, audio_processing=_status(near_end_voice=True), processing_identity=identity,
    ) is pcm
    assert processor.calls == 0
    assert controller._near_voice_activity() is True
    assert controller._barge_in_protected_available() is True

    controller._process_audio(
        pcm, audio_processing=_status(near_end_voice=False), processing_identity=identity,
    )
    assert controller._near_voice_activity() is False

    controller._process_audio(
        pcm,
        audio_processing=_status(state="degraded", protected=False, error="AEC unavailable"),
        processing_identity=identity,
    )
    assert processor.calls == 0
    assert controller._near_voice_activity() is None
    assert controller._barge_in_protected_available() is False

    assert controller._process_audio(pcm) == b"host-processed:" + pcm
    assert processor.calls == 1
    assert controller._near_voice_activity() is False


def test_realtime_handle_frame_does_not_dispatch_remote_pcm_to_host_processor() -> None:
    controller = object.__new__(RealtimeController)
    processor = _CountingProcessor()
    controller.audio_processor = processor
    controller.state = SimpleNamespace(listening=True, input_owner="asr", current={})
    controller._input_resetting = False
    controller._vad_generation = 0
    controller._vad_origin = None
    controller._last_frame = None
    controller._recent = deque()
    controller._asr_queue_overflow = False
    controller._reset_vad_pending = False
    controller.wake_always_on = False
    controller.wake_enabled = False
    controller.wake_preroll_ms = 0
    controller.owner_only_barge_in = False
    controller.workspace = None
    controller._active_input = False
    controller.speaker_sensor = None
    controller.vad = SimpleNamespace(feed=lambda _pcm: SimpleNamespace(
        speech_started=False, speech_ended=False, speech_end_sample=None,
        speech_start_sample=None, is_speech=False,
    ))

    async def no_replay(_frame):
        return False

    async def consume_without_turn(_activity, _frame):
        return None

    controller._buffer_wake_frame = no_replay
    controller._observe_activity = consume_without_turn
    frame = AudioFrame(
        b"\x01\x00" * 320, 0, 1.0, audio_processing=_status(near_end_voice=True),
        processing_identity=("device-a", 1, 1, 4, "stream-a"),
    )

    asyncio.run(controller.handle_frame(frame))

    assert processor.calls == 0
    assert controller._near_voice_activity() is True


def test_remote_degraded_aec_suppresses_barge_without_host_recovery_or_pause() -> None:
    controller = object.__new__(EpisodeController)

    class _Episode:
        closed = False

        def __init__(self) -> None:
            self.logs = []

        async def log(self, kind, **values):
            self.logs.append((kind, values))

    class _Gate:
        def __init__(self) -> None:
            self.resets = 0

        def reset(self) -> None:
            self.resets += 1

    episode = _Episode()
    gate = _Gate()
    calls = {"local_recovery": 0, "pause": 0, "cancel": 0}
    handle = SimpleNamespace(workspace=SimpleNamespace(playback_started=True, playback_key="speech-a"))
    controller.state = SimpleNamespace(listening=True)
    controller.episode = episode
    controller.output = SimpleNamespace(active=True, handle=handle)
    controller._playback_progress = lambda _handle: {"remaining_ms": 100}
    controller._barge_in_protected_available = lambda: False
    controller._clear_owner_barge_candidate = lambda: None

    async def local_recovery(_handle):
        calls["local_recovery"] += 1

    controller._handle_aec_unavailable = local_recovery
    controller.owner_only_barge_in = False
    controller.barge_in_gate = gate
    controller._barge_trace_state = (True, False, "near_voice_unknown")
    controller.player = SimpleNamespace(
        pause_confirmed=lambda *_args: calls.__setitem__("pause", calls["pause"] + 1),
    )
    frame = AudioFrame(
        bytes(640), 0, 1.0,
        audio_processing=_status(state="degraded", protected=False, error="remote reference unavailable"),
        processing_identity=("device-a", 1, 1, 4, "stream-a"),
    )
    activity = SimpleNamespace(is_speech=True, speech_started=True)

    result = asyncio.run(controller._observe_activity(activity, frame))

    assert result is None
    assert calls == {"local_recovery": 0, "pause": 0, "cancel": 0}
    assert gate.resets == 1
    assert controller._barge_trace_state[0] == "remote_aec_unavailable"
    assert episode.logs[-1][0] == "AEC_BARGE_IN_SUPPRESSED"
    assert episode.logs[-1][1]["policy"] == "continue_without_barge_in"

    asyncio.run(controller._observe_activity(activity, frame))
    assert len(episode.logs) == 1  # Do not emit one suppression log for every 20 ms frame.
    assert gate.resets == 2

    local_frame = AudioFrame(bytes(640), 0, 1.0)
    asyncio.run(controller._observe_activity(activity, local_frame))
    assert calls["local_recovery"] == 1


def test_reverse_reference_change_clears_cache_and_old_interaction_generation_is_rejected() -> None:
    controller, processor = _processing_controller()
    current = ("device-a", 2, 3, 8, "stream-b")
    pcm = bytes(640)
    controller._process_audio(
        pcm, audio_processing=_status(near_end_voice=True, reverse_generation=9),
        processing_identity=current,
    )
    assert controller._near_voice_activity() is True

    controller._process_audio(
        pcm, audio_processing=_status(near_end_voice=False, reverse_generation=10),
        processing_identity=current,
    )
    assert controller._remote_audio_reverse_generation == 10
    assert controller._near_voice_activity() is False

    with pytest.raises(ValueError, match="迟到的播放参考代次"):
        controller._process_audio(
            pcm, audio_processing=_status(near_end_voice=True, reverse_generation=9),
            processing_identity=current,
        )
    next_generation = ("device-a", 2, 3, 9, "stream-c")
    controller._process_audio(
        pcm, audio_processing=_status(near_end_voice=True, reverse_generation=0),
        processing_identity=next_generation,
    )
    assert controller._near_voice_activity() is True
    with pytest.raises(ValueError, match="已撤销"):
        controller._process_audio(
            pcm, audio_processing=_status(near_end_voice=True, reverse_generation=10),
            processing_identity=current,
        )
    assert processor.calls == 0


def test_registry_replacement_rejects_old_processing_metadata() -> None:
    registry, old_lease, old_stream = _input_stream()
    old_frame = _frame()
    registry.connect("device-a", capabilities=("audio_stream",))

    with pytest.raises(MediaProtocolError, match="租约已失效"):
        old_stream.accept(old_frame)

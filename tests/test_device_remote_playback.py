"""Remote playback media framing（远端播放媒体协议）回归。"""
from __future__ import annotations

import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.devices.media_protocol import MediaProtocolError
from voice_terminal.devices.opus_codec import OpusDecoder, OpusEncoder, find_opus_library
from voice_terminal.devices.remote_playback import (
    MAX_DEVICE_PLAYBACK_BUFFER_MS,
    PlaybackMediaFrame,
    RemotePlaybackOutbox,
    decode_playback_frame,
    encode_playback_frame,
)
from voice_terminal.devices.registry import InteractionLease
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.devices.contracts import PlaybackOutcomeUnknown


def _frame(**overrides) -> PlaybackMediaFrame:
    values = {
        "device_id": "device-a",
        "delivery_device_id": "device-a",
        "core_session_id": "core-a",
        "connection_generation": 2,
        "executor_generation": 3,
        "interaction_generation": 4,
        "stream_id": "media-a",
        "playback_key": "speech-a",
        "sequence": 0,
        "start_sample": 0,
        "sample_count": 320,
        "sample_rate": 16_000,
        "host_send_time_us": 1_234_000,
        "payload": b"\x01\x00" * 320,
    }
    values.update(overrides)
    return PlaybackMediaFrame(**values)


def test_device_acceptance_does_not_free_playback_buffer_credit() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
        )
        await outbox.enqueue("speech-a", 16_000, bytes(32_000))
        count = MAX_DEVICE_PLAYBACK_BUFFER_MS // 20
        for index in range(count):
            frame = await outbox.next_frame()
            assert frame.sequence == index
            assert outbox.acknowledge(index)
        incoming = asyncio.create_task(outbox.next_frame())
        await asyncio.sleep(0)
        assert not incoming.done()
        assert outbox.record_buffer(count, MAX_DEVICE_PLAYBACK_BUFFER_MS)
        await asyncio.sleep(0)
        assert not incoming.done()
        assert outbox.record_buffer(count, MAX_DEVICE_PLAYBACK_BUFFER_MS - 20)
        frame = await asyncio.wait_for(incoming, 0.5)
        assert frame.sequence == count
        assert outbox.acknowledge(frame.sequence, cancelled=True, reason="output_unavailable")
        assert outbox.failure("speech-a")
        assert outbox.record_buffer(count + 1, 0)
        await outbox.enqueue("speech-b", 16_000, bytes(640))
        frame = await asyncio.wait_for(outbox.next_frame(), 0.5)
        assert frame.playback_key == "speech-b"
        assert frame.sequence == count + 1
        await outbox.close()

    asyncio.run(scenario())


def test_remote_preview_slices_at_frame_aligned_chunk_boundaries() -> None:
    runtime = object.__new__(SessionRuntime)
    runtime._remote_playback_bindings = {}
    runtime.device_registry = SimpleNamespace(active=SimpleNamespace(device_id="device-a"))
    runtime.enqueue_remote_playback = AsyncMock()
    pcm = b"\x01\x02" * 48_000
    assert asyncio.run(runtime._enqueue_player_audio("preview", 16_000, pcm))
    calls = runtime.enqueue_remote_playback.await_args_list
    assert [len(call.args[2]) for call in calls] == [64_000, 32_000]
    assert b"".join(call.args[2] for call in calls) == pcm
    assert all(call.kwargs == {"delivery_device_id": "device-a"} for call in calls)


def test_opus_finish_keeps_receiving_playback_progress_while_waiting_for_credit() -> None:
    library = find_opus_library()
    if library is None:
        pytest.skip("固定 libopus 运行库未准备")
    with OpusEncoder(48_000, 64_000, library_path=library) as encoder:
        pre_skip = encoder.lookahead_samples

    async def scenario() -> None:
        lease = InteractionLease("device-a", interaction_generation=4, connection_generation=2, executor_generation=3)
        key = "speech-opus"
        outbox = RemotePlaybackOutbox(
            device_id=lease.device_id, core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
            downlink_codec="opus", downlink_encoder_pre_skip_samples=pre_skip, max_frames=1,
        )
        runtime = object.__new__(SessionRuntime)
        runtime._remote_playback_bindings = {key: (lease.device_id, outbox)}
        runtime._remote_playback_outboxes = {lease.device_id: outbox}
        runtime._remote_playback_progress = {}
        runtime._remote_playback_finished = set()
        runtime._remote_playback_lock = asyncio.Lock()
        runtime.device_registry = SimpleNamespace(accepts=lambda candidate: candidate == lease)
        runtime.player = None

        enqueue = asyncio.create_task(outbox.enqueue(key, 48_000, bytes(3840)))
        first = await outbox.next_frame()
        assert outbox.acknowledge(first.sequence)
        second = await outbox.next_frame()
        await enqueue
        finish = asyncio.create_task(runtime.finish_remote_playback(key))
        await asyncio.sleep(0)
        assert not finish.done()
        assert await asyncio.wait_for(runtime.record_remote_playback_progress(lease, "media-a", {
            "playback_key": key, "receipt_sequence": 1, "sequence": first.sequence,
            "enqueued_ms": 13, "played_ms": 4, "buffered_ms": 9, "output_estimated_ms": 4,
            "device_time_us": 100_000, "precision": "device_time", "started": True,
        }), 0.5)
        assert outbox.acknowledge(second.sequence)
        assert await asyncio.wait_for(finish, 0.5)
        await outbox.close()

    asyncio.run(scenario())


def test_playback_media_frame_round_trip_preserves_fixed_delivery_and_pcm() -> None:
    frame = _frame()
    decoded = decode_playback_frame(encode_playback_frame(frame))
    assert decoded == frame
    assert decoded.delivery_device_id == "device-a"
    assert decoded.playback_key == "speech-a"
    assert len(decoded.payload) == decoded.sample_count * 2

    encoded = encode_playback_frame(frame)
    header_length = struct.unpack_from("!I", encoded)[0]
    header = json.loads(encoded[4:4 + header_length])
    del header["audio_codec"]
    legacy_header = json.dumps(header, separators=(",", ":")).encode("utf-8")
    legacy = struct.pack("!I", len(legacy_header)) + legacy_header + encoded[4 + header_length:]
    assert decode_playback_frame(legacy) == frame


def test_playback_media_opus_frame_preserves_valid_sample_mapping() -> None:
    frame = _frame(
        audio_codec="opus", sample_rate=48_000, sample_count=648, payload=bytes([0x5a]) * 500,
    )
    decoded = decode_playback_frame(encode_playback_frame(frame))
    assert decoded == frame
    assert decoded.sample_count == 648
    assert decoded.sample_rate == 48_000
    assert decoded.payload == frame.payload
    for overrides in (
        {"sample_rate": 32_000},
        {"sample_count": 961},
        {"payload": b""},
        {"payload": bytes([0x5a]) * 1276},
    ):
        values = {
            "audio_codec": "opus", "sample_rate": 48_000,
            "sample_count": 648, "payload": bytes([0x5a]) * 500,
        }
        values.update(overrides)
        with pytest.raises(MediaProtocolError):
            encode_playback_frame(_frame(**values))


@pytest.mark.parametrize("overrides", [
    {"delivery_device_id": "device-b"},
    {"sample_rate": 22_050},
    {"sample_count": 321},
    {"payload": b"\x00"},
    {"interaction_generation": 0},
])
def test_playback_media_frame_rejects_invalid_identity_format_or_generation(overrides: dict) -> None:
    with pytest.raises(MediaProtocolError):
        encode_playback_frame(_frame(**overrides))


def test_remote_playback_outbox_splits_frames_and_preserves_per_key_sample_positions() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a", max_frames=3,
        )
        assert await outbox.enqueue("speech-a", 16_000, b"\x01\x00" * 480) == 30
        first = await outbox.next_frame()
        second = await outbox.next_frame()
        assert first is not None and second is not None
        assert (first.sequence, first.start_sample, first.sample_count) == (0, 0, 320)
        assert (second.sequence, second.start_sample, second.sample_count) == (1, 320, 160)
        assert len(second.payload) == 320

        await outbox.enqueue("speech-b", 24_000, b"\x02\x00" * 480)
        third = await outbox.next_frame()
        assert third is not None
        assert (third.sequence, third.playback_key, third.start_sample, third.sample_count) == (
            2, "speech-b", 0, 480,
        )
        await outbox.close()
        assert await outbox.next_frame() is None

    asyncio.run(scenario())


def test_opus_downlink_resamples_and_maps_encoder_delay_and_tail_samples() -> None:
    library = find_opus_library()
    if library is None:
        pytest.skip("固定 libopus 运行库未准备")
    try:
        import soxr  # noqa: F401
    except ImportError:
        pytest.skip("固定 soxr 重采样组件未准备")
    with OpusEncoder(48_000, 64_000, library_path=library) as probe:
        pre_skip = probe.lookahead_samples

    async def scenario() -> None:
        source_rate = 32_000
        source_samples = 32_000
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
            downlink_codec="opus", downlink_encoder_pre_skip_samples=pre_skip,
            max_frames=64,
        )
        pcm = b"\x01\x00" * source_samples
        assert await outbox.enqueue("speech-opus", source_rate, pcm) == 1_000
        stream = outbox._opus_streams_by_key["speech-opus"]
        assert await outbox.finish_key("speech-opus")
        expected_samples = stream.total_samples
        assert expected_samples == 48_000

        assert outbox.enqueued_ms_for_key("speech-opus") == 0
        frames = []
        for _ in range(outbox.pending_frames()):
            frame = await outbox.next_frame()
            assert frame is not None and outbox.acknowledge(frame.sequence)
            assert outbox.record_buffer(frame.sequence + 1, 0)
            frames.append(frame)
        assert frames and all(frame is not None for frame in frames)
        valid_samples = 0
        next_sample = 0
        decoded_sample_count = 0
        with OpusDecoder(48_000, library_path=library) as decoder:
            for index, frame in enumerate(frames):
                assert frame is not None
                assert frame.audio_codec == "opus"
                assert frame.sample_rate == 48_000
                assert frame.start_sample == next_sample
                assert 0 < frame.sample_count <= 960
                next_sample += frame.sample_count
                valid_samples += frame.sample_count
                decoded = decoder.decode(frame.payload)
                decoded_samples = len(decoded) // 2
                decoded_sample_count += decoded_samples
                if index == 0:
                    assert frame.sample_count == 960 - pre_skip
        assert valid_samples == expected_samples
        assert decoded_sample_count - pre_skip >= valid_samples
        assert outbox.enqueued_ms_for_key("speech-opus") == 1_000
        await outbox.close()

    asyncio.run(scenario())


def test_opus_short_stream_waits_for_finish_and_reports_exact_valid_samples() -> None:
    library = find_opus_library()
    if library is None:
        pytest.skip("固定 libopus 运行库未准备")
    with OpusEncoder(48_000, 64_000, library_path=library) as probe:
        pre_skip = probe.lookahead_samples

    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
            downlink_codec="opus", downlink_encoder_pre_skip_samples=pre_skip,
        )
        await outbox.enqueue("speech-short", 48_000, b"\x01\x00" * 300)
        assert outbox.pending_frames() == 0
        stream = outbox._opus_streams_by_key["speech-short"]
        await outbox.finish_key("speech-short")
        frame = await outbox.next_frame()
        assert frame is not None
        assert frame.audio_codec == "opus"
        assert frame.start_sample == 0
        assert frame.sample_count == stream.total_samples == 300
        with OpusDecoder(48_000, library_path=library) as decoder:
            assert len(decoder.decode(frame.payload)) == 1_920
        assert outbox.acknowledge(frame.sequence)
        assert outbox.enqueued_ms_for_key("speech-short") == 6
        await outbox.close()

    asyncio.run(scenario())


def test_opus_downlink_obeys_the_acknowledged_frame_window_during_finish() -> None:
    library = find_opus_library()
    if library is None:
        pytest.skip("固定 libopus 运行库未准备")
    with OpusEncoder(48_000, 64_000, library_path=library) as probe:
        pre_skip = probe.lookahead_samples

    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
            downlink_codec="opus", downlink_encoder_pre_skip_samples=pre_skip,
            max_frames=1,
        )
        publishing = asyncio.create_task(outbox.enqueue("speech-window", 48_000, b"\x02\x00" * 1_920))
        first = await outbox.next_frame()
        assert first is not None and first.sample_count == 960 - pre_skip
        await asyncio.sleep(0)
        assert not publishing.done()
        assert outbox.acknowledge(first.sequence)
        second = await outbox.next_frame()
        assert second is not None and second.sample_count == 960
        assert outbox.acknowledge(second.sequence)
        assert await publishing == 40

        finishing = asyncio.create_task(outbox.finish_key("speech-window"))
        tail = await outbox.next_frame()
        assert tail is not None and tail.sample_count == pre_skip
        assert outbox.acknowledge(tail.sequence)
        assert await finishing
        assert outbox.enqueued_ms_for_key("speech-window") == 40
        await outbox.close()

    asyncio.run(scenario())


def test_remote_playback_outbox_waits_for_capacity_instead_of_dropping_audio() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a", max_frames=1,
        )
        publish = asyncio.create_task(outbox.enqueue("speech-a", 16_000, b"\x00\x00" * 960))
        first = await outbox.next_frame()
        assert first is not None and first.sequence == 0
        await asyncio.sleep(0)
        assert not publish.done()
        assert outbox.acknowledge(0)
        second = await outbox.next_frame()
        assert second is not None and second.sequence == 1
        assert not publish.done()
        assert outbox.acknowledge(1)
        third = await outbox.next_frame()
        assert third is not None and third.sequence == 2
        assert outbox.acknowledge(2)
        assert await publish == 60
        await outbox.close()

    asyncio.run(scenario())


def test_remote_playback_outbox_advances_only_on_matching_fifo_device_acceptance() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a", max_frames=1,
        )
        publish = asyncio.create_task(outbox.enqueue("speech-a", 16_000, b"\x00\x00" * 640))
        first = await outbox.next_frame()
        assert first is not None and first.sequence == 0
        assert outbox.enqueued_ms_for_key("speech-a") == 0
        assert outbox.pending_ms_for_key("speech-a") == 20
        assert not outbox.acknowledge(1)
        assert outbox.acknowledge(0)
        assert outbox.enqueued_ms_for_key("speech-a") == 20
        assert outbox.pending_ms_for_key("speech-a") == 0
        second = await outbox.next_frame()
        assert second is not None and second.sequence == 1
        assert outbox.enqueued_ms_for_key("speech-a") == 20
        assert outbox.pending_ms_for_key("speech-a") == 20
        assert not outbox.acknowledge(0)
        assert outbox.acknowledge(1)
        assert outbox.enqueued_ms_for_key("speech-a") == 40
        assert await publish == 40
        await outbox.close()

    asyncio.run(scenario())


def test_remote_playback_outbox_forgets_only_keys_without_pending_device_receipts() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a", max_frames=1,
        )
        enqueue = asyncio.create_task(outbox.enqueue("speech-a", 16_000, b"\x00\x00" * 320))
        frame = await outbox.next_frame()
        assert frame is not None
        assert outbox.forget_settled_key("speech-a") is False
        assert outbox.acknowledge(frame.sequence)
        assert await enqueue == 20
        receipt = {
            "playback_key": "speech-a", "receipt_sequence": 1, "sequence": 0,
            "enqueued_ms": 20, "played_ms": 20, "buffered_ms": 0,
            "output_estimated_ms": 20, "started": True,
            "device_time_us": 1_000_000, "precision": "device_time",
        }
        assert outbox.record_progress(receipt)
        assert outbox.forget_settled_key("speech-a") is False
        assert await outbox.finish_key("speech-a") is True
        assert outbox.forget_settled_key("speech-a") is True
        assert outbox.enqueued_ms_for_key("speech-a") == 0
        assert outbox.pending_ms_for_key("speech-a") == 0
        assert outbox.progress("speech-a") is None
        await outbox.close()

    asyncio.run(scenario())


def test_cancelled_device_frame_does_not_count_toward_expected_output_duration() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a", max_frames=1,
        )
        first_write = asyncio.create_task(outbox.enqueue("speech-a", 16_000, b"\x01\x00" * 320))
        first = await outbox.next_frame()
        assert first is not None and first.start_sample == 0
        assert outbox.acknowledge(first.sequence, cancelled=True)
        assert await first_write == 20
        assert outbox.enqueued_ms_for_key("speech-a") == 0

        second_write = asyncio.create_task(outbox.enqueue("speech-a", 16_000, b"\x02\x00" * 320))
        second = await outbox.next_frame()
        assert second is not None and second.start_sample == 320
        assert outbox.acknowledge(second.sequence)
        assert await second_write == 20
        assert outbox.enqueued_ms_for_key("speech-a") == 20
        assert outbox.pending_ms_for_key("speech-a") == 0
        await outbox.close()

    asyncio.run(scenario())


def test_finished_outbox_key_rejects_late_audio_and_cannot_be_forgotten_while_pending() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a", max_frames=1,
        )
        publishing = asyncio.create_task(outbox.enqueue("speech-finished", 16_000, b"\x00\x00" * 320))
        first = await outbox.next_frame()
        assert first is not None
        assert await outbox.finish_key("speech-finished") is True
        with pytest.raises(RuntimeError, match="已结束"):
            await outbox.enqueue("speech-finished", 16_000, b"\x00\x00" * 320)
        assert outbox.forget_settled_key("speech-finished") is False
        assert outbox.acknowledge(first.sequence)
        assert await publishing == 20
        assert await outbox.finish_key("speech-finished") is True
        assert outbox.forget_settled_key("speech-finished") is True
        await outbox.close()

    asyncio.run(scenario())


def test_remote_output_waiter_does_not_finish_while_network_frames_are_unacknowledged() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
        )
        await outbox.enqueue("speech-pending", 16_000, b"\x00\x00" * 320)
        runtime = object.__new__(SessionRuntime)
        runtime._remote_playback_bindings = {"speech-pending": ("device-a", outbox)}
        runtime._remote_playback_finished = {"speech-pending"}
        runtime._remote_client_tts_progress = {}  # _remote_client_tts_progress（客户端直放时钟快照）。
        runtime.device_registry = SimpleNamespace(connection=lambda _device: SimpleNamespace(
            connected=True, connection_generation=2, executor_generation=3,
        ))
        runtime._remote_playback_progress = {"speech-pending": {
            "enqueued_ms": 0, "played_ms": 0, "buffered_ms": 0,
            "output_estimated_ms": 0, "started": True, "precision": "device_time",
        }}
        with pytest.raises(PlaybackOutcomeUnknown, match="期限内"):
            await runtime.wait_remote_playback_output("speech-pending", 0.001)
        await outbox.close()

    asyncio.run(scenario())


def test_empty_remote_playback_finishes_without_receipt_and_releases_binding() -> None:
    async def scenario() -> None:
        from voice_terminal.devices.registry import InteractionLease

        lease = InteractionLease(
            device_id="device-a", connection_generation=2,
            executor_generation=3, interaction_generation=4,
        )
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
        )
        runtime = object.__new__(SessionRuntime)
        runtime._remote_playback_lock = asyncio.Lock()
        runtime._remote_playback_bindings = {"speech-empty": ("device-a", outbox)}
        runtime._remote_playback_finished = set()
        runtime._remote_client_tts_progress = {}  # _remote_client_tts_progress（客户端直放时钟快照）。
        runtime._remote_playback_progress = {}
        runtime.device_registry = SimpleNamespace(connection=lambda _device: SimpleNamespace(
            connected=True, connection_generation=2, executor_generation=3,
        ))
        assert await runtime.finish_remote_playback("speech-empty") is True
        await runtime.wait_remote_playback_output("speech-empty", 0.01)
        assert await runtime.unbind_empty_remote_playback_target("speech-empty", lease) is True
        assert "speech-empty" not in runtime._remote_playback_bindings
        await outbox.close()

    asyncio.run(scenario())


def test_remote_playback_outbox_keeps_only_monotonic_device_clock_progress() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a", max_frames=1,
        )
        enqueue = asyncio.create_task(outbox.enqueue("speech-a", 16_000, b"\x00\x00" * 320))
        frame = await outbox.next_frame()
        assert frame is not None
        assert outbox.acknowledge(frame.sequence)
        assert await enqueue == 20
        receipt = {
            "playback_key": "speech-a", "receipt_sequence": 1, "sequence": 0,
            "enqueued_ms": 20, "played_ms": 5, "buffered_ms": 15,
            "output_estimated_ms": 4, "started": True,
            "device_time_us": 1_000_000, "precision": "device_time",
        }
        assert outbox.record_progress(receipt)
        assert outbox.progress("speech-a") == receipt
        assert not outbox.record_progress({**receipt, "receipt_sequence": 2, "played_ms": 4})
        assert not outbox.record_progress({**receipt, "receipt_sequence": 1, "device_time_us": 1_000_001})
        await outbox.close()

    asyncio.run(scenario())


def test_remote_output_waiter_requires_a_drained_device_clock_snapshot() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
        )
        connection = SimpleNamespace(
            connected=True, connection_generation=2, executor_generation=3,
        )
        runtime = object.__new__(SessionRuntime)
        runtime._remote_playback_bindings = {"speech-a": ("device-a", outbox)}
        runtime._remote_playback_finished = {"speech-a"}
        runtime._remote_client_tts_progress = {}  # _remote_client_tts_progress（客户端直放时钟快照）。
        runtime.device_registry = SimpleNamespace(connection=lambda _device: connection)
        runtime._remote_playback_progress = {"speech-a": {
            "enqueued_ms": 20, "played_ms": 20, "buffered_ms": 0,
            "output_estimated_ms": 20, "started": True, "precision": "device_time",
        }}
        outbox.enqueued_ms_for_key = lambda _key: 20
        await runtime.wait_remote_playback_output("speech-a", 0.01)

        runtime._remote_playback_progress["speech-a"] = {
            "enqueued_ms": 20, "played_ms": 0, "buffered_ms": 20,
            "output_estimated_ms": 0, "started": True, "precision": "device_time",
        }
        with pytest.raises(PlaybackOutcomeUnknown, match="期限内"):
            await runtime.wait_remote_playback_output("speech-a", 0.001)
        assert runtime.remote_playback_buffered_ms("speech-a") == 20
        await outbox.close()
        assert runtime.remote_playback_is_bound("speech-a")
        assert runtime.remote_playback_buffered_ms("speech-a") == 0
        assert runtime.remote_playback_snapshot("speech-a")["buffered_ms"] == 20

    asyncio.run(scenario())


def test_finished_remote_playback_key_rejects_late_audio() -> None:
    async def scenario() -> None:
        outbox = RemotePlaybackOutbox(
            device_id="device-a", core_session_id="core-a", connection_generation=2,
            executor_generation=3, interaction_generation=4, stream_id="media-a",
        )
        runtime = object.__new__(SessionRuntime)
        runtime._remote_playback_lock = asyncio.Lock()
        runtime._remote_playback_bindings = {"speech-a": ("device-a", outbox)}
        runtime._remote_playback_finished = {"speech-a"}
        with pytest.raises(RuntimeError, match="已结束"):
            await runtime.enqueue_remote_playback("speech-a", 16_000, b"\x00\x00" * 320)
        assert outbox.pending_ms_for_key("speech-a") == 0
        await outbox.close()

    asyncio.run(scenario())

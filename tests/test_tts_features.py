from __future__ import annotations

import asyncio
import importlib.util
import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from voice_terminal.audio.player import PcmPlayer
from voice_terminal.audio.processing import AudioPreprocessor
from voice_terminal.controller.turn_controller import InputController
from voice_terminal.providers.minimax_tts import MiniMaxTtsClient
from voice_terminal.tts.alignment import DeliveryResolver, parse_alignment
from voice_terminal.tts.scheduling import DirectStreamingPolicy, PlaybackProgress, PlaybackWindowPolicy, TtsGenerationScheduler
from voice_terminal.tts.text_merger import BidiTextMerger
from voice_terminal.vad.barge_in import BargeInGate
from voice_terminal.vad.detector import VadActivity


async def _record_async(target: list[str], value: str) -> None:
    target.append(value)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_streaming_does_not_wait_for_audio_backlog(self) -> None:
        await DirectStreamingPolicy().wait_before_send(
            lambda: PlaybackProgress(returned_ms=20_000, played_ms=0), lambda: True
        )

    async def test_playback_window_releases_at_inclusive_lead_boundary(self) -> None:
        current = [PlaybackProgress(returned_ms=4000, played_ms=2000)]
        await PlaybackWindowPolicy(lead_ms=2000).wait_before_send(lambda: current[0], lambda: True)

    async def test_playback_window_uses_device_estimate_without_rewriting_callback_counter(self) -> None:
        current = [PlaybackProgress(returned_ms=4000, played_ms=2500, output_estimated_ms=1800)]
        task = asyncio.create_task(
            PlaybackWindowPolicy(lead_ms=2000).wait_before_send(lambda: current[0], lambda: True)
        )
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(current[0].played_ms, 2500)
        current[0] = PlaybackProgress(returned_ms=4000, played_ms=2500, output_estimated_ms=2000)
        await task

    async def test_playback_window_stops_when_generation_is_invalidated(self) -> None:
        valid = [True]
        progress = [PlaybackProgress(returned_ms=9000, played_ms=0)]

        async def advance() -> None:
            await asyncio.sleep(0.01)
            valid[0] = False

        task = asyncio.create_task(advance())
        await PlaybackWindowPolicy(lead_ms=2000).wait_before_send(lambda: progress[0], lambda: valid[0])
        await task

    async def test_generation_scheduler_serializes_and_cancels_waiting_send(self) -> None:
        sent: list[str] = []
        scheduler = TtsGenerationScheduler(
            DirectStreamingPolicy(),
            lambda text: _record_async(sent, text),
            lambda: PlaybackProgress(),
        )
        scheduler.begin("g1")
        await scheduler.send("g1", "第一段")
        scheduler.cancel("g1")
        with self.assertRaisesRegex(Exception, "generation"):
            await scheduler.send("g1", "不会发送")
        self.assertEqual(sent, ["第一段"])

    async def test_generation_scheduler_cancellation_wakes_window_wait(self) -> None:
        scheduler = TtsGenerationScheduler(
            PlaybackWindowPolicy(lead_ms=2000),
            lambda _text: _record_async([], "sent"),
            lambda: PlaybackProgress(returned_ms=30_000, played_ms=0),
        )
        scheduler.begin("g1")
        task = asyncio.create_task(scheduler.send("g1", "等待"))
        await asyncio.sleep(0)
        scheduler.cancel("g1")
        with self.assertRaisesRegex(Exception, "取消"):
            await task


class TextAndDeliveryTests(unittest.TestCase):
    def test_bidi_merger_preserves_punctuation_and_flushes_by_size_or_time(self) -> None:
        merger = BidiTextMerger(max_chars=4, max_wait_ms=30)
        self.assertEqual(merger.feed("你好"), [])
        self.assertEqual(merger.feed("，世界"), ["你好，世", "界"])
        merger.feed("尾")
        self.assertEqual(merger.finish(), ["尾"])

    def test_delivery_resolver_deduplicates_cumulative_subtitles_and_marks_overlap(self) -> None:
        resolver = DeliveryResolver()
        resolver.add_provider_alignment({"words": [
            {"word": "前", "start_ms": 0, "end_ms": 100},
            {"word": "半", "start_ms": 100, "end_ms": 200},
        ]})
        resolver.add_provider_alignment({"words": [
            {"word": "前", "start_ms": 0, "end_ms": 100},
            {"word": "半", "start_ms": 100, "end_ms": 200},
        ]})
        result = resolver.resolve(
            {"started": True, "output_estimated_ms": 150}, "前半句",
            overlap_start_ms=100, overlap_stop_ms=180,
        )
        self.assertEqual([span.text for span in resolver.spans], ["前", "半"])
        self.assertEqual(result["heard_text"], "前")
        self.assertIn("半", result["uncertain_text"])
        self.assertEqual(result["precision"], "uncertain")

    def test_delivery_resolver_keeps_repeated_words_as_separate_spans(self) -> None:
        resolver = DeliveryResolver()
        resolver.add_provider_alignment({"words": [
            {"word": "好", "start_ms": 0, "end_ms": 100},
            {"word": "好", "start_ms": 100, "end_ms": 200},
        ]})
        result = resolver.resolve(
            {"started": True, "output_estimated_ms": 150}, "好好",
            overlap_start_ms=100, overlap_stop_ms=150,
        )
        self.assertEqual(result["heard_text"], "好")
        self.assertEqual(result["uncertain_text"], "好")

    def test_delivery_resolver_preserves_generated_spacing_for_word_subtitles(self) -> None:
        resolver = DeliveryResolver()
        resolver.add_provider_alignment({"words": [
            {"word": "hello", "start_ms": 0, "end_ms": 100},
            {"word": "world", "start_ms": 100, "end_ms": 200},
        ]})
        result = resolver.resolve(
            {"started": True, "output_estimated_ms": 100}, "hello world",
        )
        self.assertEqual(result["heard_text"], "hello")
        self.assertEqual(result["uncertain_text"], "world")

    def test_delivery_resolver_marks_reset_timestamp_epoch_uncertain(self) -> None:
        resolver = DeliveryResolver()
        resolver.add_provider_alignment({"words": [
            {"word": "第一句", "start_ms": 0, "end_ms": 200},
            {"word": "结束", "start_ms": 200, "end_ms": 300},
        ]})
        # The second sentence starts its own subtitle clock at zero.  It must
        # not be sorted before the first sentence and reported as heard.
        resolver.add_provider_alignment({"words": [
            {"word": "第二句", "start_ms": 0, "end_ms": 200},
        ]})
        result = resolver.resolve(
            {"started": True, "output_estimated_ms": 250}, "第一句结束第二句",
        )
        self.assertIn("第二句", result["uncertain_text"])
        self.assertEqual(result["status"], "partial")

    def test_delivery_resolver_does_not_assume_ambiguous_timestamp_units(self) -> None:
        resolver = DeliveryResolver()
        resolver.add_provider_alignment({"words": [
            {"word": "未知单位", "start": 0, "end": 100},
        ]})
        result = resolver.resolve(
            {"started": True, "output_estimated_ms": 200}, "未知单位",
        )
        self.assertEqual(result["heard_text"], "")
        self.assertEqual(result["uncertain_text"], "未知单位")
        self.assertEqual(result["precision"], "uncertain")

    def test_delivery_without_alignment_is_conservative(self) -> None:
        resolver = DeliveryResolver()
        self.assertEqual(
            resolver.resolve({"started": True, "output_estimated_ms": 300}, "完整回答"),
            {
                "status": "unknown", "heard_text": "", "uncertain_text": "完整回答",
                "unheard_text": "", "precision": "unknown", "played_ms": 300,
            },
        )
        self.assertEqual(
            resolver.resolve({"started": False, "output_estimated_ms": 0}, "完整回答")["unheard_text"],
            "完整回答",
        )
        self.assertEqual(
            resolver.resolve({"started": True, "output_estimated_ms": 0}, "完整回答")["status"],
            "none",
        )

    def test_minimax_bidi_subtitle_expands_timestamped_words(self) -> None:
        subtitle = {
            "text": "你好，世界。",
            "time_begin": 0,
            "time_end": 1067,
            "text_begin": 0,
            "text_end": 6,
            "timestamped_words": [
                {"word": "你", "time_begin": 42.666, "time_end": 213.333, "word_begin": 0, "word_end": 1},
                {"word": "好", "time_begin": 213.333, "time_end": 341.333, "word_begin": 1, "word_end": 2},
                {"word": "，", "time_begin": 341.333, "time_end": 384, "word_begin": 2, "word_end": 3},
                {"word": "世", "time_begin": 384, "time_end": 682.666, "word_begin": 3, "word_end": 4},
            ],
        }
        spans = parse_alignment(subtitle, sentence_index=1)
        self.assertEqual([span.text for span in spans], ["你", "好", "，", "世"])
        self.assertEqual([(span.start_ms, span.end_ms) for span in spans], [(43, 213), (213, 341), (341, 384), (384, 683)])
        self.assertEqual([(span.text_start, span.text_end, span.sentence_index) for span in spans], [(0, 1, 1), (1, 2, 1), (2, 3, 1), (3, 4, 1)])

    def test_bidi_cumulative_snapshots_use_sentence_pcm_origins(self) -> None:
        resolver = DeliveryResolver()
        first_snapshot = {
            "text": "第一句。",
            "time_begin": 0,
            "time_end": 200,
            "text_begin": 0,
            "text_end": 4,
            "timestamped_words": [
                {"word": "第", "time_begin": 0, "time_end": 100, "word_begin": 0, "word_end": 1},
                {"word": "一", "time_begin": 100, "time_end": 200, "word_begin": 1, "word_end": 2},
            ],
        }
        cumulative_snapshot = {
            **first_snapshot,
            "time_end": 300,
            "timestamped_words": [
                *first_snapshot["timestamped_words"],
                {"word": "句", "time_begin": 200, "time_end": 300, "word_begin": 2, "word_end": 3},
            ],
        }
        second_sentence = {
            "text": "第二句。",
            "time_begin": 0,
            "time_end": 200,
            "text_begin": 0,
            "text_end": 4,
            "timestamped_words": [
                {"word": "第", "time_begin": 0, "time_end": 100, "word_begin": 0, "word_end": 1},
                {"word": "二", "time_begin": 100, "time_end": 200, "word_begin": 1, "word_end": 2},
            ],
        }
        resolver.add_provider_alignment({"value": first_snapshot, "sentence_index": 1, "audio_base_ms": 0})
        resolver.add_provider_alignment({"value": cumulative_snapshot, "sentence_index": 1, "audio_base_ms": 0})
        resolver.add_provider_alignment({"value": second_sentence, "sentence_index": 2, "audio_base_ms": 350})
        self.assertEqual(len(resolver.spans), 5)
        self.assertEqual([span.text for span in resolver.spans], ["第", "一", "句", "第", "二"])
        self.assertEqual([(span.start_ms, span.end_ms) for span in resolver.spans], [(0, 100), (100, 200), (200, 300), (350, 450), (450, 550)])
        result = resolver.resolve({"started": True, "output_estimated_ms": 425}, "第一句。第二句。")
        self.assertIn("第一句", result["heard_text"])
        self.assertIn("第", result["uncertain_text"])

    def test_bidi_word_snapshot_replaces_provisional_sentence_envelope(self) -> None:
        resolver = DeliveryResolver()
        resolver.add_provider_alignment({
            "value": {
                "text": "第二句。",
                "time_begin": 0,
                "time_end": 800,
                "text_begin": 0,
                "text_end": 4,
            },
            "sentence_index": 2,
            "audio_base_ms": 500,
        })
        resolver.add_provider_alignment({
            "value": {
                "text": "第二句。",
                "time_begin": 0,
                "time_end": 300,
                "text_begin": 0,
                "text_end": 4,
                "timestamped_words": [
                    {"word": "第", "time_begin": 0, "time_end": 100, "word_begin": 0, "word_end": 1},
                    {"word": "二", "time_begin": 100, "time_end": 200, "word_begin": 1, "word_end": 2},
                ],
            },
            "sentence_index": 2,
            "audio_base_ms": 500,
        })
        self.assertEqual([span.text for span in resolver.spans], ["第", "二"])


class BargeInTests(unittest.TestCase):
    def test_gate_requires_sustained_processed_voice_and_protection(self) -> None:
        gate = BargeInGate(min_speech_ms=250)
        activity = VadActivity(True, True, False)
        self.assertFalse(gate.feed(activity, now=0.0, near_voice=False).confirmed)
        self.assertFalse(gate.feed(activity, now=0.25, near_voice=True, protected_available=False).confirmed)
        gate.reset()
        self.assertFalse(gate.feed(activity, now=0.0, near_voice=True).confirmed)
        decision = gate.feed(VadActivity(True, False, False), now=0.25, near_voice=True)
        self.assertTrue(decision.confirmed)
        self.assertEqual(decision.reason, "confirmed")

    def test_gate_counts_valid_voice_time_instead_of_silence_gap(self) -> None:
        gate = BargeInGate(min_speech_ms=250)
        self.assertFalse(gate.feed(VadActivity(True, True, False), now=0.0, near_voice=True).confirmed)
        self.assertFalse(gate.feed(VadActivity(True, False, False), now=0.10, near_voice=True).confirmed)
        self.assertFalse(gate.feed(VadActivity(False, False, False), now=0.60, near_voice=True).confirmed)
        decision = gate.feed(VadActivity(True, False, False), now=0.75, near_voice=True)
        self.assertFalse(decision.confirmed)
        self.assertEqual(decision.duration_ms, 100)


class PlayerAndProcessingTests(unittest.TestCase):
    def test_protected_gate_allows_input_before_first_output_callback(self) -> None:
        """Opening PortAudio must not make the first response frames deaf."""

        player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        player._stream = object()  # The runtime opens this stream once at startup.
        player.allow("turn")
        player.enqueue("turn", b"\x01\x00")

        controller = object.__new__(InputController)
        controller.player = player
        controller.audio_processor = SimpleNamespace(
            enabled=True,
            protected_interruption_available=False,
        )
        controller.workspace = None
        controller.turns = SimpleNamespace(current="turn")

        # No callback has submitted this turn to the device yet, so no
        # assistant echo exists and the near-end gate may accept speech.
        self.assertTrue(controller._barge_in_protected_available())

        # Once a real output callback starts the turn, a fresh reverse
        # reference is required again; raw microphone VAD cannot take over.
        output = bytearray(2)
        player._callback(output, 1, None, None)
        self.assertFalse(controller._barge_in_protected_available())

    def test_player_reverse_reference_includes_device_silence(self) -> None:
        references: list[bytes] = []
        player = PcmPlayer(
            sample_rate=1000,
            channels=1,
            output_device=None,
            on_audio_rendered=references.append,
        )
        player.enqueue("turn", b"\x01\x00")
        output = bytearray(4)
        player._callback(output, 2, None, None)
        self.assertEqual(bytes(output), b"\x01\x00\x00\x00")
        self.assertEqual(references, [b"\x01\x00\x00\x00"])

    def test_stop_snapshot_is_atomic_and_old_key_cannot_stop_new_key(self) -> None:
        player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        player.allow("old")
        player.enqueue("old", b"\x01\x00" * 100)
        first = player.stop_and_snapshot("old")
        self.assertTrue(first.started is False)
        player.allow("new")
        player.enqueue("new", b"\x02\x00" * 100)
        mismatch = player.stop_and_snapshot("old")
        self.assertEqual(mismatch.buffered_ms, 0)
        self.assertGreater(player.buffered_ms, 0)
        current = player.stop_and_snapshot("new")
        self.assertEqual(current.key, "new")
        self.assertEqual(player.buffered_ms, 0)

    def test_unstarted_player_hold_preserves_pcm_until_resume(self) -> None:
        started: list[str] = []
        player = PcmPlayer(
            sample_rate=1000,
            channels=1,
            output_device=None,
            on_playback_started=started.append,
        )
        player.allow("turn")
        self.assertTrue(player.hold("turn"))
        player.enqueue("turn", b"\x01\x00")

        held_output = bytearray(2)
        player._callback(held_output, 1, None, None)
        self.assertEqual(bytes(held_output), b"\x00\x00")
        self.assertFalse(player.progress("turn")["started"])
        self.assertGreater(player.buffered_ms, 0)
        self.assertEqual(started, [])

        self.assertTrue(player.resume("turn"))
        output = bytearray(2)
        player._callback(output, 1, None, None)
        self.assertEqual(bytes(output), b"\x01\x00")
        self.assertTrue(player.progress("turn")["started"])
        self.assertEqual(started, ["turn"])
        self.assertFalse(player.hold("turn"))

    def test_disabled_audio_processing_is_explicit(self) -> None:
        processor = AudioPreprocessor(enabled=False)
        self.assertEqual(processor.status, "DISABLED")
        self.assertFalse(processor.protected_interruption_available)
        self.assertEqual(processor.process(b"\x00\x00"), b"\x00\x00")

    @unittest.skipUnless(
        importlib.util.find_spec("aec_audio_processing") is not None
        and importlib.util.find_spec("webrtcvad") is not None,
        "AEC and WebRTC VAD wheels are optional in minimal test environments",
    )
    def test_protected_processing_requires_a_fresh_reverse_reference(self) -> None:
        processor = AudioPreprocessor(enabled=True, required=True)
        try:
            self.assertFalse(processor.protected_interruption_available)
            processor.push_reverse(b"\x00\x00" * 160)
            self.assertTrue(processor.protected_interruption_available)
        finally:
            processor.close()

    @unittest.skipUnless(
        importlib.util.find_spec("aec_audio_processing") is not None
        and importlib.util.find_spec("webrtcvad") is not None,
        "AEC and WebRTC VAD wheels are optional in minimal test environments",
    )
    def test_installed_aec_wheel_processes_capture_and_reverse_frames(self) -> None:
        processor = AudioPreprocessor(
            sample_rate=16000,
            reverse_sample_rate=32000,
            enabled=True,
            required=True,
        )
        try:
            capture = b"\x01\x00" * 320  # 20 ms at 16 kHz
            self.assertEqual(len(processor.process(capture)), len(capture))
            processor.push_reverse(b"\x00\x00" * 640)  # 20 ms at 32 kHz
            self.assertEqual(processor.status, "READY")
        finally:
            processor.close()

    @unittest.skipUnless(
        importlib.util.find_spec("aec_audio_processing") is not None
        and importlib.util.find_spec("webrtcvad") is not None,
        "AEC and WebRTC VAD wheels are optional in minimal test environments",
    )
    def test_processed_frames_update_independent_near_end_voice_activity(self) -> None:
        processor = AudioPreprocessor(enabled=True, required=True, vad_aggressiveness=2)
        try:
            silence = b"\x00\x00" * 320
            processor.process(silence)
            self.assertFalse(processor.last_voice_activity)

            # A voiced 220 Hz frame is a stable, deterministic input for the
            # WebRTC VAD binding.  It is intentionally run through the AEC/NS
            # processor first; the gate must not use raw microphone loudness.
            import math
            import struct

            voiced = b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * 220 * i / 16000)))
                for i in range(320)
            )
            processor.process(voiced)
            self.assertTrue(processor.last_voice_activity)
        finally:
            processor.close()


class BidiLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_bidi_start_waits_for_connected_success_and_sends_task_start_once(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid",
            websocket_path="/ws/v1/t2a_v2_bidi",
            model="speech-2.8-hd",
            api_key="test",
            transport="websocket_bidi",
        )
        events: asyncio.Queue[dict] = asyncio.Queue()
        await events.put({"event": "connected_success", "connect_id": "connect-redacted"})
        await events.put({"event": "task_started"})

        class Socket:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send(self, payload: str) -> None:
                self.sent.append(json.loads(payload))

            async def recv(self) -> str:
                return json.dumps(await events.get())

            async def close(self) -> None:
                return None

        socket = Socket()

        async def connect(*_args, **_kwargs):
            return socket

        session = client.session()
        try:
            with patch("websockets.connect", new=connect):
                await session.start()
            self.assertEqual([item["event"] for item in socket.sent], ["task_start"])
            self.assertEqual(session.connection_id, 1)
            self.assertTrue(session.reusable)
        finally:
            await session.close()

    async def test_prewarm_only_performs_handshake_and_is_checked_out_once(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid",
            websocket_path="/ws/v1/t2a_v2_bidi",
            model="speech-2.8-hd",
            api_key="test",
            transport="websocket_bidi",
        )
        events: asyncio.Queue[dict] = asyncio.Queue()
        await events.put({"event": "connected_success"})
        await events.put({"event": "task_started"})

        class Socket:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send(self, payload: str) -> None:
                self.sent.append(json.loads(payload))

            async def recv(self) -> str:
                return json.dumps(await events.get())

            async def close(self) -> None:
                return None

        socket = Socket()

        async def connect(*_args, **_kwargs):
            return socket

        try:
            with patch("websockets.connect", new=connect):
                result = await client.prewarm()
            session = client.session()
            self.assertEqual(result["status"], "READY")
            self.assertEqual([item["event"] for item in socket.sent], ["task_start"])
            self.assertTrue(client.checkout_prewarm(session))
            self.assertFalse(client.checkout_prewarm(session))
            self.assertFalse(session._prewarmed)
        finally:
            await client.close()

    async def test_bidi_alignment_events_carry_sentence_pcm_origin(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid",
            websocket_path="/ws/v1/t2a_v2_bidi",
            model="speech-2.8-hd",
            api_key="test",
            transport="websocket_bidi",
        )
        session = client.session()
        events: asyncio.Queue[dict] = asyncio.Queue()

        class Socket:
            async def send(self, _payload: str) -> None:
                return None

            async def recv(self) -> str:
                return json.dumps(await events.get())

            async def close(self) -> None:
                return None

        session._socket = Socket()
        session._started = True
        session._generation_active = True
        session.connection_id = 1
        session._receiver = asyncio.create_task(session._receive_loop())
        try:
            # 6400 bytes at 32 kHz, mono, 16-bit equals 100 ms of returned PCM.
            await events.put({"event": "task_continued", "data": {"audio": "00" * 6400}})
            await events.put({"event": "sentence_start"})
            await events.put({
                "event": "task_continued",
                "data": {
                    "audio": "0100",
                    "subtitle": {
                        "text": "第二",
                        "time_begin": 0,
                        "time_end": 100,
                        "text_begin": 0,
                        "text_end": 2,
                        "timestamped_words": [
                            {"word": "第", "time_begin": 0, "time_end": 50, "word_begin": 0, "word_end": 1},
                        ],
                    },
                },
            })
            for _ in range(100):
                if session.alignment_events:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(len(session.alignment_events), 1)
            alignment = session.alignment_events[0]
            self.assertEqual(alignment["sentence_index"], 1)
            self.assertEqual(alignment["audio_base_ms"], 100)
            self.assertEqual(alignment["audio_returned_ms"], 100)
        finally:
            await session.close()

    async def test_flush_marks_connection_non_reusable_and_drops_late_audio(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid",
            websocket_path="/ws/v1/t2a_v2",
            model="speech-2.8-hd",
            api_key="test",
            transport="websocket_bidi",
            cancel_timeout_ms=500,
        )
        session = client.session()
        self.assertIs(session, client.session())
        events: asyncio.Queue[dict] = asyncio.Queue()

        class Socket:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send(self, payload: str) -> None:
                self.sent.append(json.loads(payload))

            async def recv(self) -> str:
                return json.dumps(await events.get())

            async def close(self) -> None:
                return None

        socket = Socket()
        session._socket = socket
        session._started = True
        session._generation_active = True
        session.connection_id = 1
        session._receiver = asyncio.create_task(session._receive_loop())
        try:
            await session.send_text("第一轮")
            flush_task = asyncio.create_task(session.flush())
            await asyncio.sleep(0)
            await events.put({"event": "task_flushed"})
            await flush_task
            self.assertTrue(session.generation_finished)
            self.assertFalse(session.reusable)
            # Audio after task_flushed is stale until the next task_continue.
            await events.put({"event": "data", "data": {"audio": "0100"}})
            await asyncio.sleep(0)
            self.assertEqual(session.received_bytes, 0)
            self.assertEqual([item["event"] for item in socket.sent], ["task_continue", "task_flush"])
        finally:
            await session.close()

    async def test_2205_retries_only_explicitly_rejected_text(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid", websocket_path="/ws/v1/t2a_v2_bidi",
            model="speech-2.8-hd", api_key="test", transport="websocket_bidi",
        )
        session = client.session()
        events: asyncio.Queue[dict] = asyncio.Queue()

        class Socket:
            def __init__(self) -> None:
                self.sent: list[dict] = []
            async def send(self, payload: str) -> None:
                self.sent.append(json.loads(payload))
            async def recv(self) -> str:
                return json.dumps(await events.get())
            async def close(self) -> None:
                return None

        socket = Socket()
        session._socket = socket
        session._started = True
        session._generation_active = True
        session._receiver = asyncio.create_task(session._receive_loop())
        try:
            await session.send_text("被拒绝片段")
            await events.put({
                "event": "task_failed",
                "base_resp": {"status_code": 2205, "status_msg": "backpressure"},
                "rejected_text": "被拒绝片段",
            })
            for _ in range(100):
                if len(socket.sent) >= 2:
                    break
                await asyncio.sleep(.01)
            self.assertEqual([item.get("text") for item in socket.sent], ["被拒绝片段", "被拒绝片段"])
            flush_task = asyncio.create_task(session.flush())
            await asyncio.sleep(0)
            await events.put({"event": "task_flushed"})
            await flush_task
        finally:
            await session.close()

    async def test_2205_without_current_text_association_is_fatal(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid", websocket_path="/ws/v1/t2a_v2_bidi",
            model="speech-2.8-hd", api_key="test", transport="websocket_bidi",
        )
        session = client.session()
        events: asyncio.Queue[dict] = asyncio.Queue()

        class Socket:
            async def send(self, _payload: str) -> None:
                return None
            async def recv(self) -> str:
                return json.dumps(await events.get())
            async def close(self) -> None:
                return None

        session._socket = Socket()
        session._started = True
        session._generation_active = True
        session._receiver = asyncio.create_task(session._receive_loop())
        try:
            await events.put({
                "event": "task_failed",
                "base_resp": {"status_code": 2205, "status_msg": "backpressure"},
                "rejected_text": "没有发送过",
            })
            await asyncio.wait_for(session._receiver, 1)
            self.assertIn("未关联", str(session._error))
        finally:
            await session.close()

    async def test_2205_repeated_text_is_not_guessed(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid", websocket_path="/ws/v1/t2a_v2_bidi",
            model="speech-2.8-hd", api_key="test", transport="websocket_bidi",
        )
        session = client.session()
        events: asyncio.Queue[dict] = asyncio.Queue()

        class Socket:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            async def send(self, payload: str) -> None:
                self.sent.append(json.loads(payload))

            async def recv(self) -> str:
                return json.dumps(await events.get())

            async def close(self) -> None:
                return None

        socket = Socket()
        session._socket = socket
        session._started = True
        session._generation_active = True
        session._receiver = asyncio.create_task(session._receive_loop())
        try:
            await session.send_text("重复片段")
            await session.send_text("重复片段")
            await events.put({
                "event": "task_failed",
                "base_resp": {"status_code": 2205, "status_msg": "backpressure"},
                "rejected_text": "重复片段",
            })
            await asyncio.wait_for(session._receiver, 1)
            self.assertIn("无法唯一关联", str(session._error))
            self.assertEqual([item.get("text") for item in socket.sent], ["重复片段", "重复片段"])
        finally:
            await session.close()

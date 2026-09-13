from __future__ import annotations

import unittest
import asyncio
import json
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from voice_terminal.config import load_config
from voice_terminal.providers.minimax_tts import MiniMaxTtsError, MiniMaxTtsSession, MiniMaxTtsClient


class MiniMaxTtsAudioTests(unittest.TestCase):
    def test_decode_hex_audio_payload(self) -> None:
        self.assertEqual(MiniMaxTtsSession._decode_audio("0001feff1020"), b"\x00\x01\xfe\xff\x10\x20")

    def test_reject_invalid_hex_audio_payload(self) -> None:
        with self.assertRaises(MiniMaxTtsError):
            MiniMaxTtsSession._decode_audio("not-audio")

    def test_receive_timeout_uses_service_idle_window(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid",
            websocket_path="/ws",
            model="speech-2.8-hd",
            api_key="test",
            timeout_ms=15000,
        )
        self.assertGreaterEqual(client.receive_timeout_seconds, 120)

    def test_start_payload_includes_selected_agent_emotion(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid", websocket_path="/ws", model="speech-2.8-hd",
            api_key="test", transport="websocket_bidi", default_emotion="calm",
        )
        self.assertEqual(client.start_payload("happy")["voice_setting"]["emotion"], "happy")
        self.assertEqual(client.start_payload("not-valid")["voice_setting"]["emotion"], "calm")

    def test_emotion_transport_rejection_disables_extension_and_keeps_neutral_payload(self) -> None:
        client = MiniMaxTtsClient(
            base_url="https://example.invalid", websocket_path="/ws", model="speech-2.8-hd",
            api_key="test", transport="websocket_bidi",
        )
        client.disable_emotion_transport("unknown voice_setting.emotion")
        self.assertEqual(client.emotion_status, "unsupported")
        self.assertEqual(client.applied_emotion, "calm")
        self.assertNotIn("emotion", client.start_payload("happy")["voice_setting"])

    def test_agent_emotion_configuration_is_loaded(self) -> None:
        config = load_config(Path("config/default.yaml"))
        self.assertEqual(config.tts.emotion_mode, "agent")
        self.assertEqual(config.tts.default_emotion, "calm")
        self.assertEqual(config.tts.emotion_transport, "voice_setting")


class MiniMaxTtsProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_finish_close_after_audio_is_failure(self) -> None:
        class ConnectionClosedError(Exception):
            pass

        class Socket:
            async def send(self, _payload):
                raise ConnectionClosedError("no close frame received or sent")

            async def close(self):
                pass

        client = SimpleNamespace()
        session = MiniMaxTtsSession(client)
        session._socket = Socket()
        session._audio_chunks_received = 2
        session._audio_bytes_received = 4096

        with self.assertRaises(ConnectionClosedError):
            await session.finish()
        self.assertTrue(session._finish_requested)
        self.assertFalse(session.server_finished)
        await session.close()

    def session_with_events(self, events):
        class Socket:
            async def recv(self):
                if not events:
                    raise RuntimeError("synthetic transport close")
                value = events.pop(0)
                if isinstance(value, Exception):
                    raise value
                return json.dumps(value)

            async def close(self):
                pass

        session = MiniMaxTtsSession(SimpleNamespace(receive_timeout_seconds=1))
        session._socket = Socket()
        return session

    async def test_fast_receive_spills_and_drains_in_order_without_consumer(self):
        # More than 8 MiB and far more than the old 64-packet capacity.
        packets = [bytes([i % 251]) * 65536 for i in range(132)]
        session = self.session_with_events([
            {"data": {"audio": pcm.hex()}} for pcm in packets
        ] + [{"event": "task_finished"}])
        try:
            await asyncio.wait_for(session._receive_loop(), 5)
            self.assertTrue(session.server_finished)
            self.assertEqual(session.received_bytes, sum(map(len, packets)))
            cache = session._audio
            self.assertTrue(cache._rolled)
            actual = bytearray()
            async for pcm in session.audio_chunks():
                actual.extend(pcm)
                await asyncio.sleep(0)
            self.assertEqual(actual, b"".join(packets))
            self.assertTrue(cache.closed)
        finally:
            await session.close()

    async def test_partial_audio_and_is_final_do_not_hide_disconnect(self):
        session = self.session_with_events([{"data": {"audio": "01000200"}, "is_final": True}])
        session._finish_requested = True
        await session._receive_loop()
        actual = bytearray()
        with self.assertRaisesRegex(MiniMaxTtsError, "task_finished"):
            async for pcm in session.audio_chunks():
                actual.extend(pcm)
        self.assertEqual(actual, b"\x01\x00\x02\x00")
        self.assertFalse(session.server_finished)
        self.assertIsNone(session._audio)
        await session.close()

    async def test_task_failed_preserves_cached_pcm(self):
        session = self.session_with_events([
            {"data": {"audio": "0100"}},
            {"event": "task_failed", "base_resp": {"status_msg": "rate limit"}},
        ])
        await session._receive_loop()
        stream = session.audio_chunks()
        self.assertEqual(await anext(stream), b"\x01\x00")
        with self.assertRaisesRegex(MiniMaxTtsError, "rate limit"):
            await anext(stream)
        await session.close()

    async def test_segment_wait_requires_final_not_partial_audio(self):
        session = self.session_with_events([])
        waiting = asyncio.create_task(session.wait_for_segment(0))
        await asyncio.sleep(0)
        session._store_audio(b"\x01\x00" * 1024)
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        # Segment completion can release pacing without ending the task.
        session._socket = self.session_with_events([
            {"event": "task_continued", "is_final": True},
            {"event": "task_finished"},
        ])._socket
        await session._receive_loop()
        await asyncio.wait_for(waiting, 1)
        self.assertEqual(session.completed_segments, 1)
        await session.close()

    async def test_close_wakes_segment_waiter(self):
        session = self.session_with_events([])
        waiting = asyncio.create_task(session.wait_for_segment(0))
        await asyncio.sleep(0)
        await session.close()
        with self.assertRaisesRegex(MiniMaxTtsError, "分段完成"):
            await asyncio.wait_for(waiting, 1)

    async def test_cache_failure_is_reported(self):
        session = self.session_with_events([{"data": {"audio": "0100"}}])
        with patch("voice_terminal.providers.minimax_tts.tempfile.SpooledTemporaryFile", side_effect=OSError("disk full")):
            await session._receive_loop()
        with self.assertRaisesRegex(MiniMaxTtsError, "缓存写入失败"):
            await anext(session.audio_chunks())
        await session.close()

    async def test_close_wakes_waiting_consumer_and_releases_spill(self):
        session = self.session_with_events([])
        stream = session.audio_chunks()
        waiting = asyncio.create_task(anext(stream, None))
        await asyncio.sleep(0)
        await session.close()
        self.assertIsNone(await asyncio.wait_for(waiting, 1))
        other = self.session_with_events([])
        other._store_audio(b"00" * (5 * 1024 * 1024))
        cache = other._audio
        await other.close()
        self.assertTrue(cache.closed)
        self.assertEqual([pcm async for pcm in other.audio_chunks()], [])

    async def test_local_close_does_not_turn_receiver_cancellation_into_failure(self):
        waiting = asyncio.Event()

        class Socket:
            async def recv(self):
                await waiting.wait()

            async def close(self):
                pass

        session = MiniMaxTtsSession(SimpleNamespace(receive_timeout_seconds=1))
        session._socket = Socket()
        session._receiver = asyncio.create_task(session._receive_loop())
        await asyncio.sleep(0)

        await session.close()

        self.assertTrue(session.receive_finished)
        self.assertIsNone(session._error)

    async def test_flush_close_race_reports_closed_session_without_none_receiver_error(self):
        flush_sent = asyncio.Event()
        block_receive = asyncio.Event()

        class Socket:
            async def send(self, payload):
                if json.loads(payload)["event"] != "task_flush":
                    raise AssertionError("expected task_flush")
                flush_sent.set()

            async def recv(self):
                await block_receive.wait()

            async def close(self):
                return None

        session = MiniMaxTtsSession(SimpleNamespace(
            receive_timeout_seconds=1, transport="websocket_bidi", sample_rate=32000,
        ))
        session._socket = Socket()
        session._started = True
        session._receiver = asyncio.create_task(session._receive_loop())
        try:
            flushing = asyncio.create_task(session.flush())
            await asyncio.wait_for(flush_sent.wait(), 1)
            await session.close()
            with self.assertRaisesRegex(MiniMaxTtsError, "会话已关闭"):
                await asyncio.wait_for(flushing, 1)
        finally:
            if not session.closed:
                await session.close()

    async def test_cancel_close_race_reports_closed_session_without_none_receiver_error(self):
        cancel_sent = asyncio.Event()
        block_receive = asyncio.Event()

        class Socket:
            async def send(self, payload):
                if json.loads(payload)["event"] != "task_cancel":
                    raise AssertionError("expected task_cancel")
                cancel_sent.set()

            async def recv(self):
                await block_receive.wait()

            async def close(self):
                return None

        session = MiniMaxTtsSession(SimpleNamespace(
            receive_timeout_seconds=1, cancel_timeout_seconds=1,
            transport="websocket_bidi", sample_rate=32000,
        ))
        session._socket = Socket()
        session._started = True
        session._receiver = asyncio.create_task(session._receive_loop())
        try:
            cancelling = asyncio.create_task(session.cancel(timeout_seconds=1))
            await asyncio.wait_for(cancel_sent.wait(), 1)
            await session.close()
            with self.assertRaisesRegex(MiniMaxTtsError, "会话已关闭"):
                await asyncio.wait_for(cancelling, 1)
        finally:
            if not session.closed:
                await session.close()

    async def test_unowned_receiver_cancellation_remains_a_failure(self):
        waiting = asyncio.Event()

        class Socket:
            async def recv(self):
                await waiting.wait()

            async def close(self):
                pass

        session = MiniMaxTtsSession(SimpleNamespace(receive_timeout_seconds=1))
        session._socket = Socket()
        receiver = asyncio.create_task(session._receive_loop())
        await asyncio.sleep(0)
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)

        self.assertIsInstance(session._error, MiniMaxTtsError)
        self.assertIn("接收已取消", str(session._error))
        await session.close()

    async def test_final_audio_on_task_finished_is_preserved(self):
        session = self.session_with_events([{"event": "task_finished", "data": {"audio": "0100"}}])
        await session._receive_loop()
        await session.finish()
        self.assertEqual(b"".join([pcm async for pcm in session.audio_chunks()]), b"\x01\x00")
        await session.close()

    async def test_send_text_surfaces_receiver_failure_before_network_write(self) -> None:
        class Receiver:
            def done(self):
                return True

            def cancelled(self):
                return False

            def exception(self):
                return MiniMaxTtsError("receiver failed")

        session = MiniMaxTtsSession(SimpleNamespace())
        session._started = True
        session._socket = object()
        session._receiver = Receiver()
        session._error = MiniMaxTtsError("receiver failed")
        with self.assertRaisesRegex(MiniMaxTtsError, "receiver failed"):
            await session.send_text("你好")

    async def test_confirmed_cancel_makes_bidi_connection_non_reusable(self) -> None:
        """A cancelled Bidi task must not be used for a later speak call."""
        session = MiniMaxTtsSession(SimpleNamespace(
            receive_timeout_seconds=1, transport="websocket_bidi", sample_rate=32000,
        ))

        class Socket:
            calls = 0

            async def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return json.dumps({"event": "task_canceled"})
                raise RuntimeError("synthetic transport close")

            async def close(self):
                pass

        session._socket = Socket()
        await session._receive_loop()
        self.assertTrue(session.generation_finished)
        self.assertTrue(session._connection_dead)
        self.assertFalse(session.reusable)
        await session.close()

    async def test_confirmed_flush_makes_bidi_connection_non_reusable(self) -> None:
        """Each flushed voice output gets an isolated next Bidi session."""
        session = MiniMaxTtsSession(SimpleNamespace(
            receive_timeout_seconds=1, transport="websocket_bidi", sample_rate=32000,
        ))

        class Socket:
            calls = 0

            async def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return json.dumps({"event": "task_flushed"})
                raise RuntimeError("synthetic transport close")

            async def close(self):
                pass

        session._socket = Socket()
        await session._receive_loop()
        self.assertTrue(session.generation_finished)
        self.assertTrue(session._connection_dead)
        self.assertFalse(session.reusable)
        await session.close()

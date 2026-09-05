from __future__ import annotations

import unittest
from types import SimpleNamespace

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


class MiniMaxTtsProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_finish_close_after_audio_is_graceful(self) -> None:
        class ConnectionClosedError(Exception):
            pass

        class Socket:
            async def send(self, _payload):
                raise ConnectionClosedError("no close frame received or sent")

        client = SimpleNamespace()
        session = MiniMaxTtsSession(client)
        session._socket = Socket()
        session._audio_chunks_received = 2
        session._audio_bytes_received = 4096

        await session.finish()
        self.assertTrue(session._finish_requested)

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

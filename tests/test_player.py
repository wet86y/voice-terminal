from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from voice_terminal.audio.player import PcmPlayer


class PcmPlayerTests(unittest.TestCase):
    def test_partial_chunk_is_played_before_following_chunk(self) -> None:
        player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        first_chunk = b"\x01\x00\x02\x00\x03\x00"
        second_chunk = b"\x04\x00\x05\x00"
        player.enqueue("turn_1", first_chunk)
        player.enqueue("turn_1", second_chunk)

        first_output = bytearray(4)
        player._callback(first_output, 2, None, None)
        self.assertEqual(bytes(first_output), b"\x01\x00\x02\x00")

        second_output = bytearray(4)
        player._callback(second_output, 2, None, None)
        self.assertEqual(bytes(second_output), b"\x03\x00\x04\x00")

        third_output = bytearray(4)
        player._callback(third_output, 2, None, None)
        self.assertEqual(bytes(third_output), b"\x05\x00\x00\x00")
        self.assertEqual(player.buffered_ms, 0)

    def test_progress_counts_enqueued_and_callback_consumed_audio(self) -> None:
        player = PcmPlayer(sample_rate=1000, channels=1, output_device=None)
        player.allow("turn_1")
        player.enqueue("turn_1", b"\x01\x00" * 1000)

        before = player.progress("turn_1")
        self.assertEqual(before["enqueued_ms"], 1000)
        self.assertEqual(before["played_ms"], 0)
        self.assertEqual(before["buffered_ms"], 1000)

        player._callback(bytearray(1000), 500, None, None)
        after = player.progress("turn_1")
        self.assertEqual(after["enqueued_ms"], 1000)
        self.assertEqual(after["played_ms"], 500)
        self.assertEqual(after["buffered_ms"], 500)


class PcmPlayerRemoteOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_pause_and_resume_route_to_the_playback_key_device(self) -> None:
        player = PcmPlayer(sample_rate=16_000, channels=1, output_device=None)
        transactions = SimpleNamespace(
            pause_confirmed=AsyncMock(return_value=True),
            resume_confirmed=AsyncMock(return_value=True),
        )
        player.configure_remote_output(
            enqueue=AsyncMock(return_value=True),
            is_bound=lambda key: key == "remote-speech",
            progress=lambda _key: None,
            buffered_ms=lambda _key: 0,
            wait_until_complete=AsyncMock(),
            finish_stream=AsyncMock(),
            transactions=lambda key: transactions if key == "remote-speech" else None,
        )

        self.assertTrue(await player.pause_confirmed("remote-speech"))
        self.assertTrue(await player.resume_confirmed("remote-speech"))
        transactions.pause_confirmed.assert_awaited_once_with("remote-speech")
        transactions.resume_confirmed.assert_awaited_once_with("remote-speech")

    async def test_async_enqueue_uses_remote_route_and_waits_for_device_completion(self) -> None:
        player = PcmPlayer(sample_rate=16_000, channels=1, output_device=None)
        remote_enqueue = AsyncMock(return_value=True)
        remote_finish = AsyncMock(return_value=True)
        remote_wait = AsyncMock(return_value=None)
        player.configure_remote_output(
            enqueue=remote_enqueue,
            is_bound=lambda key: key == "remote-speech",
            progress=lambda key: {
                "key": key, "enqueued_ms": 20, "played_ms": 20, "buffered_ms": 0,
                "output_estimated_ms": 20, "started": True, "precision": "device_time",
                "host_output_time": 10.0,
            },
            buffered_ms=lambda _key: 0,
            wait_until_complete=remote_wait,
            finish_stream=remote_finish,
            transactions=lambda _key: None,
        )
        player.allow("remote-speech")
        await player.enqueue_async("remote-speech", b"\x01\x00" * 320)
        await player.finish_stream_async("remote-speech")
        await player.wait_until_output_complete(timeout_seconds=1.0)

        remote_enqueue.assert_awaited_once_with("remote-speech", 16_000, b"\x01\x00" * 320)
        remote_finish.assert_awaited_once_with("remote-speech")
        remote_wait.assert_awaited_once_with("remote-speech", 1.0)
        self.assertEqual(player._buffered_bytes, 0)
        self.assertEqual(player.progress("remote-speech")["output_estimated_ms"], 20)
        self.assertTrue(player.has_started("remote-speech"))

    async def test_remote_enqueue_failure_never_falls_back_to_local_audio(self) -> None:
        player = PcmPlayer(sample_rate=16_000, channels=1, output_device=None)

        async def fail_remote(_key: str, _rate: int, _pcm: bytes) -> bool:
            raise RuntimeError("media channel lost")

        player.configure_remote_output(
            enqueue=fail_remote,
            is_bound=lambda _key: False,
            progress=lambda _key: None,
            buffered_ms=lambda _key: 0,
            wait_until_complete=AsyncMock(),
            finish_stream=AsyncMock(),
            transactions=lambda _key: None,
        )
        player.allow("remote-speech")
        with self.assertRaisesRegex(RuntimeError, "media channel lost"):
            await player.enqueue_async("remote-speech", b"\x01\x00" * 320)
        self.assertEqual(player._buffered_bytes, 0)


if __name__ == "__main__":
    unittest.main()

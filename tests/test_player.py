from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()

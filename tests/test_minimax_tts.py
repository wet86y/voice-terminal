from __future__ import annotations

import unittest

from voice_terminal.providers.minimax_tts import MiniMaxTtsError, MiniMaxTtsSession


class MiniMaxTtsAudioTests(unittest.TestCase):
    def test_decode_hex_audio_payload(self) -> None:
        self.assertEqual(MiniMaxTtsSession._decode_audio("0001feff1020"), b"\x00\x01\xfe\xff\x10\x20")

    def test_reject_invalid_hex_audio_payload(self) -> None:
        with self.assertRaises(MiniMaxTtsError):
            MiniMaxTtsSession._decode_audio("not-audio")

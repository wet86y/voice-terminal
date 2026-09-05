import base64
import unittest

from voice_terminal.asr.protocol import AudioChunk, decode_audio_chunk


class ProtocolTests(unittest.TestCase):
    def test_audio_chunk_round_trip(self) -> None:
        original = AudioChunk(turn_id="turn_1", pcm=b"\x01\x02", is_final=True)
        decoded = decode_audio_chunk(original.to_dict())
        self.assertEqual(decoded, original)

    def test_invalid_audio_base64_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decode_audio_chunk({"turn_id": "turn_1", "pcm_base64": "not-base64"})


if __name__ == "__main__":
    unittest.main()

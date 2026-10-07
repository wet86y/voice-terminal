from __future__ import annotations

import unittest

from voice_terminal.speaker.client import SpeakerWorkerError
from voice_terminal.speaker.rolling import RollingSpeakerIdentity, RollingSpeakerSensor


SR = 16000


def pcm(samples: int, value: int = 321) -> bytes:
    return (value.to_bytes(2, "little", signed=True)) * samples


class RollingSpeakerSensorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.calls: list[bytes] = []

        async def embed(payload: bytes) -> tuple[float, ...]:
            self.calls.append(payload)
            return (1.0,)

        self.sensor = RollingSpeakerSensor(
            embed,
            lambda _vector: 0.8,
        )

    def test_buffer_is_bounded_and_keeps_absolute_sample_range(self) -> None:
        self.sensor.submit_frame(pcm(SR * 3), start_sample=0, speech=True)
        self.sensor.submit_frame(pcm(SR * 3), start_sample=SR * 3, speech=True)
        self.assertEqual(self.sensor.buffered_samples, SR * 4)
        self.assertEqual(self.sensor.latest_sample, SR * 6)
        self.assertEqual(self.sensor._chunks[0].start_sample, SR * 2)

    def test_gap_clears_audio_that_cannot_be_joined_safely(self) -> None:
        self.sensor.submit_frame(pcm(400), start_sample=0, speech=True)
        self.sensor.submit_frame(pcm(400), start_sample=500, speech=True)
        self.assertEqual(self.sensor.buffered_samples, 400)
        self.assertEqual(self.sensor._chunks[0].start_sample, 500)

    async def test_owner_requires_full_decision_speech_gate(self) -> None:
        # A 700 ms quality-eligible part of the 2 s model window is not enough
        # to authorize an identity decision.
        self.sensor.submit_frame(pcm(SR), start_sample=0, speech=True)
        self.sensor.submit_frame(pcm(SR), start_sample=SR, speech=False)
        snapshot = await self.sensor.analyze()
        self.assertEqual(snapshot.identity, RollingSpeakerIdentity.UNKNOWN)
        self.assertEqual(snapshot.reason, "insufficient_decision_speech")
        self.assertEqual(self.calls, [])

    async def test_continuous_candidate_can_be_owner_and_has_no_audio_or_embedding(self) -> None:
        self.sensor.submit_frame(pcm(SR * 2), start_sample=100, speech=True)
        snapshot = await self.sensor.analyze(
            candidate_start_sample=100, candidate_end_sample=100 + SR * 2,
        )
        self.assertEqual(snapshot.identity, RollingSpeakerIdentity.OWNER)
        self.assertEqual(snapshot.quality, "ready")
        self.assertEqual(snapshot.analysis_start_sample, 100)
        self.assertEqual(snapshot.analysis_end_sample, 100 + SR * 2)
        self.assertTrue(snapshot.covers_candidate(100, 100 + SR * 2))
        self.assertFalse(hasattr(snapshot, "pcm"))
        self.assertFalse(hasattr(snapshot, "embedding"))
        self.assertEqual(len(self.calls), 1)

    async def test_candidate_window_never_uses_audio_before_candidate_start(self) -> None:
        self.sensor.submit_frame(pcm(SR * 2), start_sample=0, speech=True)
        result = await self.sensor.analyze(
            candidate_start_sample=SR, candidate_end_sample=SR * 2,
        )
        self.assertEqual(result.identity, RollingSpeakerIdentity.UNKNOWN)
        self.assertEqual(result.reason, "window_unavailable")
        self.assertEqual(self.calls, [])

    async def test_candidate_can_use_its_recent_two_seconds_without_neighbor_audio(self) -> None:
        self.sensor.submit_frame(pcm(SR), start_sample=0, speech=False)
        self.sensor.submit_frame(pcm(SR * 3), start_sample=SR, speech=True)
        result = await self.sensor.analyze(
            candidate_start_sample=SR, candidate_end_sample=SR * 4,
        )
        self.assertEqual(result.identity, RollingSpeakerIdentity.OWNER)
        self.assertEqual((result.analysis_start_sample, result.analysis_end_sample), (SR * 2, SR * 4))
        self.assertTrue(result.covers_candidate(SR, SR * 4))

    async def test_decision_speech_accumulates_across_candidate_clipped_history(self) -> None:
        # Candidate spans 2.5 s with a 500 ms pause. The 2 s model window
        # contains 1.5 s speech, while the candidate history contains 2 s.
        self.sensor.submit_frame(pcm(SR), start_sample=0, speech=True)
        self.sensor.submit_frame(pcm(SR // 2), start_sample=SR, speech=False)
        self.sensor.submit_frame(pcm(SR), start_sample=SR + SR // 2, speech=True)
        result = await self.sensor.analyze(
            candidate_start_sample=0, candidate_end_sample=SR * 2 + SR // 2,
        )
        self.assertEqual(result.identity, RollingSpeakerIdentity.OWNER)
        self.assertEqual((result.analysis_start_sample, result.analysis_end_sample),
                         (SR // 2, SR * 2 + SR // 2))

    async def test_non_owner_uses_continuity_cutoff_and_middle_is_unknown(self) -> None:
        self.sensor.submit_frame(pcm(SR * 2), start_sample=0, speech=True)
        self.sensor.score_embedding = lambda _vector: 0.49
        self.assertEqual((await self.sensor.analyze()).identity, RollingSpeakerIdentity.NON_OWNER)
        self.sensor.score_embedding = lambda _vector: 0.55
        self.assertEqual((await self.sensor.analyze()).identity, RollingSpeakerIdentity.UNKNOWN)

    async def test_inference_failure_fails_open_and_snapshot_expires(self) -> None:
        async def fail(_payload: bytes) -> tuple[float, ...]:
            raise RuntimeError("private exception details")

        self.sensor.embed_pcm = fail
        self.sensor.submit_frame(pcm(SR * 2), start_sample=0, speech=True)
        with self.assertLogs("voice_terminal.speaker.rolling", level="WARNING") as logs:
            result = await self.sensor.analyze(completed_at=10.0)
        self.assertIn("RuntimeError: private exception details", "\n".join(logs.output))
        self.assertIn("start_sample=0 end_sample=32000", "\n".join(logs.output))
        self.assertEqual(result.identity, RollingSpeakerIdentity.UNKNOWN)
        self.assertEqual(result.reason, "inference_failed")
        self.assertFalse(result.is_fresh(now=11.0, max_age_ms=800))

    async def test_audio_quality_rejection_is_not_an_inference_failure(self) -> None:
        """音频质量不满足条件保留具体标签，不误报为模型或通信故障。"""
        async def reject(_payload: bytes) -> tuple[float, ...]:
            raise SpeakerWorkerError("录音音量过低", code="SPEAKER_AUDIO_QUALITY", quality="too_quiet")

        self.sensor.embed_pcm = reject
        self.sensor.submit_frame(pcm(SR * 2), start_sample=0, speech=True)
        result = await self.sensor.analyze()
        self.assertEqual(result.identity, RollingSpeakerIdentity.UNKNOWN)
        self.assertEqual(result.quality, "too_quiet")
        self.assertEqual(result.reason, "audio_quality_rejected")

    async def test_missing_vad_does_not_authorize_identity(self) -> None:
        self.sensor.submit_frame(pcm(SR * 2), start_sample=0, speech=None)
        result = await self.sensor.analyze()
        self.assertEqual(result.identity, RollingSpeakerIdentity.UNKNOWN)
        self.assertEqual(result.reason, "vad_unavailable")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

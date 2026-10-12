from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

from voice_terminal.session.ambient import AmbientTranscriptStore
from voice_terminal.speaker.contracts import SpeakerObservation, SpeakerState
from voice_terminal.speaker.enrollment import (
    EnrollmentRejectedError,
    centroid_of,
    check_segments,
)
from voice_terminal.speaker.protocol import decode_pcm, encode_pcm
from voice_terminal.speaker.registry import SpeakerProfileStore
from voice_terminal.speaker.state import SpeakerStateMachine
from voice_terminal.speaker.worker import SpeakerWorkerEngine


def observation(score: float | None, *, turn_id: str = "turn", generation: int = 1,
                speech_ms: int = 1500, quality: str = "ready",
                speech_ratio: float | None = None) -> SpeakerObservation:
    return SpeakerObservation(
        turn_id=turn_id, generation=generation,
        window_started_sample=0, window_ended_sample=24000,
        similarity=score, speech_ms=speech_ms, quality=quality,
        speech_ratio=speech_ratio,
    )


class _FakeEmbeddingModel:
    model_version = "fake-model"

    def embed_pcm(self, _pcm: bytes) -> tuple[float, ...]:
        return (1.0, 0.0, 0.0)


class SpeakerStateTests(unittest.TestCase):
    """Turn-level aggregation replaced the per-window hysteresis."""

    def setUp(self) -> None:
        self.machine = SpeakerStateMachine()
        self.machine.reset(turn_id="turn", generation=1)

    def test_binary_boundaries_and_unavailable_never_authorize_owner(self) -> None:
        self.assertEqual(self.machine.decision().state, SpeakerState.UNKNOWN)
        self.machine.observe(observation(0.60))
        self.assertEqual(self.machine.decision().state, SpeakerState.OWNER_CONFIRMED)
        self.machine.mark_unavailable("model_failure")
        decision = self.machine.decision()
        self.assertEqual(decision.state, SpeakerState.UNAVAILABLE)
        self.assertEqual(decision.route, "pending")
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.reason, "model_failure")
        self.assertIsNone(self.machine._last_owner_confirmed_at)

    def test_continuity_clock_only_refreshes_for_new_eligible_evidence(self) -> None:
        with patch("voice_terminal.speaker.state.time.monotonic", return_value=100.0):
            self.machine.observe(observation(0.8))
        with patch("voice_terminal.speaker.state.time.monotonic", return_value=103.0):
            self.machine.decision()
            self.machine.observe(observation(None, quality="insufficient_speech"))
        self.assertEqual(self.machine._last_owner_confirmed_at, 100.0)
        self.machine.reset(turn_id="next", generation=2)
        with patch("voice_terminal.speaker.state.time.monotonic", return_value=106.0):
            self.machine.observe(observation(0.55, turn_id="next", generation=2))
            self.assertEqual(self.machine.decision().state, SpeakerState.NON_OWNER_CONFIRMED)

    def test_new_valid_evidence_refreshes_after_observation_tail_is_full(self) -> None:
        with patch("voice_terminal.speaker.state.time.monotonic", return_value=100.0):
            for index in range(64):
                self.machine.observe(replace(observation(0.8), window_ended_sample=24000 + index * 8000))
        with patch("voice_terminal.speaker.state.time.monotonic", return_value=103.0):
            self.machine.observe(replace(observation(0.8), window_ended_sample=600000))
        self.assertEqual(len(self.machine.observations), 64)
        self.assertEqual(self.machine._last_owner_confirmed_at, 103.0)

    def test_owner_confirmed_when_the_turn_aggregate_clears_the_threshold(self) -> None:
        self.assertEqual(self.machine.observe(observation(0.72)), SpeakerState.OWNER_CONFIRMED)
        self.assertEqual(self.machine.observe(observation(0.68)), SpeakerState.OWNER_CONFIRMED)
        decision = self.machine.decision()
        self.assertEqual(decision.state, SpeakerState.OWNER_CONFIRMED)
        self.assertEqual(decision.route, "main")
        self.assertAlmostEqual(decision.similarity, 0.70, places=6)
        self.assertEqual(decision.aggregated_windows, 2)

    def test_ambiguous_turn_never_confirms_on_its_own(self) -> None:
        # 低于主人阈值且没有连续主人证据，二分判断为非主人。
        self.assertEqual(self.machine.observe(observation(0.55)), SpeakerState.NON_OWNER_CONFIRMED)
        self.assertEqual(self.machine.decision().route, "ambient")

    def test_continuity_requires_a_previous_owner_confirmation(self) -> None:
        self.machine.observe(observation(0.80))
        self.assertEqual(self.machine.decision().state, SpeakerState.OWNER_CONFIRMED)
        # A follow-up turn with weaker evidence keeps the conversation alive...
        self.machine.reset(turn_id="turn-2", generation=2)
        self.assertEqual(self.machine.observe(observation(0.55, turn_id="turn-2", generation=2)),
                         SpeakerState.OWNER_CONTINUITY)
        self.assertEqual(self.machine.decision().route, "main")
        # ...but only while the confirmation is recent.
        self.machine.clear_continuity()
        self.assertEqual(self.machine.decision().state, SpeakerState.NON_OWNER_CONFIRMED)

    def test_low_aggregate_rejects_and_clears_continuity(self) -> None:
        self.machine.observe(observation(0.80))
        self.machine.reset(turn_id="turn-2", generation=2)
        self.assertEqual(self.machine.observe(observation(0.10, turn_id="turn-2", generation=2)),
                         SpeakerState.NON_OWNER_CONFIRMED)
        self.assertEqual(self.machine.decision().route, "ambient")
        # The rejection drops the owner trust, so the next ambiguous turn
        # cannot inherit continuity from the older confirmation.
        self.machine.reset(turn_id="turn-3", generation=3)
        self.assertEqual(self.machine.observe(observation(0.55, turn_id="turn-3", generation=3)),
                         SpeakerState.NON_OWNER_CONFIRMED)

    def test_short_input_can_confirm_without_whole_turn_wait(self) -> None:
        # 有效短音频直接计算，取消旧整轮人声门槛。
        self.assertEqual(self.machine.observe(observation(0.99, speech_ms=400)), SpeakerState.OWNER_CONFIRMED)
        decision = self.machine.decision()
        self.assertEqual(decision.state, SpeakerState.OWNER_CONFIRMED)
        self.assertEqual(decision.route, "main")
        self.assertEqual(decision.aggregated_windows, 1)

    def test_short_or_stale_observations_never_confirm(self) -> None:
        machine = SpeakerStateMachine()
        machine.reset(turn_id="turn", generation=3)
        self.assertEqual(machine.observe(observation(0.99, generation=3, quality="not_speech")),
                         SpeakerState.UNKNOWN)
        self.assertEqual(machine.observe(observation(0.99, generation=2)), SpeakerState.UNKNOWN)
        self.assertEqual(machine.decision().observation_count, 1)

    def test_speech_weighted_aggregate_prefers_the_longer_window(self) -> None:
        self.machine.observe(observation(0.90, speech_ms=3000))
        self.machine.observe(observation(0.20, speech_ms=1000))
        # (0.90*3000 + 0.20*1000) / 4000 = 0.725
        self.assertAlmostEqual(self.machine.decision().similarity, 0.725, places=6)

    def test_aggregate_embedding_is_used_when_a_scorer_is_supplied(self) -> None:
        machine = SpeakerStateMachine(scorer=lambda aggregate: 0.91)
        machine.reset(turn_id="turn", generation=1)
        machine.observe(observation(0.10))
        self.assertEqual(machine.decision().state, SpeakerState.OWNER_CONFIRMED)


class SpeakerWorkerTests(unittest.TestCase):
    def test_sliding_window_emits_once_per_step_and_flushes_short_turn(self) -> None:
        worker = SpeakerWorkerEngine(
            Path("missing.onnx"), sample_rate=16000,
            window_ms=1500, step_ms=500, min_speech_ms=500,
        )
        worker.model = _FakeEmbeddingModel()
        first = (b"\x00\x10") * 24000
        result = worker.feed({
            "turn_id": "turn", "generation": 1, "start_sample": 0,
            "end_sample": 24000, "pcm_base64": encode_pcm(first),
        })
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["window_started_sample"], result[0]["window_ended_sample"]), (0, 24000))
        second = (b"\x00\x10") * 8000
        result = worker.feed({
            "turn_id": "turn", "generation": 1, "start_sample": 24000,
            "end_sample": 32000, "pcm_base64": encode_pcm(second),
        })
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["window_started_sample"], result[0]["window_ended_sample"]), (8000, 32000))
        worker.finish("turn", 1)
        # Once the semantic boundary closes a generation, a frame that was
        # still queued behind the finish command cannot recreate its buffer.
        self.assertEqual(worker.feed({
            "turn_id": "turn", "generation": 1, "start_sample": 32000,
            "end_sample": 40000, "pcm_base64": encode_pcm(second),
        }), [])
        worker.finish("future", 2)
        self.assertEqual(worker.feed({
            "turn_id": "future", "generation": 1, "start_sample": 0,
            "end_sample": 8000, "pcm_base64": encode_pcm(second),
        }), [])

        tail = SpeakerWorkerEngine(
            Path("missing.onnx"), sample_rate=16000,
            window_ms=1500, step_ms=500, min_speech_ms=500,
        )
        tail.model = _FakeEmbeddingModel()
        self.assertEqual(len(tail.feed({
            "turn_id": "tail", "generation": 1, "start_sample": 0,
            "end_sample": 25600, "pcm_base64": encode_pcm((b"\x00\x10") * 25600),
        })), 1)
        flushed_full = tail.finish("tail", 1)
        self.assertEqual(len(flushed_full), 1)
        self.assertEqual(flushed_full[0]["window_ended_sample"], 25600)

        short = SpeakerWorkerEngine(
            Path("missing.onnx"), sample_rate=16000,
            window_ms=1500, step_ms=500, min_speech_ms=500,
        )
        short.model = _FakeEmbeddingModel()
        pcm = (b"\x00\x10") * 3200  # 200 ms
        self.assertEqual(short.feed({
            "turn_id": "short", "generation": 1, "start_sample": 0,
            "end_sample": 3200, "pcm_base64": encode_pcm(pcm),
        }), [])
        flushed = short.finish("short", 1)
        self.assertEqual(len(flushed), 1)
        self.assertEqual(flushed[0]["quality"], "insufficient_audio")
        self.assertEqual(flushed[0]["embedding"], [])

    def test_pcm_protocol_rejects_empty_and_odd_payloads(self) -> None:
        with self.assertRaises(ValueError):
            decode_pcm(encode_pcm(b""))
        with self.assertRaises(ValueError):
            decode_pcm(encode_pcm(b"\x00"))

    def test_vad_verdict_gates_windows_on_speech_ratio(self) -> None:
        """Noise-only windows must not produce an embedding at all."""
        loud = (b"\x00\x10") * 32000  # loud enough to pass the energy gate
        engine = SpeakerWorkerEngine(
            Path("missing.onnx"), sample_rate=16000,
            window_ms=2000, step_ms=1000, min_speech_ms=700,
            min_window_speech_ratio=0.40,
        )
        engine.model = _FakeEmbeddingModel()

        def feed(start: int) -> list[dict]:
            return engine.feed({
                "turn_id": "turn", "generation": 1, "start_sample": start,
                "end_sample": start + 16000, "pcm_base64": encode_pcm(loud[:32000]),
                "speech": False,
            })

        # Every frame marked as non-speech -> the window is explicitly rejected.
        emitted = feed(0) + feed(16000)
        self.assertTrue(emitted)
        for item in emitted:
            self.assertEqual(item["quality"], "not_speech")
            self.assertEqual(item["embedding"], [])
            self.assertEqual(item["speech_ms"], 0)

    def test_vad_speech_ratio_reports_speech_duration(self) -> None:
        engine = SpeakerWorkerEngine(
            Path("missing.onnx"), sample_rate=16000,
            window_ms=2000, step_ms=1000, min_speech_ms=700,
            min_window_speech_ratio=0.40,
        )
        engine.model = _FakeEmbeddingModel()
        pcm = (b"\x00\x10") * 16000
        emitted = []
        for start in range(0, 32000, 16000):
            emitted += engine.feed({
                "turn_id": "turn", "generation": 1, "start_sample": start,
                "end_sample": start + 16000, "pcm_base64": encode_pcm(pcm),
                "speech": True,
            })
        self.assertTrue(emitted)
        for item in emitted:
            self.assertEqual(item["quality"], "ready")
            self.assertEqual(item["speech_ms"], 2000)
            self.assertAlmostEqual(item["speech_ratio"], 1.0, places=4)
            self.assertEqual(len(item["embedding"]), 3)

    def test_without_vad_verdicts_the_energy_fallback_still_works(self) -> None:
        engine = SpeakerWorkerEngine(
            Path("missing.onnx"), sample_rate=16000,
            window_ms=2000, step_ms=1000, min_speech_ms=700,
        )
        engine.model = _FakeEmbeddingModel()
        emitted = engine.feed({
            "turn_id": "turn", "generation": 1, "start_sample": 0,
            "end_sample": 32000, "pcm_base64": encode_pcm((b"\x00\x10") * 32000),
        })
        self.assertTrue(emitted)
        self.assertEqual(emitted[0]["quality"], "ready")
        self.assertIsNone(emitted[0]["speech_ratio"])


class SpeakerStorageTests(unittest.TestCase):
    def test_profile_is_atomic_single_user_and_rebuilds_centroid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SpeakerProfileStore(Path(directory), model_id="test-model")
            result = store.replace_templates([
                ("正常朗读", (1.0, 0.0, 0.0), {"speech_ms": 5000}),
                ("自然说话", (0.9, 0.1, 0.0), {"speech_ms": 5000}),
                ("夹嗓子", (0.8, 0.2, 0.0), {"speech_ms": 5000}),
            ])
            self.assertTrue(result["available"])
            self.assertEqual(result["template_count"], 3)
            self.assertEqual(result["vector_dim"], 3)
            self.assertEqual(len(store.vectors()), 4)  # centroid plus templates
            template_id = result["templates"][0]["template_id"]
            toggled = store.set_template_centroid(template_id, False)
            self.assertTrue(toggled["available"])
            self.assertFalse(next(item for item in toggled["templates"] if item["template_id"] == template_id)["include_in_centroid"])
            remaining = [item["template_id"] for item in toggled["templates"] if item["template_id"] != template_id]
            store.set_template_centroid(remaining[0], False)
            with self.assertRaises(ValueError):
                store.set_template_centroid(remaining[1], False)
            store.set_template_centroid(template_id, True)
            store.delete_template(template_id)
            self.assertEqual(store.snapshot()["template_count"], 2)
            self.assertTrue(store.path.is_file())

    def test_ambient_context_is_bounded_and_refinement_stays_session_local(self) -> None:
        store = AmbientTranscriptStore(max_records=2, max_bytes=4096)
        store.append({"turn_id": "one", "text": "环境一", "similarity": 0.1, "reason": "non-owner"})
        store.append({"turn_id": "two", "text": "环境二", "similarity": 0.2, "reason": "non-owner"})
        self.assertTrue(store.update_refined("one", "环境一精修"))
        store.append({"turn_id": "three", "text": "环境三", "similarity": 0.3, "reason": "non-owner"})
        snapshot = store.snapshot()
        self.assertEqual(snapshot["total_count"], 2)
        self.assertTrue(snapshot["session_scoped"])
        self.assertEqual(snapshot["records"][0]["turn_id"], "two")


class EnrollmentGateTests(unittest.TestCase):
    def _segments(self, speech_ms: int = 5000) -> list[dict]:
        return [{"speech_ms": speech_ms} for _ in range(3)]

    def test_accepts_consistent_segments(self) -> None:
        detail = check_segments(
            self._segments(),
            [(1.0, 0.0, 0.0), (0.98, 0.1, 0.0), (0.97, 0.0, 0.2)],
        )
        self.assertGreater(detail["min_similarity"], 0.9)

    def test_rejects_segments_that_disagree_about_the_speaker(self) -> None:
        with self.assertRaises(EnrollmentRejectedError) as raised:
            check_segments(
                self._segments(),
                [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            )
        self.assertIn("不像同一个人", str(raised.exception))
        self.assertEqual(raised.exception.detail["min_pair"], [0, 1])

    def test_rejects_segments_with_too_little_speech(self) -> None:
        with self.assertRaises(EnrollmentRejectedError) as raised:
            check_segments(
                [{"speech_ms": 5000}, {"speech_ms": 1200}, {"speech_ms": 5000}],
                [(1.0, 0.0, 0.0), (0.99, 0.0, 0.0), (0.98, 0.0, 0.0)],
            )
        self.assertEqual(raised.exception.detail["short_segments"], [1])

    def test_centroid_is_normalised_and_rejects_degenerate_input(self) -> None:
        centre = centroid_of([(1.0, 0.0, 0.0), (1.0, 0.0, 0.0)])
        self.assertAlmostEqual(sum(x * x for x in centre), 1.0, places=6)
        with self.assertRaises(EnrollmentRejectedError):
            centroid_of([(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)])


class SpeakerProfileFingerprintTests(unittest.TestCase):
    """Templates must never be scored against a different feature frontend."""

    def _store(self) -> SpeakerProfileStore:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        return SpeakerProfileStore(Path(self.temporary.name), model_id="test-model")

    def _enroll(self, store: SpeakerProfileStore, *, probe: tuple[float, ...] | None = None,
                frontend: str = "frontend-a") -> None:
        store.replace_templates(
            [("第一段", (1.0, 0.0, 0.0), {"speech_ms": 5000}),
             ("第二段", (0.95, 0.1, 0.0), {"speech_ms": 5000})],
            fingerprint={"frontend_version": frontend, "model_id": "test-model"},
            probe=probe or (1.0, 0.0),
        )

    def test_matching_fingerprint_keeps_the_profile_available(self) -> None:
        store = self._store()
        self._enroll(store, probe=(1.0, 0.0))
        store.set_runtime_fingerprint({"frontend_version": "frontend-a", "model_id": "test-model"},
                                      (1.0, 0.0))
        self.assertFalse(store.stale)
        self.assertTrue(store.available)
        self.assertFalse(store.snapshot()["needs_reenrollment"])

    def test_changed_frontend_version_marks_the_profile_stale(self) -> None:
        store = self._store()
        self._enroll(store, frontend="frontend-a")
        store.set_runtime_fingerprint({"frontend_version": "frontend-b", "model_id": "test-model"})
        self.assertTrue(store.stale)
        self.assertEqual(store.stale_reason, "feature_frontend_changed")
        self.assertFalse(store.available)

    def test_numeric_frontend_drift_is_detected_by_the_probe(self) -> None:
        store = self._store()
        self._enroll(store, probe=(1.0, 0.0))
        # Same declared version, materially different embedding: exactly the
        # failure a version string alone cannot catch.
        store.set_runtime_fingerprint({"frontend_version": "frontend-a", "model_id": "test-model"},
                                      (0.0, 1.0))
        self.assertTrue(store.stale)
        self.assertEqual(store.stale_reason, "feature_numeric_mismatch")
        self.assertFalse(store.available)

    def test_changed_model_marks_the_profile_stale(self) -> None:
        store = self._store()
        self._enroll(store)
        store.set_runtime_fingerprint({"frontend_version": "frontend-a", "model_id": "other-model"})
        self.assertTrue(store.stale)
        self.assertEqual(store.stale_reason, "speaker_model_changed")

    def test_schema_one_profile_is_never_scored(self) -> None:
        store = self._store()
        self._enroll(store)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["schema_version"] = 1
        raw.pop("feature_fingerprint", None)
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        reloaded = SpeakerProfileStore(Path(self.temporary.name), model_id="test-model")
        self.assertTrue(reloaded.stale)
        self.assertEqual(reloaded.stale_reason, "profile_schema_upgraded")
        self.assertFalse(reloaded.available)
        # The vectors survive for inspection; they are simply not used.
        self.assertEqual(len(reloaded.templates), 2)

    def test_profile_without_a_runtime_fingerprint_stays_usable(self) -> None:
        """Offline tooling and unit fixtures must not be forced stale."""
        store = self._store()
        self._enroll(store)
        self.assertFalse(store.stale)
        self.assertTrue(store.available)

    def test_centroid_template_count_is_reported(self) -> None:
        store = self._store()
        self._enroll(store)
        self.assertEqual(store.snapshot()["centroid_template_count"], 2)
        template_id = next(iter(store.templates))
        store.set_template_centroid(template_id, False)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["centroid_template_count"], 1)
        self.assertIsNotNone(snapshot["warning"])


if __name__ == "__main__":
    unittest.main()

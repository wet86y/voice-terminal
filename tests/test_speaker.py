from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voice_terminal.session.ambient import AmbientTranscriptStore
from voice_terminal.speaker.contracts import SpeakerObservation, SpeakerState
from voice_terminal.speaker.protocol import decode_pcm, encode_pcm
from voice_terminal.speaker.registry import SpeakerProfileStore
from voice_terminal.speaker.state import SpeakerStateMachine
from voice_terminal.speaker.worker import SpeakerWorkerEngine


def observation(score: float | None, *, turn_id: str = "turn", generation: int = 1,
                speech_ms: int = 1500, quality: str = "ready") -> SpeakerObservation:
    return SpeakerObservation(
        turn_id=turn_id, generation=generation,
        window_started_sample=0, window_ended_sample=24000,
        similarity=score, speech_ms=speech_ms, quality=quality,
    )


class _FakeEmbeddingModel:
    model_version = "fake-model"

    def embed_pcm(self, _pcm: bytes) -> tuple[float, ...]:
        return (1.0, 0.0, 0.0)


class SpeakerStateTests(unittest.TestCase):
    def test_owner_hysteresis_and_non_owner_confirmation(self) -> None:
        machine = SpeakerStateMachine()
        machine.reset(turn_id="turn", generation=1)
        self.assertEqual(machine.observe(observation(0.55)), SpeakerState.OWNER_CONFIRMED)
        self.assertEqual(machine.observe(observation(0.45)), SpeakerState.OWNER_CONTINUITY)
        self.assertEqual(machine.observe(observation(0.35)), SpeakerState.UNKNOWN)
        self.assertEqual(machine.observe(observation(0.35)), SpeakerState.NON_OWNER_CONFIRMED)
        self.assertEqual(machine.decision().similarity, 0.35)

    def test_short_or_stale_observations_never_confirm(self) -> None:
        machine = SpeakerStateMachine()
        machine.reset(turn_id="turn", generation=3)
        self.assertEqual(machine.observe(observation(0.99, generation=3, speech_ms=200)), SpeakerState.UNKNOWN)
        self.assertEqual(machine.observe(observation(0.99, generation=2)), SpeakerState.UNKNOWN)
        self.assertEqual(machine.decision().observation_count, 1)


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


if __name__ == "__main__":
    unittest.main()

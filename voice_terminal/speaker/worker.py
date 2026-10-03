from __future__ import annotations

from ..resources import bootstrap_resources, process_resource_status
if __name__ == "__main__":
    bootstrap_resources("speaker")

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from .engine import InsufficientAudioError, WeSpeakerOnnxEngine
from .quality import base_window_quality
from .protocol import decode_pcm
from ..config import load_config
from ..core.events import utc_now_iso
from ..env import load_env_file
from ..logging_setup import configure_logging
from ..server.product import ProductSettingsStore
from ..compute import process_memory_bytes

logger = logging.getLogger(__name__)


class SpeakerWorkerEngine:
    def __init__(self, model_path: Path, *, sample_rate: int = 16000,
                 window_ms: int = 2000, step_ms: int = 500,
                 min_speech_ms: int = 700,
                 min_window_speech_ratio: float = 0.40,
                 device: str = "cpu", fallback_device: str = "cpu") -> None:
        self.model_path = model_path
        self.model = WeSpeakerOnnxEngine(
            model_path, sample_rate=sample_rate,
            device=device, fallback_device=fallback_device,
        )
        self.sample_rate = sample_rate
        self.window_samples = sample_rate * window_ms // 1000
        self.step_samples = sample_rate * step_ms // 1000
        self.window_ms = int(window_ms)
        self.min_speech_ms = max(1, int(min_speech_ms))
        self.min_window_speech_ratio = min(1.0, max(0.0, float(min_window_speech_ratio)))
        self.buffers: dict[tuple[str, int], bytearray] = {}
        from ..resources import registry
        registry.observe(self, "speaker_window_pcm", lambda value: sum(len(p) for p in value.buffers.values()))
        self.starts: dict[tuple[str, int], int] = {}
        self.last_emitted: dict[tuple[str, int], int] = {}
        # Per-window VAD speech bookkeeping: absolute sample index of every
        # frame the controller marked as speech, so a window's speech duration
        # is measured from the upstream VAD instead of a fixed amplitude gate.
        self.speech_ranges: dict[tuple[str, int], list[tuple[int, int]]] = {}
        # Merged VAD ranges for the full turn.  The rolling window ranges above
        # are pruned; this compact list preserves distinct turn speech time so
        # overlapping windows cannot inflate the minimum-turn gate.
        self.turn_speech_ranges: dict[tuple[str, int], list[tuple[int, int]]] = {}
        # Turns for which the caller supplied an explicit VAD verdict on at
        # least one frame.  Distinguishing "no speech" from "no verdict" is
        # what lets a noise-only window be rejected outright instead of
        # silently falling back to the energy heuristic.
        self.vad_tracked: set[tuple[str, int]] = set()
        self.latest_generation: dict[str, int] = {}
        # A finish command closes a turn generation.  Frames that were still
        # queued when the 150 ms budget expired must not recreate that old
        # worker buffer after the semantic boundary has already passed.
        self.finished: dict[tuple[str, int], None] = {}

    def load(self) -> None:
        self.model.load()

    @property
    def model_version(self) -> str:
        return self.model.model_version

    def _quality(self, pcm: bytes, *, vad_measured: bool = False) -> tuple[str, int, bool]:
        """Return ``(quality, speech_ms, vad_supplied)``.

        ``speech_ms`` is only an energy-based estimate.  When the controller
        supplies a VAD verdict per frame the window-level speech duration is
        computed from it instead (see :meth:`_window_speech_ranges`), because a fixed
        absolute amplitude threshold drifts with microphone gain and counts
        non-speech energy as speech.
        """
        quality, speech_ms = base_window_quality(
            pcm, sample_rate=self.sample_rate, min_speech_ms=self.min_speech_ms,
            vad_measured=vad_measured,
        )
        return quality, speech_ms, False

    @staticmethod
    def _append_range(ranges: list[tuple[int, int]], start: int, end: int) -> None:
        """Add an interval while merging duplicate, overlapping, or adjacent frames."""
        if end <= start:
            return
        index = 0
        while index < len(ranges) and ranges[index][1] < start:
            index += 1
        while index < len(ranges) and ranges[index][0] <= end:
            previous_start, previous_end = ranges.pop(index)
            start = min(start, previous_start)
            end = max(end, previous_end)
        ranges.insert(index, (start, end))

    def _window_speech_ranges(self, key: tuple[str, int], start: int,
                              end: int) -> tuple[list[tuple[int, int]], bool]:
        """Merged voiced sample ranges intersecting ``[start, end)``."""
        if key not in self.vad_tracked:
            return [], False
        result: list[tuple[int, int]] = []
        for frame_start, frame_end in self.speech_ranges.get(key, ()):
            overlap_start = max(start, frame_start)
            overlap_end = min(end, frame_end)
            self._append_range(result, overlap_start, overlap_end)
        return result, True

    def _turn_speech_ms(self, key: tuple[str, int], through_sample: int) -> int | None:
        """Distinct VAD speech time from the turn start through ``through_sample``."""
        if key not in self.vad_tracked:
            return None
        total_samples = sum(
            max(0, min(through_sample, frame_end) - frame_start)
            for frame_start, frame_end in self.turn_speech_ranges.get(key, ())
            if frame_start < through_sample
        )
        return int(total_samples * 1000 / self.sample_rate)

    def _observation(self, *, turn_id: str, generation: int, start: int, end: int,
                     pcm: bytes) -> dict[str, Any]:
        key = (turn_id, generation)
        speech_ranges, vad_supplied = self._window_speech_ranges(key, start, end)
        quality, fallback_speech_ms, _ = self._quality(pcm, vad_measured=vad_supplied)
        if vad_supplied:
            speech_samples = sum(range_end - range_start for range_start, range_end in speech_ranges)
            speech_ms = int(speech_samples * 1000 / self.sample_rate)
            window_ms = max(1, self.window_samples * 1000 // self.sample_rate)
            speech_ratio: float | None = round(min(1.0, speech_ms / window_ms), 4)
            if quality == "ready" and speech_ratio < self.min_window_speech_ratio:
                # A window made mostly of noise or another source must not
                # produce an embedding, otherwise background audio would be
                # scored as if it were the enrolled speaker.
                quality = "not_speech"
            elif quality == "ready" and speech_ms < self.min_speech_ms:
                quality = "insufficient_audio"
        else:
            speech_ms = fallback_speech_ms
            speech_ratio = None
        turn_speech_ms = self._turn_speech_ms(key, end)
        payload: dict[str, Any] = {
            "type": "speaker.embedding",
            "turn_id": turn_id,
            "generation": generation,
            "window_started_sample": start,
            "window_ended_sample": end,
            # The sidecar does not own the user's template registry, so the
            # initial payload leaves matching fields empty.  Voice Server
            # fills them after comparing the embedding with its centre.
            "similarity": None,
            "matched_template_id": None,
            "speech_ms": speech_ms,
            "speech_ratio": speech_ratio,
            "turn_speech_ms": turn_speech_ms,
            "quality": quality,
            "model_version": self.model_version,
            "timestamp": utc_now_iso(),
            "embedding": [],
        }
        if quality == "ready":
            try:
                payload["embedding"] = list(self.model.embed_pcm(pcm))
            except InsufficientAudioError:
                # Never let the graph's NaN path reach the decision layer.
                payload["quality"] = "insufficient_audio"
        return payload

    def feed(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        turn_id = payload.get("turn_id")
        generation = payload.get("generation")
        start_sample = payload.get("start_sample")
        end_sample = payload.get("end_sample")
        if (not isinstance(turn_id, str) or not turn_id.strip()
                or isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 0):
            raise ValueError("speaker audio.frame requires turn_id and generation")
        if (isinstance(start_sample, bool) or not isinstance(start_sample, int)
                or isinstance(end_sample, bool) or not isinstance(end_sample, int)
                or start_sample < 0 or end_sample < start_sample):
            raise ValueError("speaker audio.frame sample range invalid")
        latest = self.latest_generation.get(turn_id)
        if latest is not None and generation < latest:
            # A resumed turn may still have an old frame in the send queue.
            # Ignore it at the worker boundary so it cannot recreate a stale
            # buffer after the newer generation has already started.
            return []
        if latest is None or generation > latest:
            self.latest_generation[turn_id] = generation
            for stale_key in [key for key in self.buffers if key[0] == turn_id and key[1] != generation]:
                self.buffers.pop(stale_key, None)
                self.starts.pop(stale_key, None)
                self.last_emitted.pop(stale_key, None)
                self.speech_ranges.pop(stale_key, None)
                self.turn_speech_ranges.pop(stale_key, None)
                self.vad_tracked.discard(stale_key)
            self.finished = {key: value for key, value in self.finished.items() if key[0] != turn_id}
        key = (turn_id, generation)
        if key in self.finished:
            return []
        pcm = decode_pcm(payload.get("pcm_base64"))
        if len(pcm) % 2:
            raise ValueError("speaker audio.frame PCM is not int16 aligned")
        if (end_sample - start_sample) != len(pcm) // 2:
            raise ValueError("speaker audio.frame sample range does not match PCM length")
        speech = payload.get("speech")
        if isinstance(speech, bool):
            self.vad_tracked.add(key)
            if speech:
                self._append_range(
                    self.speech_ranges.setdefault(key, []), start_sample, end_sample,
                )
                self._append_range(
                    self.turn_speech_ranges.setdefault(key, []), start_sample, end_sample,
                )
        if key not in self.buffers:
            self.buffers[key] = bytearray()
            self.starts[key] = start_sample
            # The first complete window is emitted once the full buffer is
            # available.  Subsequent windows advance by the step;
            # initializing at ``window - step`` avoids emitting the same
            # initial window three times.
            self.last_emitted[key] = start_sample + self.window_samples - self.step_samples
        buffer = self.buffers[key]
        expected = self.starts[key] + len(buffer) // 2
        if start_sample > expected:
            # A worker-side gap is recoverable; reset to the newest contiguous
            # frame instead of poisoning subsequent turns.
            buffer.clear()
            self.starts[key] = start_sample
            self.last_emitted[key] = start_sample + self.window_samples - self.step_samples
            expected = start_sample
        overlap = max(0, expected - start_sample)
        buffer.extend(pcm[overlap * 2:])
        result: list[dict[str, Any]] = []
        current_end = self.starts[key] + len(buffer) // 2
        while (current_end - self.last_emitted[key] >= self.step_samples
               and len(buffer) // 2 >= self.window_samples):
            # Emit each scheduled end point exactly once, including multiple
            # windows that became available in one large incoming frame.
            window_end = self.last_emitted[key] + self.step_samples
            window_start = window_end - self.window_samples
            start_offset = max(0, window_start - self.starts[key]) * 2
            end_offset = max(0, window_end - self.starts[key]) * 2
            window = bytes(buffer[start_offset:end_offset])
            result.append(self._observation(
                turn_id=turn_id, generation=generation,
                start=window_start, end=window_end, pcm=window,
            ))
            self.last_emitted[key] += self.step_samples
        # Retain only enough history for the next overlapping window.  This
        # keeps a long turn bounded without changing absolute sample indices.
        keep_from = current_end - self.window_samples - self.step_samples
        if keep_from > self.starts[key]:
            trim = min(len(buffer) // 2, keep_from - self.starts[key])
            if trim:
                del buffer[:trim * 2]
                self.starts[key] += trim
        pruning = self.starts[key] - self.window_samples
        if pruning > 0 and key in self.speech_ranges:
            self.speech_ranges[key] = [
                (max(a, pruning), b) for a, b in self.speech_ranges[key] if b > pruning
            ]
        return result

    def finish(self, turn_id: str, generation: int) -> list[dict[str, Any]]:
        key = (turn_id, generation)
        latest = self.latest_generation.get(turn_id)
        if latest is None or generation > latest:
            self.latest_generation[turn_id] = generation
            for stale_key in [item for item in self.buffers if item[0] == turn_id and item[1] != generation]:
                self.buffers.pop(stale_key, None)
                self.starts.pop(stale_key, None)
                self.last_emitted.pop(stale_key, None)
                self.speech_ranges.pop(stale_key, None)
                self.turn_speech_ranges.pop(stale_key, None)
                self.vad_tracked.discard(stale_key)
            self.finished = {item: value for item, value in self.finished.items() if item[0] != turn_id}
        self.finished[key] = None
        if len(self.finished) > 1024:
            self.finished = dict(list(self.finished.items())[-512:])
        buffer = self.buffers.get(key)
        result: list[dict[str, Any]] = []
        # Preserve a useful final observation for turns shorter than the
        # sliding window.  Anything the model cannot embed is explicitly marked
        # ``insufficient_audio`` and can never become a non-owner decision.
        if buffer:
            sample_count = len(buffer) // 2
            if sample_count >= self.window_samples and self.starts[key] + sample_count > self.last_emitted[key]:
                # Flush the newest full window even when the turn ended
                # between the regular step points.
                end = self.starts[key] + sample_count
                start = end - self.window_samples
                result.append(self._observation(
                    turn_id=turn_id, generation=generation,
                    start=start, end=end,
                    pcm=bytes(buffer[-self.window_samples * 2:]),
                ))
            elif sample_count < self.window_samples:
                start = self.starts[key]
                result.append(self._observation(
                    turn_id=turn_id, generation=generation,
                    start=start, end=start + sample_count, pcm=bytes(buffer),
                ))
        self.buffers.pop((turn_id, generation), None)
        self.starts.pop((turn_id, generation), None)
        self.last_emitted.pop((turn_id, generation), None)
        self.speech_ranges.pop((turn_id, generation), None)
        self.turn_speech_ranges.pop((turn_id, generation), None)
        self.vad_tracked.discard((turn_id, generation))
        return result


async def run(config_path: Path | None = None) -> None:
    try:
        import websockets  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 websockets，请先运行安装脚本。") from exc
    root = Path(__file__).resolve().parents[2]
    config = load_config(root / "config" / "default.yaml", config_path if config_path and config_path.is_file() else None)
    config = ProductSettingsStore(
        path=root / "config" / "product-settings.json",
        config=config,
    ).effective_config()
    model_path = config.speaker_model_path
    engine = SpeakerWorkerEngine(
        model_path, sample_rate=config.audio.sample_rate,
        window_ms=config.speaker.window_ms, step_ms=config.speaker.step_ms,
        min_speech_ms=config.speaker.min_speech_ms,
        min_window_speech_ratio=config.speaker.min_window_speech_ratio,
        device=config.compute.devices.get("wespeaker", "cpu"),
        fallback_device=config.compute.fallback_devices.get("wespeaker", "cpu"),
    )
    await asyncio.to_thread(engine.load)

    async def handler(websocket, path=None):
        if path not in (None, "/speaker"):
            await websocket.close(code=1008, reason="unknown path")
            return
        async for raw in websocket:
            try:
                message = json.loads(raw) if isinstance(raw, str) else {}
                kind = message.get("type") if isinstance(message, dict) else None
                if kind == "health":
                    from ..audio.shared import capabilities
                    await websocket.send(json.dumps({
                        "type": "health", "request_id": message.get("request_id"),
                        "status": "READY", "model_loaded": True,
                        "model_version": engine.model_version,
                        "frontend_version": engine.model.frontend_version,
                        "device": engine.model.device,
                        "requested_device": engine.model.requested_device,
                        "load_error": engine.model.load_error,
                        "process_memory_rss_bytes": process_memory_bytes(),
                        "resource_status": process_resource_status(),  # resource_status（资源状态）。
                        "shared_audio": capabilities(),  # shared_audio（共享音频能力）。
                    }, ensure_ascii=False))
                elif kind == "fingerprint":
                    # Lets Voice Server prove that the enrolled templates were
                    # produced by this exact feature frontend and these exact
                    # model weights, without loading a second ONNX session.
                    try:
                        probe = list(engine.model.probe_embedding())
                        payload = {
                            "type": "fingerprint",
                            "request_id": message.get("request_id"),
                            "frontend_version": engine.model.frontend_version,
                            "model_version": engine.model_version,
                            "embedding_dim": len(probe),
                            "probe": probe,
                        }
                    except Exception as exc:
                        payload = {
                            "type": "fingerprint",
                            "request_id": message.get("request_id"),
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    await websocket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                elif kind == "audio.frame":
                    for item in await asyncio.to_thread(engine.feed, message):
                        await websocket.send(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                elif kind == "embed":
                    request_id = message.get("request_id")
                    from ..audio.shared import SharedAudioReader, native_audio_call
                    reader = None
                    try:
                        if "audio_ref" in message:
                            if "pcm_base64" in message:
                                raise ValueError("共享引用不能同时带音频正文")
                            try:
                                reader = SharedAudioReader(message["audio_ref"], request_id, 10 * 16000 * 2)
                            except Exception as exc:
                                await websocket.send(json.dumps({"type": "error", "request_id": request_id,
                                    "code": "AUDIO_REF_REJECTED", "message": str(exc)[:300]}, ensure_ascii=False))
                                continue  # 原生推理尚未提交，允许创建方回退正文协议。
                            pcm = reader.view
                        else:
                            pcm = decode_pcm(message.get("pcm_base64"))
                        quality, speech_ms, _ = engine._quality(pcm)
                        embedding = list(await native_audio_call(engine.model.embed_pcm, pcm, reader)) if quality == "ready" else []
                    finally:
                        if reader is not None and not reader.in_use:
                            reader.close()
                    await websocket.send(json.dumps({
                        "type": "speaker.embed_result", "request_id": request_id,
                        "quality": quality, "speech_ms": speech_ms,
                        "model_version": engine.model_version, "embedding": embedding,
                    }, ensure_ascii=False, separators=(",", ":")))
                elif kind == "turn.finish":
                    turn_id, generation = message.get("turn_id"), message.get("generation")
                    if (isinstance(turn_id, str) and turn_id.strip()
                            and isinstance(generation, int) and not isinstance(generation, bool)
                            and generation >= 0):
                        for item in await asyncio.to_thread(engine.finish, turn_id, generation):
                            await websocket.send(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                        await websocket.send(json.dumps({
                            "type": "speaker.finished", "turn_id": turn_id,
                            "generation": generation,
                        }, ensure_ascii=False))
                else:
                    await websocket.send(json.dumps({"type": "error", "code": "INVALID_COMMAND", "message": "声纹命令无效"}, ensure_ascii=False))
            except Exception as exc:
                logger.exception("speaker.worker.request_failed")
                await websocket.send(json.dumps({
                    "type": "error", "code": "SPEAKER_WORKER_ERROR",
                    "request_id": message.get("request_id") if isinstance(message, dict) else None,
                    "message": str(exc)[:300],
                }, ensure_ascii=False))

    async with websockets.serve(handler, config.speaker_worker.host, config.speaker_worker.port):
        logger.info("Speaker Worker listening on ws://%s:%s/speaker", config.speaker_worker.host, config.speaker_worker.port)
        from ..application.managed import wait_for_shutdown
        await wait_for_shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice Terminal Speaker Worker")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    load_env_file(root / ".env.local")
    configure_logging(root / "logs" / "speaker-worker.log")
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()

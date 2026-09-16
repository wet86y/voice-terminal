from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from .contracts import SpeakerObservation
from .engine import SpeakerModelError, WeSpeakerOnnxEngine
from .protocol import decode_pcm
from ..config import load_config
from ..core.events import utc_now_iso
from ..env import load_env_file
from ..logging_setup import configure_logging

logger = logging.getLogger(__name__)


class SpeakerWorkerEngine:
    def __init__(self, model_path: Path, *, sample_rate: int = 16000,
                 window_ms: int = 1500, step_ms: int = 500,
                 min_speech_ms: int = 500) -> None:
        self.model_path = model_path
        self.model = WeSpeakerOnnxEngine(model_path, sample_rate=sample_rate)
        self.sample_rate = sample_rate
        self.window_samples = sample_rate * window_ms // 1000
        self.step_samples = sample_rate * step_ms // 1000
        self.min_speech_ms = max(1, int(min_speech_ms))
        self.buffers: dict[tuple[str, int], bytearray] = {}
        self.starts: dict[tuple[str, int], int] = {}
        self.last_emitted: dict[tuple[str, int], int] = {}
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

    def _quality(self, pcm: bytes) -> tuple[str, int]:
        try:
            import numpy as np  # type: ignore
            values = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            if values.size == 0:
                return "insufficient_audio", 0
            rms = float(np.sqrt(np.mean((values / 32768.0) ** 2)))
            if rms < 0.003:
                return "too_quiet", 0
            clipped = float(np.mean(np.abs(values) >= 32760.0))
            if clipped > 0.02:
                return "clipped", int(values.size * 1000 / self.sample_rate)
            # Approximate effective speech duration from non-silent samples;
            # VAD remains authoritative for routing, this quality gate only
            # prevents a short utterance from becoming a false identity hit.
            voiced = int(np.count_nonzero(np.abs(values) >= 327.0))
            speech_ms = int(voiced * 1000 / self.sample_rate)
            if speech_ms < self.min_speech_ms:
                return "insufficient_audio", speech_ms
            return "ready", speech_ms
        except Exception:
            return "ready", len(pcm) // 2 * 1000 // self.sample_rate

    def _observation(self, *, turn_id: str, generation: int, start: int, end: int,
                     pcm: bytes) -> dict[str, Any]:
        quality, speech_ms = self._quality(pcm)
        payload: dict[str, Any] = {
            "type": "speaker.embedding",
            "turn_id": turn_id,
            "generation": generation,
            "window_started_sample": start,
            "window_ended_sample": end,
            # The sidecar does not own the user's template registry, so the
            # initial payload leaves matching fields empty.  Voice Server
            # fills them after comparing the embedding with its centre and
            # optional labelled templates.
            "similarity": None,
            "matched_template_id": None,
            "speech_ms": speech_ms,
            "quality": quality,
            "model_version": self.model_version,
            "timestamp": utc_now_iso(),
            "embedding": [],
        }
        if quality == "ready":
            payload["embedding"] = list(self.model.embed_pcm(pcm))
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
            self.finished = {key: value for key, value in self.finished.items() if key[0] != turn_id}
        key = (turn_id, generation)
        if key in self.finished:
            return []
        pcm = decode_pcm(payload.get("pcm_base64"))
        if len(pcm) % 2:
            raise ValueError("speaker audio.frame PCM is not int16 aligned")
        if (end_sample - start_sample) != len(pcm) // 2:
            raise ValueError("speaker audio.frame sample range does not match PCM length")
        if key not in self.buffers:
            self.buffers[key] = bytearray()
            self.starts[key] = start_sample
            # The first complete window is emitted once the 1.5 s buffer is
            # available.  Subsequent windows advance by the 500 ms step;
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
            self.finished = {item: value for item, value in self.finished.items() if item[0] != turn_id}
        self.finished[key] = None
        if len(self.finished) > 1024:
            self.finished = dict(list(self.finished.items())[-512:])
        buffer = self.buffers.get(key)
        result: list[dict[str, Any]] = []
        # Preserve a useful final observation for turns shorter than the
        # sliding window.  Under 500 ms is explicitly marked insufficient and
        # can never become a non-owner decision.
        if buffer:
            sample_count = len(buffer) // 2
            if sample_count >= self.window_samples and self.starts[key] + sample_count > self.last_emitted[key]:
                # Flush the newest full window even when the turn ended
                # between the regular 500 ms update points.
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
        return result


async def run(config_path: Path | None = None) -> None:
    try:
        import websockets  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 websockets，请先运行安装脚本。") from exc
    root = Path(__file__).resolve().parents[2]
    config = load_config(root / "config" / "default.yaml", config_path if config_path and config_path.is_file() else None)
    model_path = config.speaker_model_path
    engine = SpeakerWorkerEngine(
        model_path, sample_rate=config.audio.sample_rate,
        window_ms=config.speaker.window_ms, step_ms=config.speaker.step_ms,
        min_speech_ms=config.speaker.min_speech_ms,
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
                    await websocket.send(json.dumps({
                        "type": "health", "request_id": message.get("request_id"),
                        "status": "READY", "model_loaded": True,
                        "model_version": engine.model_version,
                    }, ensure_ascii=False))
                elif kind == "audio.frame":
                    for item in await asyncio.to_thread(engine.feed, message):
                        await websocket.send(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                elif kind == "embed":
                    request_id = message.get("request_id")
                    pcm = decode_pcm(message.get("pcm_base64"))
                    quality, speech_ms = engine._quality(pcm)
                    embedding = list(engine.model.embed_pcm(pcm)) if quality == "ready" else []
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
                    "message": str(exc)[:300],
                }, ensure_ascii=False))

    async with websockets.serve(handler, config.speaker_worker.host, config.speaker_worker.port):
        logger.info("Speaker Worker listening on ws://%s:%s/speaker", config.speaker_worker.host, config.speaker_worker.port)
        await asyncio.Future()


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

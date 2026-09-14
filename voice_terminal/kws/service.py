"""Thread-owned keyword spotting service.

The native sherpa stream is deliberately owned by one dedicated thread.  The
async audio/controller path only copies bounded PCM frames into a queue and
polls detection events; it never calls the native decoder itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from queue import Empty, Full, Queue, SimpleQueue
from threading import Event, Thread
from typing import Protocol


logger = logging.getLogger(__name__)


class KeywordDetector(Protocol):
    def accept_pcm(self, pcm: bytes) -> str | None: ...
    def reset(self) -> None: ...


@dataclass(frozen=True)
class KwsAudioFrame:
    pcm: bytes
    start_sample: int
    end_sample: int
    captured_at: float


@dataclass(frozen=True)
class KwsDetection:
    label: str
    start_sample: int
    end_sample: int
    captured_at: float


class KwsWorker:
    """Bounded, dedicated-thread adapter around one :class:`KwsDetector`."""

    def __init__(self, detector: KeywordDetector, *, queue_frames: int = 50) -> None:
        if queue_frames < 1:
            raise ValueError("queue_frames must be positive")
        self._detector = detector
        self._input: Queue[KwsAudioFrame | None] = Queue(maxsize=queue_frames)
        self._detections: SimpleQueue[KwsDetection] = SimpleQueue()
        self._stop = Event()
        self._reset = Event()
        self._thread: Thread | None = None
        self._error: str | None = None
        self.submitted_frames = 0
        self.dropped_frames = 0
        self.detected_count = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="kws-worker", daemon=True)
        self._thread.start()

    def submit(self, frame: KwsAudioFrame) -> bool:
        """Non-blockingly enqueue PCM; a dropped block resets native state."""

        if not self.running:
            return False
        try:
            self._input.put_nowait(frame)
            self.submitted_frames += 1
            return True
        except Full:
            self.dropped_frames += 1
            # A KWS stream must not join samples across a queue gap.  Reset
            # before the next decode instead of producing a detection from a
            # discontinuous phrase.
            self._reset.set()
            # Keep a sustained overload observable without turning one error
            # per input block into a log flood.
            if self.dropped_frames == 1 or self.dropped_frames & (self.dropped_frames - 1) == 0:
                logger.warning("kws.worker_queue_overflow dropped=%d", self.dropped_frames)
            return False

    def poll(self) -> KwsDetection | None:
        try:
            return self._detections.get_nowait()
        except Empty:
            return None

    def reset(self) -> None:
        """Discard queued audio/results and reset the native stream in its owner thread."""

        while True:
            try:
                self._input.get_nowait()
            except Empty:
                break
        while True:
            try:
                self._detections.get_nowait()
            except Empty:
                break
        self._reset.set()

    def close(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        try:
            self._input.put_nowait(None)
        except Full:
            # The worker polls the stop flag every 100 ms, so it does not need
            # a sentinel when a congested queue cannot accept one.
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            self._detector.reset()
            while not self._stop.is_set():
                if self._reset.is_set():
                    self._detector.reset()
                    self._reset.clear()
                try:
                    frame = self._input.get(timeout=0.1)
                except Empty:
                    continue
                if frame is None:
                    return
                detected = self._detector.accept_pcm(frame.pcm)
                if detected is not None:
                    self.detected_count += 1
                    self._detections.put(KwsDetection(
                        label=detected,
                        start_sample=frame.start_sample,
                        end_sample=frame.end_sample,
                        captured_at=frame.captured_at,
                    ))
        except Exception as exc:
            self._error = str(exc) or type(exc).__name__
            logger.exception("kws.worker_failed")

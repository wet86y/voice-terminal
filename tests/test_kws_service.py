from __future__ import annotations

import time

from voice_terminal.kws.service import KwsAudioFrame, KwsWorker


class _Detector:
    def __init__(self) -> None:
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1

    def accept_pcm(self, pcm: bytes) -> str | None:
        return "HEI_CLAW" if pcm == b"wake" else None


def test_kws_worker_owns_detection_and_publishes_metadata():
    detector = _Detector()
    worker = KwsWorker(detector)
    worker.start()
    try:
        assert worker.submit(KwsAudioFrame(b"wake", 160, 164, 12.5))
        deadline = time.monotonic() + 1
        detection = None
        while time.monotonic() < deadline and detection is None:
            detection = worker.poll()
            time.sleep(0.01)
        assert detection is not None
        assert detection.label == "HEI_CLAW"
        assert detection.start_sample == 160
        assert detection.end_sample == 164
        assert detection.captured_at == 12.5
        assert detector.reset_count >= 1
    finally:
        worker.close()


def test_kws_worker_reset_discards_stale_detection():
    detector = _Detector()
    worker = KwsWorker(detector)
    worker.start()
    try:
        assert worker.submit(KwsAudioFrame(b"wake", 0, 4, 1.0))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and worker.detected_count == 0:
            time.sleep(0.01)
        worker.reset()
        assert worker.poll() is None
    finally:
        worker.close()

"""A KWS-only microphone probe.

This deliberately bypasses the VAD, ASR, LLM, TTS and WebSocket runtime so
the installed keyword-spotting model can be verified in isolation.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from ..audio.devices import resolve_device
from ..config import load_config
from .detector import KwsDetector


def build_detector(config) -> KwsDetector:
    return KwsDetector(
        model_dir=config.kws_dir,
        keywords_file=config.kws_keywords_file,
        keyword_label=config.wake.keyword_label,
        threshold=config.wake.threshold,
        keywords_score=config.wake.keywords_score,
        num_trailing_blanks=config.wake.num_trailing_blanks,
        provider=config.wake.device,
        sample_rate=config.audio.sample_rate,
    )


def run_microphone(detector: KwsDetector, config, *, duration: float | None) -> int:
    try:
        import sounddevice as sd  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 sounddevice，请先运行 scripts\\setup.ps1。") from exc

    sample_rate = config.audio.sample_rate
    if config.audio.channels != 1 or config.audio.sample_format != "int16" or sample_rate != 16000:
        raise ValueError("KWS probe requires 16 kHz mono int16 capture")
    blocksize = max(1, sample_rate * config.audio.block_ms // 1000)
    device = resolve_device(config.audio.input_device, input_device=True)
    deadline = time.monotonic() + duration if duration is not None else None
    print(
        f"KWS ready: keyword={config.wake.keyword!r} label=@{config.wake.keyword_label} "
        f"device={device if device is not None else 'system default'}; press Ctrl+C to stop.",
        flush=True,
    )
    hits = 0
    try:
        with sd.RawInputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            device=device,
        ) as stream:
            while deadline is None or time.monotonic() < deadline:
                pcm, overflowed = stream.read(blocksize)
                if overflowed:
                    print("warning: microphone input overflow", file=sys.stderr, flush=True)
                detected = detector.accept_pcm(bytes(pcm))
                if detected is not None:
                    hits += 1
                    print(f"WAKE_DETECTED label={detected} at={time.strftime('%H:%M:%S')}", flush=True)
    except KeyboardInterrupt:
        pass
    print(f"KWS stopped; detections={hits}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run only the local sherpa-onnx KWS microphone probe")
    parser.add_argument("--config", type=Path, default=Path("config/local.yaml"), help="optional local YAML override")
    parser.add_argument("--duration", type=float, default=None, help="stop automatically after N seconds")
    args = parser.parse_args(argv)
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be greater than zero")

    root = Path(__file__).resolve().parents[2]
    config = load_config(root / "config" / "default.yaml", args.config if args.config.is_file() else None)
    if not config.wake.enabled:
        print("note: wake.enabled=false in the normal runtime; probe continues for isolated verification.", file=sys.stderr)
    detector = build_detector(config)
    return run_microphone(detector, config, duration=args.duration)


if __name__ == "__main__":
    raise SystemExit(main())

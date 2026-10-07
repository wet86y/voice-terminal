"""Create ignored, deterministic long-audio fixtures for analysis checks."""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=(ROOT / ".local/cache/evaluation/audio/long.wav"))
    parser.add_argument("--output-dir", type=Path, default=(ROOT / ".local/cache/evaluation/audio"))
    parser.add_argument(
        "--durations",
        nargs="*",
        type=float,
        default=(29.8, 30.0, 30.01, 45.0, 59.8, 60.0, 60.8, 61.0),
    )
    args = parser.parse_args()

    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    audio, sample_rate = sf.read(args.source, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        audio = resample_poly(audio, 16000, sample_rate)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for duration in args.durations:
        if duration <= 0:
            raise SystemExit("duration must be positive")
        samples = round(duration * 16000)
        repeats = int(np.ceil(samples / len(audio)))
        fixture = np.tile(audio, repeats)[:samples]
        label = f"{duration:g}".replace(".", "_")
        path = args.output_dir / f"long-stage4-{label}s.wav"
        sf.write(path, fixture, 16000, subtype="PCM_16")
        print(f"{path} {len(fixture) / 16000:.5f}s")


if __name__ == "__main__":
    main()

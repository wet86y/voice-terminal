"""Offline KWS recall probe.

Feeds real 16 kHz mono int16 WAV files through the *installed* sherpa-onnx
keyword spotter in 20 ms blocks (the same granularity the runtime uses) and
reports hits, misses and detection latency.

Three modes exist on purpose:

``as-shipped``
    Build the production :class:`KwsDetector` from ``config/default.yaml`` plus
    ``config/product-settings.json``.  This is the only mode that also exercises
    the project's label match, so a hit here is a real product hit.

``wake``
    Use the managed keywords file but override ``threshold`` / ``keywords_score``
    / ``num_trailing_blanks`` so the knobs can be swept without editing config.

``official``
    Use the model's own ``test_wavs/keywords.txt`` and ``zh_*.wav`` files.  This
    is the calibration baseline: it shows what the model can do on the phrases
    it was published with, which separates "model is weak" from "our wake word
    or our knobs are weak".

Example::

    python scripts/probe_kws_recall.py --mode official
    python scripts/probe_kws_recall.py --mode as-shipped --wav logs/evaluation/kws-probe/qisan_plain.wav
    python scripts/probe_kws_recall.py --mode wake --sweep-score 1.0,2.0,3.0
"""

from __future__ import annotations

import argparse
import math
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROBE_DIR = ROOT / "logs" / "evaluation" / "kws-probe"


def product_config():
    """The exact runtime config: YAML plus product settings overlay."""

    from voice_terminal.config import load_config
    from voice_terminal.server.product import ProductSettingsStore

    config = load_config(ROOT / "config" / "default.yaml", ROOT / "config" / "local.yaml")
    store = ProductSettingsStore(path=ROOT / "config" / "product-settings.json", config=config)
    return store.effective_config()


def model_dir(config) -> Path:
    path = Path(config.kws_dir)
    if not path.is_dir():
        raise SystemExit(f"KWS 模型目录不存在：{path}")
    return path


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"{path.name} 必须是 16 kHz 单声道 int16 WAV")
        if handle.getframerate() != 16000:
            raise ValueError(f"{path.name} 采样率必须是 16000，实际 {handle.getframerate()}")
        return handle.readframes(handle.getnframes())


def blocks(pcm: bytes, block_ms: int):
    step = max(2, 16000 * block_ms // 1000 * 2)
    for offset in range(0, len(pcm), step):
        yield offset, pcm[offset:offset + step]


def speech_rms(samples: np.ndarray, gate: int = 300) -> float:
    """RMS of the speech-active part only, so file padding cannot skew SNR."""

    active = samples[np.abs(samples) >= gate]
    if active.size < 160:  # < 10 ms of speech: fall back to the whole file
        active = samples
    return float(np.sqrt(np.mean(np.square(active.astype(np.float64)))))


def degrade(pcm: bytes, *, gain_db: float, snr_db: float | None, seed: int = 20260915) -> bytes:
    """Apply a level change and/or additive white noise, as a far/quiet talker would."""

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    samples *= 10.0 ** (gain_db / 20.0)
    if snr_db is not None:
        noise_power = (speech_rms(samples) ** 2) / (10.0 ** (snr_db / 10.0))
        generator = np.random.default_rng(seed)
        samples += generator.normal(0.0, math.sqrt(max(noise_power, 1e-12)), samples.size)
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def variant_label(gain_db: float, snr_db: float | None) -> str:
    text = f"{gain_db:+.0f}dB"
    return text if snr_db is None else f"{text}/SNR{snr_db:.0f}dB"


class Spotter:
    """Minimal replica of ``KwsDetector`` with explicit knobs."""

    def __init__(self, model_dir: Path, keywords_file: Path, *, threshold: float,
                 score: float, trailing_blanks: int, sample_rate: int = 16000,
                 precision: str = "as-shipped", chunk: int = 16,
                 max_active_paths: int = 4) -> None:
        import sherpa_onnx  # type: ignore

        self.model_dir = model_dir

        def one(pattern: str, *, stem: str | None = None) -> Path:
            if stem is None:
                matches = sorted(model_dir.glob(pattern))
            else:
                matches = self.pick(stem, chunk, precision)
            if not matches:
                raise FileNotFoundError(f"{model_dir} 缺少 {pattern}")
            return matches[0]

        self.encoder = one("encoder*.onnx", stem="encoder")
        self.decoder = one("decoder*.onnx", stem="decoder")
        self.joiner = one("joiner*.onnx", stem="joiner")
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(one("tokens.txt")),
            encoder=str(self.encoder),
            decoder=str(self.decoder),
            joiner=str(self.joiner),
            num_threads=1,
            max_active_paths=max_active_paths,
            keywords_file=str(keywords_file),
            keywords_score=score,
            keywords_threshold=threshold,
            num_trailing_blanks=trailing_blanks,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()
        self.sample_rate = sample_rate
        self._np = __import__("numpy")

    def pick(self, stem: str, chunk: int, precision: str) -> list[Path]:
        """Resolve the encoder/decoder/joiner variant the way the runtime does.

        ``KwsDetector`` uses ``sorted(glob(...))[0]``, which silently selects the
        ``.int8.onnx`` file because ``i`` sorts before ``o``.  ``as-shipped``
        reproduces that; ``fp32`` and ``int8`` make the choice explicit.
        """

        candidates = sorted(self.model_dir.glob(f"{stem}-*.onnx"))
        if not candidates:
            raise FileNotFoundError(f"{self.model_dir} 缺少 {stem}-*.onnx")
        if precision == "as-shipped":
            return candidates
        if precision == "int8":
            chosen = [path for path in candidates if ".int8." in path.name] or candidates
        else:
            chosen = [path for path in candidates if ".int8." not in path.name] or candidates
        chunked = [path for path in chosen if f"chunk-{chunk}-" in path.name]
        return chunked or chosen

    def run(self, pcm: bytes, *, block_ms: int = 20, preprocess=None) -> list[tuple[float, str]]:
        hits: list[tuple[float, str]] = []
        # One file must not inherit the previous file's decoder state.  Without
        # this the same configuration scored 19/27 or 20/27 purely because the
        # preceding audio differed, which made comparisons unreproducible.
        self._spotter.reset_stream(self._stream)
        for offset, block in blocks(pcm, block_ms):
            if preprocess is not None:
                block = preprocess(block)
            samples = self._np.frombuffer(block, dtype=self._np.int16).astype(self._np.float32) / 32768.0
            self._stream.accept_waveform(self.sample_rate, samples)
            while self._spotter.is_ready(self._stream):
                self._spotter.decode_stream(self._stream)
                detected = self._spotter.get_result(self._stream)
                if detected:
                    hits.append(((offset + len(block)) / 2 / self.sample_rate, detected))
                    # Mirror the runtime: re-arm immediately after a hit.
                    self._spotter.reset_stream(self._stream)
                    break
        return hits

    def close(self) -> None:
        self._stream = None
        self._spotter = None


def preprocessor_factory():
    """Return a callable that runs one PCM block through the product AEC/NS chain."""

    from voice_terminal.audio.processing import AudioPreprocessor

    chain = AudioPreprocessor(
        sample_rate=16000, channels=1, reverse_sample_rate=32000,
        enabled=True, required=False,
        enable_aec=True, enable_ns=True, enable_agc=False,
    )
    if not chain.available:
        raise RuntimeError(f"音频前处理不可用：{chain.error}")
    print(f"preprocess: status={chain.status} aec=on ns=on agc=off")
    return chain.process


def sweep_values(raw: str | None, default: list[float]) -> list[float]:
    if not raw:
        return default
    return [float(item) for item in raw.split(",") if item.strip()]


def report(label: str, hits: list[tuple[float, str]], duration: float) -> bool:
    if hits:
        pretty = ", ".join(f"{when:.2f}s->{text!r}" for when, text in hits[:6])
        print(f"  HIT   {label}: {len(hits)} 次 | {pretty}")
    else:
        print(f"  MISS  {label}: 0 次 (音频 {duration:.2f}s)")
    return bool(hits)


def build_corpora(wavs: list[Path], gains: list[float], snrs: list[float | None],
                  trials: int) -> list[tuple[str, list[tuple[str, bytes]]]]:
    """Materialise every (gain, SNR, noise-seed) variant once.

    Every knob combination is then scored on byte-identical audio, so a
    difference between two rows can only come from the knob.
    """

    corpora: list[tuple[str, list[tuple[str, bytes]]]] = []
    for gain in gains:
        for snr in snrs:
            seeds: list[int | None] = list(range(trials)) if (snr is not None and trials > 1) else [None]
            items: list[tuple[str, bytes]] = []
            for seed in seeds:
                for wav in wavs:
                    pcm = read_wav(wav)
                    if gain or snr is not None:
                        pcm = degrade(pcm, gain_db=gain, snr_db=snr, seed=20260915 + (seed or 0))
                    items.append((wav.name + ("" if seed is None else f"#{seed}"), pcm))
            corpora.append((variant_label(gain, snr), items))
    return corpora


def mode_official(args) -> int:
    config = product_config()
    directory = model_dir(config)
    test_dir = directory / "test_wavs"
    wavs = [Path(item) for item in args.wav] if args.wav else sorted(test_dir.glob("zh_*.wav"))
    keywords = test_dir / "keywords.txt"
    print(f"official keywords: {keywords}")
    thresholds = sweep_values(args.sweep_threshold,
                              [args.threshold if args.threshold is not None else 0.25])
    scores = sweep_values(args.sweep_score, [args.score if args.score is not None else 1.0])
    blanks = [int(item) for item in sweep_values(
        args.sweep_blanks, [args.trailing_blanks if args.trailing_blanks is not None else 1])]
    gains = sweep_values(args.gain_db, [0.0])
    snrs = sweep_values(args.snr_db, [None]) if args.snr_db else [None]
    corpora = build_corpora(wavs, gains, snrs, args.trials)
    best: dict[str, list[tuple[float, str]]] = {}
    print(f"official baseline: threshold={thresholds} score={scores} blanks={blanks}")
    for blank in blanks:
        for score in scores:
            for threshold in thresholds:
                spotter = Spotter(directory, keywords, threshold=threshold,
                                  score=score, trailing_blanks=blank,
                                  precision=args.precision, chunk=args.chunk,
                                  max_active_paths=args.max_active_paths)
                row = []
                for variant, items in corpora:
                    hits = 0
                    for name, pcm in items:
                        found = spotter.run(pcm, block_ms=args.block_ms)
                        hits += 1 if found else 0
                        base = name.split("#")[0]
                        best.setdefault(base, [])
                        if found and not best[base]:
                            best[base] = found
                        if args.verbose:
                            report(f"{name} [{variant} t={threshold} s={score} b={blank}]", found,
                                   len(pcm) / 2 / 16000)
                    row.append(f"{variant}={hits}/{len(items)}")
                spotter.close()
                print(f"t={threshold:<6} score={score:<5} blanks={blank}: " + "  ".join(row))
    print("\n每个音频在任一参数组合下最佳：")
    for name, found in best.items():
        report(name, found, 0.0)
    return 0


def mode_wake(args) -> int:
    config = product_config()
    directory = model_dir(config)
    keywords = Path(args.keywords_file) if args.keywords_file else Path(config.kws_keywords_file)
    print(f"wake keyword={config.wake.keyword!r} label=@{config.wake.keyword_label} "
          f"threshold={config.wake.threshold} score={config.wake.keywords_score} "
          f"blanks={config.wake.num_trailing_blanks}")
    print(f"keywords file: {keywords}")
    print(f"  content: {keywords.read_text(encoding='utf-8').strip()!r}")

    wavs = [Path(item) for item in args.wav] if args.wav else sorted(PROBE_DIR.glob("*.wav"))
    wavs = [wav for wav in wavs if not wav.name.startswith("_")]
    if not wavs:
        print("没有可用 WAV；请用 --wav 指定", file=sys.stderr)
        return 2
    preprocess = preprocessor_factory() if args.preprocess else None

    # Explicit --threshold/--score/--blanks win; otherwise use the product config.
    thresholds = sweep_values(args.sweep_threshold,
                              [args.threshold if args.threshold is not None else config.wake.threshold])
    scores = sweep_values(args.sweep_score,
                          [args.score if args.score is not None else config.wake.keywords_score])
    blanks = [int(item) for item in sweep_values(
        args.sweep_blanks,
        [args.trailing_blanks if args.trailing_blanks is not None else config.wake.num_trailing_blanks])]
    gains = sweep_values(args.gain_db, [0.0])
    snrs = sweep_values(args.snr_db, [None]) if args.snr_db else [None]

    # Pre-build the degraded corpora once; every knob combination sees identical audio.
    corpora = build_corpora(wavs, gains, snrs, args.trials)

    print(f"\n矩阵: threshold={thresholds} x score={scores} x blanks={blanks} "
          f"x 退化={[name for name, _ in corpora]} x preprocess={'on' if preprocess else 'off'} "
          f"x {len(wavs)} 个音频")
    for blank in blanks:
        for score in scores:
            for threshold in thresholds:
                spotter = Spotter(directory, keywords, threshold=threshold, score=score,
                                  trailing_blanks=blank, precision=args.precision,
                                  chunk=args.chunk, max_active_paths=args.max_active_paths)
                if args.verbose:
                    print(f"    files: {spotter.encoder.name} | {spotter.joiner.name}")
                row = []
                for variant, items in corpora:
                    hits = 0
                    for name, pcm in items:
                        found = spotter.run(pcm, block_ms=args.block_ms, preprocess=preprocess)
                        hits += 1 if found else 0
                        if args.verbose:
                            report(f"{name} [{variant} t={threshold} s={score} b={blank}]",
                                   found, len(pcm) / 2 / 16000)
                    row.append(f"{variant}={hits}/{len(items)}")
                spotter.close()
                print(f"t={threshold:<6} score={score:<5} blanks={blank}: " + "  ".join(row))
    return 0


def mode_generate(args) -> int:
    """Generate managed keywords.txt files for candidate wake words."""

    from voice_terminal.kws.keywords import generate_keywords, keyword_label

    config = product_config()
    directory = model_dir(config)
    out_dir = PROBE_DIR / "keywords"
    out_dir.mkdir(parents=True, exist_ok=True)
    for candidate in args.candidates:
        label = keyword_label(candidate)
        raw = out_dir / f"{label}.raw.txt"
        final = out_dir / f"{label}.txt"
        generate_keywords(
            model_dir=directory, raw_path=raw, output_path=final,
            keyword=candidate, label=label,
        )
        print(f"{candidate!r} -> label=@{label} | {final.read_text(encoding='utf-8').strip()!r}")
    return 0


def mode_as_shipped(args) -> int:
    from voice_terminal.kws.detector import KwsDetector

    config = product_config()
    detector = KwsDetector(
        model_dir=config.kws_dir,
        keywords_file=config.kws_keywords_file,
        keyword_label=config.wake.keyword_labels,
        threshold=config.wake.threshold,
        keywords_score=config.wake.keywords_score,
        num_trailing_blanks=config.wake.num_trailing_blanks,
        max_active_paths=config.wake.max_active_paths,
        provider=config.wake.device,
        sample_rate=config.audio.sample_rate,
    )
    print(f"as-shipped: keywords={list(config.wake.keywords)!r} "
          f"labels={['@' + item for item in detector.keyword_labels]!r} "
          f"threshold={config.wake.threshold} score={config.wake.keywords_score} "
          f"blanks={config.wake.num_trailing_blanks} beam={config.wake.max_active_paths}")
    print(f"keywords file content: {config.kws_keywords_file.read_text(encoding='utf-8').strip()!r}")
    wavs = [Path(item) for item in args.wav] if args.wav else sorted(
        (ROOT / "logs" / "evaluation" / "kws-probe").glob("*.wav"))
    preprocess = preprocessor_factory() if args.preprocess else None
    hits = 0
    for wav in wavs:
        pcm = read_wav(wav)
        found: list[tuple[float, str]] = []
        for offset, block in blocks(pcm, args.block_ms):
            if preprocess is not None:
                block = preprocess(block)
            label = detector.accept_pcm(block)
            if label is not None:
                found.append(((offset + len(block)) / 2 / 16000, label))
                detector.reset()
        hits += 1 if found else 0
        report(wav.name, found, len(pcm) / 2 / 16000)
    print(f"as-shipped 命中 {hits}/{len(wavs)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线 KWS 召回/延迟探针")
    parser.add_argument("--mode", choices=("as-shipped", "wake", "official", "generate"),
                        default="as-shipped")
    parser.add_argument("--wav", action="append", default=None, help="可重复；默认用 logs/evaluation/kws-probe/*.wav")
    parser.add_argument("--block-ms", type=int, default=20)
    parser.add_argument("--threshold", type=float, default=None,
                        help="显式阈值；缺省时 wake 模式用产品配置、official 模式用 0.25")
    parser.add_argument("--score", type=float, default=None,
                        help="显式 boosting 分数；缺省时 wake 模式用产品配置、official 模式用 1.0")
    parser.add_argument("--blanks", type=int, default=None, dest="trailing_blanks",
                        help="显式 trailing blanks；缺省时 wake 模式用产品配置、official 模式用 1")
    parser.add_argument("--sweep-threshold", default=None)
    parser.add_argument("--sweep-score", default=None)
    parser.add_argument("--sweep-blanks", default=None)
    parser.add_argument("--gain-db", default=None, help="音量衰减列表，例如 0,-6,-12,-18")
    parser.add_argument("--snr-db", default=None, help="加性白噪声信噪比列表，例如 20,10,5,0")
    parser.add_argument("--trials", type=int, default=1, help="每个噪声条件重复次数（不同噪声种子）")
    parser.add_argument("--precision", choices=("as-shipped", "int8", "fp32"), default="as-shipped",
                        help="编码器精度；as-shipped 复现 sorted(glob)[0] 的 int8 行为")
    parser.add_argument("--chunk", type=int, choices=(8, 16), default=16, help="zipformer chunk 大小")
    parser.add_argument("--max-active-paths", type=int, default=4, dest="max_active_paths",
                        help="beam search 活跃路径数；运行时固定为 4")
    parser.add_argument("--keywords-file", default=None, help="覆盖受管 keywords.txt（wake 模式）")
    parser.add_argument("--candidates", default="七三,你好七三,小七小七",
                        help="generate 模式：候选唤醒词，逗号分隔")
    parser.add_argument("--preprocess", action="store_true", help="把音频先过产品 AEC/NS 前处理链")
    parser.add_argument("--verbose", action="store_true", help="逐音频打印命中细节")
    args = parser.parse_args(argv)
    if args.block_ms < 10 or args.block_ms % 10:
        parser.error("--block-ms 必须是 10 的倍数")
    if args.mode == "official":
        return mode_official(args)
    if args.mode == "wake":
        return mode_wake(args)
    if args.mode == "generate":
        args.candidates = [item.strip() for item in args.candidates.split(",") if item.strip()]
        return mode_generate(args)
    return mode_as_shipped(args)


if __name__ == "__main__":
    raise SystemExit(main())

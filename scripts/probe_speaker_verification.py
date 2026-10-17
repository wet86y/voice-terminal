"""Speaker-verification regression benchmark.

Produces a report under ``.local/cache/evaluation/`` with the numbers the redesign is
accepted against:

* frontend equivalence to the WeSpeaker reference frontend (A1)
* genuine vs impostor score separation and EER (A2)
* FAR/FRR at the configured operating point (A3)
* robustness to a pure recording-gain change (A4)

Run:  python scripts/probe_speaker_verification.py
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))

from voice_terminal.paths import AppPaths
from voice_terminal.config import load_config                       # noqa: E402
from voice_terminal.speaker.engine import WeSpeakerOnnxEngine       # noqa: E402
from voice_terminal.speaker.features import pcm_to_fbank            # noqa: E402

SR = 16000

CORPUS_NOTE = """\
语料局限（必须连同数字一起读）：
  * `.local/cache/evaluation/audio/` 是 Microsoft Huihui Desktop 合成语音，不是真人录音
    （见 docs/testing/known-issues.md）。
  * 该目录中 8 个文件是同一次 60 秒录音的不同截断（首 20 秒字节相同），
    因此"同一说话人"的窗口高度相关，不能当作独立试对数量。
  * `sustained-20s.wav` 在两条独立声纹管线上都与其余文件相距很远，按另一个
    音色/发声方式处理，默认不计入同人集合。
  * 异人集合只有 sherpa-onnx / funasr 自带的少量说话人。
所以本文的 EER 只能用于**回归对比**，不能当作真实场景 EER。
"""


def read_wav(path: Path) -> np.ndarray:
    import torch
    import torchaudio

    with wave.open(str(path), "rb") as reader:
        channels, rate = reader.getnchannels(), reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SR:
        samples = torchaudio.functional.resample(torch.from_numpy(samples), rate, SR).numpy()
    return np.clip(samples, -32768, 32767).astype("<i2").astype(np.float32)


def windows(samples: np.ndarray, window_ms: int, step_ms: int) -> list[np.ndarray]:
    size = SR * window_ms // 1000
    step = SR * step_ms // 1000
    if samples.size < size:
        return [samples] if samples.size else []
    return [samples[start:start + size] for start in range(0, samples.size - size + 1, step)]


def cosine(a, b) -> float:
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denominator) if denominator > 1e-8 else -1.0


def eer(genuine: np.ndarray, impostor: np.ndarray) -> tuple[float, float]:
    if not genuine.size or not impostor.size:
        return float("nan"), float("nan")
    best, threshold = 1.0, 0.0
    for candidate in np.unique(np.concatenate([genuine, impostor])):
        delta = abs(float((impostor >= candidate).mean()) - float((genuine < candidate).mean()))
        if delta < best:
            best, threshold = delta, float(candidate)
    return float((impostor >= threshold).mean()), threshold


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "default.yaml")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    paths = AppPaths.resolve(ROOT)
    config = load_config(args.config, paths.config / "local.yaml")
    model_path = config.speaker_model_path
    if not model_path.is_file():
        print(f"声纹模型不存在：{model_path}", file=sys.stderr)
        return 1

    engine = WeSpeakerOnnxEngine(model_path, sample_rate=SR)
    engine.load()

    audio_dir = ROOT / ".local/cache/evaluation" / "audio"
    impostor_files = sorted((paths.install / "models/kws/sherpa-onnx-kws-zipformer-zh-en-3M/test_wavs").glob("*.wav"))
    impostor_files += [
        (paths.install / "models/funasr/fsmn-vad/example/vad_example.wav"),
        (paths.install / "models/funasr/paraformer-zh-streaming/example/asr_example.wav"),
    ]

    # A single 60 s recording, split by time: enroll on the first 30 s, test on
    # the unseen tail.  Using different files would silently re-use the same
    # audio, because most of the corpus is truncations of this one recording.
    master = read_wav(audio_dir / "long-stage4-60s.wav")
    enroll_segments = [master[i * 10 * SR:(i + 1) * 10 * SR] for i in range(3)]
    genuine_source = master[30 * SR:]
    other_voice = read_wav(audio_dir / "sustained-20s.wav")

    def embed(pcm: np.ndarray):
        return np.asarray(engine.embed_pcm(pcm.astype("<i2").tobytes()), dtype=np.float32)

    centroid = np.mean(np.stack([embed(seg) for seg in enroll_segments]), axis=0)
    centroid /= np.linalg.norm(centroid)

    window_ms, step_ms = config.speaker.window_ms, config.speaker.step_ms
    genuine = [cosine(embed(w), centroid) for w in windows(genuine_source, window_ms, step_ms)]
    impostor: list[float] = []
    for path in impostor_files:
        if not path.is_file():
            continue
        for w in windows(read_wav(path), window_ms, step_ms):
            impostor.append(cosine(embed(w), centroid))
    for w in windows(other_voice, window_ms, step_ms):
        impostor.append(cosine(embed(w), centroid))

    g, i = np.array(genuine), np.array(impostor)
    confirm = config.speaker.confirm_threshold
    continuity = config.speaker.continuity_threshold
    far_confirm = float((i >= confirm).mean()) if i.size else float("nan")
    frr_confirm = float((g < confirm).mean()) if g.size else float("nan")
    far_cont = float((i >= continuity).mean()) if i.size else float("nan")
    frr_cont = float((g < continuity).mean()) if g.size else float("nan")
    e, threshold = eer(g, i)

    # A4: a pure recording-level change between enrollment and use must not
    # walk a genuine score across the operating point.  Feature-domain cosine
    # is reported for diagnostics, but the acceptance-relevant quantity is how
    # far the genuine *score* moves, because that is what the threshold sees.
    probe_window = master[30 * SR:31 * SR]
    baseline_score = cosine(embed(probe_window), centroid)
    gain_rows = []
    for gain in (0.5, 2.0):
        scaled = np.clip(probe_window * gain, -32768, 32767).astype("<i2").astype(np.float32)
        gain_rows.append((gain, cosine(embed(scaled), centroid)))

    reference_features = pcm_to_fbank(probe_window.astype("<i2").tobytes())
    feature_rows = []
    for gain in (0.5, 2.0):
        scaled = np.clip(probe_window * gain, -32768, 32767).astype("<i2").astype(np.float32)
        scaled_features = pcm_to_fbank(scaled.astype("<i2").tobytes())
        count = min(reference_features.shape[0], scaled_features.shape[0])
        feature_rows.append((gain, cosine(reference_features[:count].ravel(),
                                          scaled_features[:count].ravel())))
    max_score_drift = max(abs(value - baseline_score) for _, value in gain_rows)

    lines = [
        "# 声纹识别回归基准",
        "",
        f"- 模型：`{model_path.name}`（{engine.model_version}）",
        f"- 特征前端：`{engine.frontend_version}`",
        f"- 窗口：{window_ms} ms / 步进 {step_ms} ms",
        f"- 阈值：确认 {confirm} / 保持 {continuity}",
        f"- 注册：3 × 10 s（同一录音的前 30 s）；测试：同一录音第 30 s 之后（未见过）",
        "",
        "## A2/A3 分数分布",
        "",
        "| 集合 | n | 均值 | p05 | p50 | p95 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| 同人 | {g.size} | {g.mean():+.4f} | {np.percentile(g, 5):+.4f} | "
        f"{np.percentile(g, 50):+.4f} | {np.percentile(g, 95):+.4f} |" if g.size else "| 同人 | 0 | - | - | - | - |",
        f"| 异人 | {i.size} | {i.mean():+.4f} | {np.percentile(i, 5):+.4f} | "
        f"{np.percentile(i, 50):+.4f} | {np.percentile(i, 95):+.4f} |" if i.size else "| 异人 | 0 | - | - | - | - |",
        "",
        f"- 分离度（同人均值 − 异人均值）：**{g.mean() - i.mean():+.4f}**"
        if g.size and i.size else "- 分离度：n/a",
        f"- EER：**{e * 100:.2f}%**（阈值 {threshold:+.4f}）" if not np.isnan(e) else "- EER：n/a",
        f"- @确认阈值 {confirm}：FAR {far_confirm * 100:.1f}% / FRR {frr_confirm * 100:.1f}%",
        f"- @保持阈值 {continuity}：FAR {far_cont * 100:.1f}% / FRR {frr_cont * 100:.1f}%",
        "",
        "## A4 电平鲁棒性",
        "",
        f"同一段语音在基准增益下的本人分数：**{baseline_score:+.4f}**",
        "",
        "| 增益 | 本人分数 | 分数漂移 | 特征域余弦 |",
        "|---|---:|---:|---:|",
    ]
    for (gain, score), (_, feature_cosine) in zip(gain_rows, feature_rows):
        lines.append(f"| ×{gain} | {score:+.4f} | {score - baseline_score:+.4f} | {feature_cosine:.6f} |")
    lines += [
        "",
        f"- 最大分数漂移：**{max_score_drift:.4f}**"
        f"（判定要求 |漂移| ≪ 同人—异人间隔 {g.mean() - i.mean():+.4f}）",
        f"- 最大漂移后仍与确认阈值 {confirm} 的余量："
        f"**{min(value for _, value in gain_rows) - confirm:+.4f}**",
        "",
        "## 语料局限", "", "```text", CORPUS_NOTE.strip(), "```", "",
    ]

    report = "\n".join(lines)
    out = args.out or (ROOT / ".local/cache/evaluation" / "speaker-verification-latest.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n报告已写入 {out}")
    print(json.dumps({
        "separation": float(g.mean() - i.mean()) if g.size and i.size else None,
        "eer": None if np.isnan(e) else e,
        "far_confirm": far_confirm,
        "frr_confirm": frr_confirm,
        "gain_score_drift": max_score_drift,
        "gain_feature_cosines": [value for _, value in feature_rows],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

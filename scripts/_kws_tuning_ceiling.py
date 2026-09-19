"""Tuning-ceiling matrix for the KWS wake word.

Everything the *product settings* cannot change (90 s conversation window,
state machine, sensitivity=99 => threshold=0.01) is held fixed.  Only the
tunable detector inputs are varied:

    keywords_score      (boosting, currently 1.0 and not exposed in the UI)
    num_trailing_blanks (currently 1)
    max_active_paths    (currently hard-coded 4)
    the managed keyword entries (same @七三 label, several pronunciations)
    the wake phrase itself

Run:  python scripts/_kws_tuning_ceiling.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "logs" / "evaluation" / "kws-probe"
MANAGED = Path(r"D:\模型\voice-terminal\kws\sherpa-onnx-kws-zipformer-zh-en-3M\keywords.txt")
SCRIPTS = ["r1", "r2", "r3"]
GROUPS = {"qisan": "七三", "nihao_qisan": "你好七三", "qisan_tongxue": "七三同学"}
REPORT = PROBE / "tuning-ceiling.txt"


def wavs(*groups: str) -> list[str]:
    result: list[str] = []
    for group in groups:
        for index in SCRIPTS:
            result += ["--wav", str(PROBE / f"rend_{group}_{index}.wav")]
    return result


NEGATIVES: list[str] = []
for item in sorted((ROOT / "logs" / "evaluation" / "audio").glob("*.wav")):
    if any(token in item.name for token in ("long-stage4", "long25", "query-", "sustained")):
        NEGATIVES += ["--wav", str(item)]


def run(title: str, *, keywords: Path, score: float, blanks: int, paths: int,
        streams: list[str]) -> None:
    """Unused helper kept out of the way: the matrix below prints through emit()."""


def main() -> int:
    multi = PROBE / "keywords" / "七三-多词条.txt"
    alone = PROBE / "keywords" / "七三同学.txt"
    all_groups = wavs(*GROUPS)
    with REPORT.open("w", encoding="utf-8") as handle:
        handle.write("KWS 调参上限矩阵 (threshold=0.01 固定, SNR 12/8/5 dB, 每组 9 个样本)\n")

    def emit(title: str, *, keywords: Path, score: float, blanks: int, paths: int) -> None:
        with REPORT.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {title} ===\n")
            handle.flush()
        command = [
            sys.executable, str(ROOT / "scripts" / "probe_kws_recall.py"),
            "--mode", "wake", "--keywords-file", str(keywords),
            "--sweep-threshold", "0.01", "--sweep-score", str(score),
            "--blanks", str(blanks), "--max-active-paths", str(paths),
            "--snr-db", "12,8,5", "--trials", "3", "--verbose", *all_groups,
        ]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        with REPORT.open("a", encoding="utf-8") as handle:
            handle.write(result.stdout or "")
            handle.write(result.stderr or "")
        print(f"完成: {title}", flush=True)

    emit("A 现状: 七三 / score1.0 / blanks1 / paths4", keywords=MANAGED, score=1.0, blanks=1, paths=4)
    emit("B 提分: 七三 / score1.5", keywords=MANAGED, score=1.5, blanks=1, paths=4)
    emit("C 提分: 七三 / score2.0", keywords=MANAGED, score=2.0, blanks=1, paths=4)
    emit("D 多词条: 七三+你好七三+七三同学 / score2.0 / blanks1", keywords=multi, score=2.0, blanks=1, paths=4)
    emit("E 多词条+blanks3", keywords=multi, score=2.0, blanks=3, paths=4)
    emit("F 多词条+paths8", keywords=multi, score=2.0, blanks=1, paths=8)
    emit("G 换词: 七三同学 / score2.0", keywords=alone, score=2.0, blanks=1, paths=4)

    # False accepts on ~7.3 minutes of wake-word-free Mandarin speech.
    for title, keywords, score, blanks, paths in (
        ("A 现状", MANAGED, 1.0, 1, 4),
        ("C 提分2.0", MANAGED, 2.0, 1, 4),
        ("D 多词条", multi, 2.0, 1, 4),
        ("E 多词条+blanks3", multi, 2.0, 3, 4),
        ("G 换词 七三同学", alone, 2.0, 1, 4),
    ):
        command = [
            sys.executable, str(ROOT / "scripts" / "probe_kws_recall.py"),
            "--mode", "wake", "--keywords-file", str(keywords),
            "--sweep-threshold", "0.01", "--sweep-score", str(score),
            "--blanks", str(blanks), "--max-active-paths", str(paths), *NEGATIVES,
        ]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        line = next((item for item in (result.stdout or "").splitlines() if item.startswith("t=")), "")
        with REPORT.open("a", encoding="utf-8") as handle:
            handle.write(f"\n误触发 {title}: {line.strip()}\n")
        print(f"误触发 {title}: {line.strip()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

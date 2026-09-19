"""Turn verbose probe output into per-phrase recall tables.

Reads the raw ``--verbose`` transcripts produced by
``scripts/probe_kws_recall.py`` and aggregates them per wake phrase and SNR, so
"the user says X" and "which entries are registered" stay separable.

Usage:  python scripts/_kws_matrix_report.py logs/evaluation/kws-probe/tuning-ceiling.txt ...
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SECTION = re.compile(r"^=== (.+?) ===$")
HIT = re.compile(r"^\s*(HIT|MISS)\s+(rend_(\w+?)_(r\d)\.wav)(#\d+)? \[([^\]]+)\]")
GROUP_LABEL = {"qisan": "七三", "nihao_qisan": "你好七三", "qisan_tongxue": "七三同学"}
SNR = re.compile(r"SNR(\d+)dB")


def parse(path: Path) -> dict[str, dict[tuple[str, int], list[int]]]:
    sections: dict[str, dict[tuple[str, int], list[int]]] = {}
    title: str | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        header = SECTION.match(line.strip())
        if header:
            title = header.group(1)
            sections.setdefault(title, {})
            continue
        found = HIT.match(line)
        if not found or title is None:
            continue
        hit, _name, group, _rend, _seed, variant = found.groups()
        snr = SNR.search(variant)
        if snr is None:
            continue
        key = (GROUP_LABEL.get(group, group), int(snr.group(1)))
        bucket = sections[title].setdefault(key, [0, 0])
        bucket[0] += 1 if hit == "HIT" else 0
        bucket[1] += 1
    return sections


def main(argv: list[str]) -> int:
    for item in argv:
        path = Path(item)
        if not path.is_file():
            print(f"跳过不存在的文件: {path}")
            continue
        sections = parse(path)
        print(f"\n########## {path.name} ##########")
        for title, table in sections.items():
            if not table:
                continue
            print(f"\n{title}")
            snrs = sorted({snr for _group, snr in table}, reverse=True)
            print("  说的词 \\ SNR   " + "  ".join(f"{snr:>7}dB" for snr in snrs))
            for group in GROUP_LABEL.values():
                cells = []
                for snr in snrs:
                    hits, total = table.get((group, snr), [0, 0])
                    cells.append(f"{hits:>3}/{total:<3}" if total else "   -   ")
                print(f"  {group:<12} " + "  ".join(f"{cell:>9}" for cell in cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Merge the daily wake/ASR timeline for diagnosing KWS wake failures."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(r"D:\项目开发\语音前端\logs")
DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-09-19"


def stamp(line: str) -> str:
    match = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
    return match.group(1) if match else ""


events: list[tuple[str, str, str]] = []

server = (ROOT / "voice-server.log").read_text(encoding="utf-8", errors="replace").splitlines()
seen: set[str] = set()
for line in server:
    if not line.startswith(DAY):
        continue
    wake = re.search(r"turn=(wake-[0-9a-f]+)", line)
    if wake and wake.group(1) not in seen:
        seen.add(wake.group(1))
        events.append((stamp(line), "KWS唤醒", wake.group(1)[:12]))
    for pattern, label in (
        (r"input_owner=(\w+)", "所有权"),
        (r"wake_detection_ms=(\S+)", "唤醒耗时"),
    ):
        found = re.search(pattern, line)
        if found:
            events.append((stamp(line), label, found.group(1)))

asr = (ROOT / "asr-worker.log").read_text(encoding="utf-8", errors="replace").splitlines()
for line in asr:
    if not line.startswith(DAY):
        continue
    final = re.search(r"asr\.worker\.result .*type=asr\.final text_len=(\d+) text='(.*)'", line)
    if final:
        events.append((stamp(line), "ASR结果", f"len={final.group(1)} {final.group(2)}"))
    started = re.search(r"asr\.worker\.turn_start", line)
    if started:
        events.append((stamp(line), "ASR开始", ""))

events.sort(key=lambda item: item[0])
print(f"{'时间':<20} {'事件':<9} 详情")
for when, kind, detail in events:
    print(f"{when:<20} {kind:<9} {detail}")
print(f"\n合计: KWS唤醒={sum(1 for e in events if e[1]=='KWS唤醒')} "
      f"ASR轮次={sum(1 for e in events if e[1]=='ASR开始')} "
      f"空结果={sum(1 for e in events if e[1]=='ASR结果' and e[2].startswith('len=0'))}")

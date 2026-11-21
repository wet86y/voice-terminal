"""Trigger one real Shadow activation through the product's own debug entry.

The project exposes `shadow.debug.wake` on the Voice Terminal WebSocket
(`voice_terminal/server/websocket.py`) and `config/local.yaml` enables it. This
drives that command against the running server and reports the Shadow-side events
it emits, so the DSH-backed lane can be verified end to end.

After the activation settles, inspect what it wrote:

    node scripts/dsh-diagnose/find-null-kind-rows.mjs
    node scripts/dsh-diagnose/inspect-session.mjs ".local/data/dsh/sessions/<project>/<session>"

Usage: python scripts/dsh-diagnose/trigger-shadow-wake.py [timeout-seconds]
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import websockets

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
TIMEOUT_S = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0


async def main() -> int:
    node = os.environ.get("VOICE_NODE_EXECUTABLE") or shutil.which("node")
    if not node:
        raise RuntimeError("诊断需要当前项目已有的 Node（脚本运行环境）。")
    result = await asyncio.to_thread(subprocess.run,
        [node, str(ROOT / "scripts/dsh-diagnose/lib/current-endpoints.mjs"), "voice"],
        cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "正式宿主尚未提供语音端点。")
    origin = result.stdout.strip()
    from urllib.parse import urlsplit
    address = urlsplit(origin)
    if address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port:
        raise RuntimeError("正式调试回执中的语音端点无效。")
    url = f"ws://{address.netloc}/ws"  # url（当前实际语音实时接口）。
    request_id = str(uuid.uuid4())
    print(f"connecting {url}")
    events: list[dict] = []
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as socket:
        await socket.send(json.dumps({
            "type": "command", "request_id": str(uuid.uuid4()),
            "command": "subscribe", "payload": {},
        }))
        while True:
            first = json.loads(await asyncio.wait_for(socket.recv(), timeout=20))
            if first.get("type") == "runtime.snapshot":
                break
        payload = first.get("payload") or {}
        dsh = payload.get("dsh") or {}
        print("dsh:", json.dumps({k: dsh.get(k) for k in ("enabled", "state", "ui_origin")}, ensure_ascii=False))
        shadow = payload.get("shadow") or {}
        print("shadow:", json.dumps(shadow, ensure_ascii=False)[:300])

        await socket.send(json.dumps({
            "type": "command", "request_id": request_id,
            "command": "shadow.debug.wake", "payload": {},
        }))
        print("wake sent, collecting events…")
        deadline = asyncio.get_running_loop().time() + TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            remaining = max(1.0, deadline - asyncio.get_running_loop().time())
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=min(remaining, 20.0))
            except asyncio.TimeoutError:
                continue
            message = json.loads(raw)
            kind = message.get("type") or "?"
            if kind in {"error", "shadow.debug.wake", "shadow.debug.event"}:
                print(f"  [{kind}] {json.dumps(message.get('payload'), ensure_ascii=False)[:400]}")
            events.append({"kind": kind})
            if kind == "error":
                print("server refused the wake")
                break
            if kind == "shadow.debug.wake":
                result = (message.get("payload") or {}).get("result")
                print("wake result:", result)
                break

    counts: dict[str, int] = {}
    for item in events:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    print("event types seen:", json.dumps(counts, ensure_ascii=False))
    print(f"repo root: {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

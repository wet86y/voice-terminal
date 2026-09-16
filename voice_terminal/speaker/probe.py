"""Application-level readiness probe for the optional Speaker Worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def _display_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


async def probe(url: str, timeout_s: float = 2.0) -> tuple[bool, dict[str, Any]]:
    started = time.perf_counter()
    request_id = uuid.uuid4().hex
    try:
        import websockets  # type: ignore

        async with websockets.connect(
            url,
            open_timeout=max(0.05, timeout_s),
            close_timeout=0.2,
            ping_interval=None,
        ) as socket:
            await socket.send(json.dumps({"type": "health", "request_id": request_id}))
            raw = await asyncio.wait_for(socket.recv(), max(0.05, timeout_s))
            if isinstance(raw, bytes):
                raise ValueError("health 响应不能是二进制")
            response = json.loads(raw)
            if not isinstance(response, dict) or response.get("request_id") != request_id:
                raise ValueError("health 响应 request_id 不匹配")
            ready = (
                response.get("type") == "health"
                and response.get("status") == "READY"
                and response.get("model_loaded") is True
            )
            payload: dict[str, Any] = {
                "url": _display_url(url),
                "status": response.get("status", "UNKNOWN"),
                "model_loaded": response.get("model_loaded") is True,
                "model_version": response.get("model_version"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            if response.get("error"):
                payload["error"] = str(response["error"])
            if not ready:
                payload["code"] = "SPEAKER_WORKER_NOT_READY"
            return ready, payload
    except asyncio.TimeoutError:
        return False, {
            "url": _display_url(url),
            "status": "TIMEOUT",
            "model_loaded": False,
            "code": "SPEAKER_WORKER_TIMEOUT",
            "error": f"health 超时（{timeout_s:g}s）",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except json.JSONDecodeError as exc:
        return False, {
            "url": _display_url(url),
            "status": "ERROR",
            "model_loaded": False,
            "code": "SPEAKER_WORKER_PROTOCOL_ERROR",
            "error": f"health 响应 JSON 无效：{exc}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except ValueError as exc:
        return False, {
            "url": _display_url(url),
            "status": "ERROR",
            "model_loaded": False,
            "code": "SPEAKER_WORKER_PROTOCOL_ERROR",
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:
        return False, {
            "url": _display_url(url),
            "status": "UNAVAILABLE",
            "model_loaded": False,
            "code": "SPEAKER_WORKER_UNAVAILABLE",
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Speaker Worker WebSocket URL")
    parser.add_argument("--timeout", type=float, default=2.0, help="health timeout in seconds")
    args = parser.parse_args()
    ready, payload = asyncio.run(probe(args.url, args.timeout))
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

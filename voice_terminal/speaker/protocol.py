from __future__ import annotations

import base64
from typing import Any


def encode_pcm(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def decode_pcm(value: Any, *, max_bytes: int = 10 * 16000 * 2) -> bytes:
    if not isinstance(value, str):
        raise ValueError("speaker PCM must be base64 text")
    if len(value) > max(128, int(max_bytes) * 2):
        raise ValueError("speaker PCM payload is too large")
    decoded = base64.b64decode(value, validate=True)
    if not decoded:
        raise ValueError("speaker PCM must not be empty")
    if len(decoded) % 2:
        raise ValueError("speaker PCM must be int16 aligned")
    if len(decoded) > max(1, int(max_bytes)):
        raise ValueError("speaker PCM payload is too large")
    return decoded

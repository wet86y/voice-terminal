from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AudioChunk:
    turn_id: str
    pcm: bytes
    is_final: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "audio.chunk",
            "turn_id": self.turn_id,
            "pcm_base64": base64.b64encode(self.pcm).decode("ascii"),
            "is_final": self.is_final,
        }


def decode_audio_chunk(payload: dict[str, Any]) -> AudioChunk:
    turn_id = payload.get("turn_id")
    encoded = payload.get("pcm_base64")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("ASR audio.chunk 缺少 turn_id")
    if not isinstance(encoded, str):
        raise ValueError("ASR audio.chunk 缺少 pcm_base64")
    try:
        pcm = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("ASR audio.chunk 的 pcm_base64 无效") from exc
    return AudioChunk(turn_id=turn_id, pcm=pcm, is_final=payload.get("is_final") is True)


def asr_result(result_type: str, turn_id: str, text: str) -> dict[str, Any]:
    if result_type not in {"asr.partial", "asr.final"}:
        raise ValueError(f"未知 ASR 结果类型：{result_type}")
    return {"type": result_type, "turn_id": turn_id, "text": text}

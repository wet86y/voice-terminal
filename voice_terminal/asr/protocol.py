from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AudioChunk:
    turn_id: str
    pcm: bytes
    is_final: bool = False
    sent_at_monotonic_ns: int | None = None  # sent_at_monotonic_ns（本机单调时钟发送时间，纳秒）。

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": "audio.chunk",
            "turn_id": self.turn_id,
            "pcm_base64": base64.b64encode(self.pcm).decode("ascii"),
            "is_final": self.is_final,
        }
        if self.sent_at_monotonic_ns is not None:
            payload["sent_at_monotonic_ns"] = self.sent_at_monotonic_ns
        return payload


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
    sent_at = payload.get("sent_at_monotonic_ns")
    if sent_at is not None and (type(sent_at) is not int or sent_at <= 0):
        raise ValueError("ASR（语音识别）sent_at_monotonic_ns（本机发送时间）必须是正整数")
    return AudioChunk(turn_id=turn_id, pcm=pcm, is_final=payload.get("is_final") is True,
                      sent_at_monotonic_ns=sent_at)


def asr_result(result_type: str, turn_id: str, text: str) -> dict[str, Any]:
    if result_type not in {"asr.partial", "asr.final"}:
        raise ValueError(f"未知 ASR 结果类型：{result_type}")
    return {"type": result_type, "turn_id": turn_id, "text": text}

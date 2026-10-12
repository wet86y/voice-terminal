"""Enrollment protocol: prompt material and acceptance gates.

Enrollment deliberately asks for **three different continuous sentences** rather
than one prescribed line.  A controlled experiment on this machine (one TTS
voice, one renderer, one channel, only the text varying) showed that matching
the enrollment text inflates the score by ~0.10 cosine purely from content:
enrolling on one fixed phrase scored +0.9554 on that same phrase but +0.8552 on
different text, while the runtime always sees spontaneous speech that never
matches the prompt.  A fixed line therefore makes the settings-page self test
look better than the live path and pushes the calibrated threshold too high.

For the same reason the prompts are continuous sentences, not isolated short
phrases and not sustained vowels: enrolling on 10 s sentences left 1.3-2.4 s
isolated clips at only ~0.49 cosine, i.e. well outside the domain this model
was trained on.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

from .engine import WeSpeakerOnnxEngine
from .registry import (
    ENROLLMENT_MIN_SEGMENT_SPEECH_MS,
    ENROLLMENT_MIN_SPEECH_RATIO,
    ENROLLMENT_MIN_TOTAL_SPEECH_MS,
    ENROLLMENT_SELF_SIMILARITY_MIN,
)

#: Reading material shown during enrollment.  Three distinct sentences with
#: varied initials/finals and tones, each long enough that a natural reading
#: lasts roughly 6-8 seconds.  They exist to make the *content* diverse and to
#: keep the recording consistent; they are not a magic phrase.
ENROLLMENT_PROMPTS: tuple[dict[str, Any], ...] = (
    {
        "index": 0,
        "label": "第 1 段",
        "text": "今天下午三点，我们在楼下的咖啡店讨论下个月的出行安排。",
    },
    {
        "index": 1,
        "label": "第 2 段",
        "text": "他把窗户打开，让清晨凉快的风吹进安静的书房。",
    },
    {
        "index": 2,
        "label": "第 3 段",
        "text": "冰箱里还剩半盒草莓，记得明天早上带上再出门。",
    },
)

ENROLLMENT_REQUIRED_SEGMENTS = len(ENROLLMENT_PROMPTS)


class EnrollmentRejectedError(ValueError):
    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def enrollment_prompts() -> list[dict[str, Any]]:
    return [dict(item) for item in ENROLLMENT_PROMPTS]


def pairwise_similarity(embeddings: Sequence[Sequence[float]]) -> dict[str, Any]:
    """Pairwise cosine matrix and the weakest pair for one enrollment."""
    count = len(embeddings)
    matrix: list[list[float | None]] = []
    weakest: float | None = None
    weakest_pair: list[int] | None = None
    for i in range(count):
        row: list[float | None] = []
        for j in range(count):
            if i == j:
                row.append(1.0)
                continue
            if j < i:
                row.append(matrix[j][i])
                continue
            score = WeSpeakerOnnxEngine.similarity(
                tuple(float(x) for x in embeddings[i]),
                tuple(float(x) for x in embeddings[j]),
            )
            if not math.isfinite(score):
                score = -1.0
            row.append(score)
            if weakest is None or score < weakest:
                weakest = score
                weakest_pair = [i, j]
        matrix.append(row)
    return {
        "matrix": matrix,
        "min_similarity": weakest,
        "min_pair": weakest_pair,
    }


def check_segments(segments: Sequence[dict[str, Any]],
                   embeddings: Sequence[Sequence[float]], *, config=None) -> dict[str, Any]:
    """Reject an enrollment whose own segments disagree about the speaker.

    ``segments`` carries the per-segment quality measured at capture time.  A
    same-speaker enrollment through one device scores far above
    :data:`ENROLLMENT_SELF_SIMILARITY_MIN`; anything below means the microphone
    moved, someone else spoke, or the frontend is broken — all of which would
    otherwise be stored as a silently useless template.
    """
    min_segment = getattr(config, "enrollment_min_segment_speech_ms", ENROLLMENT_MIN_SEGMENT_SPEECH_MS)
    min_total = getattr(config, "enrollment_min_total_speech_ms", ENROLLMENT_MIN_TOTAL_SPEECH_MS)
    min_ratio = getattr(config, "enrollment_min_speech_ratio", ENROLLMENT_MIN_SPEECH_RATIO)
    min_similarity = getattr(config, "enrollment_self_similarity_min", ENROLLMENT_SELF_SIMILARITY_MIN)
    if len(embeddings) != len(segments):
        raise EnrollmentRejectedError("录音段与声纹向量数量不一致")
    short = [
        index for index, item in enumerate(segments)
        if int(item.get("speech_ms") or 0) < min_segment
    ]
    detail: dict[str, Any] = {
        "min_segment_speech_ms": min_segment,
        "min_total_speech_ms": min_total,
        "min_speech_ratio": min_ratio,
        "min_self_similarity": min_similarity,
        "segment_speech_ms": [int(item.get("speech_ms") or 0) for item in segments],
    }
    if short:
        detail["short_segments"] = short
        raise EnrollmentRejectedError(
            f"第 {'、'.join(str(i + 1) for i in short)} 段有效人声不足 "
            f"{min_segment // 1000} 秒，请放慢语速重新录制。",
            detail=detail,
        )
    total_speech = sum(int(item.get("speech_ms") or 0) for item in segments)
    detail["total_speech_ms"] = total_speech
    if total_speech < min_total:
        raise EnrollmentRejectedError(
            f"三段有效人声合计 {total_speech / 1000:.1f} 秒，低于 "
            f"{min_total // 1000} 秒。请把每段读满 6–10 秒，"
            "整段连续朗读、不要长时间停顿。",
            detail=detail,
        )
    # Only meaningful when the WebRTC VAD actually measured the segment; an
    # energy estimate is too gain-dependent to gate a voiced ratio on.
    thin = [
        index for index, item in enumerate(segments)
        if item.get("speech_measured_by_vad")
        and int(item.get("duration_ms") or 0) > 0
        and int(item.get("speech_ms") or 0) / int(item["duration_ms"]) < min_ratio
    ]
    if thin:
        detail["low_speech_ratio_segments"] = thin
        raise EnrollmentRejectedError(
            f"第 {'、'.join(str(i + 1) for i in thin)} 段大部分是静音或噪声，"
            f"有效人声占比低于 {min_ratio:.0%}，请在安静环境中连续朗读。",
            detail=detail,
        )
    scores = pairwise_similarity(embeddings)
    detail.update({
        "pairwise": scores["matrix"],
        "min_similarity": scores["min_similarity"],
        "min_pair": scores["min_pair"],
    })
    weakest = scores["min_similarity"]
    if weakest is None or weakest < min_similarity:
        pair = scores["min_pair"] or [0, 1]
        raise EnrollmentRejectedError(
            "三段录音彼此差异过大（最低相似度 "
            f"{0.0 if weakest is None else weakest:.2f} < {min_similarity:.2f}）："
            f"第 {pair[0] + 1} 段与第 {pair[1] + 1} 段不像同一个人。"
            "请确认三段由同一人、在同一设备与同一距离下连续录制。",
            detail=detail,
        )
    return detail


def centroid_of(embeddings: Sequence[Sequence[float]]) -> list[float]:
    """L2-normalised mean of already-normalised segment embeddings."""
    if np is None:
        raise RuntimeError("numpy unavailable")
    if not embeddings:
        raise EnrollmentRejectedError("没有可用的声纹向量")
    stack = np.stack([np.asarray(item, dtype=np.float32).reshape(-1) for item in embeddings])
    mean = stack.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if not math.isfinite(norm) or norm <= 1e-8:
        raise EnrollmentRejectedError("三段录音的平均声纹向量无效")
    return [float(x) for x in (mean / norm)]


__all__ = [
    "ENROLLMENT_PROMPTS",
    "ENROLLMENT_REQUIRED_SEGMENTS",
    "EnrollmentRejectedError",
    "centroid_of",
    "check_segments",
    "enrollment_prompts",
    "pairwise_similarity",
]

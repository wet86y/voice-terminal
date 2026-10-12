"""声纹可调参数的唯一默认值与校验入口。"""
from __future__ import annotations

import math
from typing import Any, Mapping

# 所有字段均由设置页持久化；毫秒字段使用单调时钟或不重复音频时长。
TUNING_DEFAULTS: dict[str, float | int] = {
    "continuity_window_ms": 5000,  # 连续主人进入时限。
    "continuity_max_ms": 10000,  # 连续主人最长时限。
    "non_owner_threshold": 0.35,  # 持续非主人阈值。
    "non_owner_ticks": 3,  # 连续有效反证心跳数。
    "context_max_ms": 3200,  # 增长式分析上下文上限。
    "recovery_max_distance": 0.25,  # 跨轮恢复最大余弦距离。
    "pending_ttl_ms": 30000,  # 待确认文本保留期限。
    "pending_max_turns": 8,  # 待确认轮次数量上限。
    "calibration_speech_ms": 3200,  # 临时校准有效语音量。
    "calibration_max_ms": 10000,  # 激活后最长无条件放行时间。
    "calibration_similarity": 0.65,  # 临时样本与候选质心最低相似度。
    "session_weight": 0.60,  # 会话声纹评分权重。
    "enrollment_min_segment_speech_ms": 3000,  # 永久注册单段最低人声。
    "enrollment_min_total_speech_ms": 15000,  # 永久注册最低总人声。
    "enrollment_min_speech_ratio": 0.50,  # 永久注册最低人声比例。
    "enrollment_self_similarity_min": 0.70,  # 永久注册样本一致性。
    "enrollment_max_clipped_ratio": 0.005,  # 永久注册最大削波比例。
}
BOUNDS = {
    "continuity_window_ms": (0, 60000), "continuity_max_ms": (0, 120000),
    "non_owner_threshold": (-1, 1), "non_owner_ticks": (1, 20),
    "context_max_ms": (45, 16000), "recovery_max_distance": (0, 2),
    "pending_ttl_ms": (400, 300000), "pending_max_turns": (1, 64),
    "calibration_speech_ms": (45, 30000), "calibration_max_ms": (400, 60000),
    "calibration_similarity": (-1, 1), "session_weight": (0, 1),
    "enrollment_min_segment_speech_ms": (45, 15000),
    "enrollment_min_total_speech_ms": (45, 45000),
    "enrollment_min_speech_ratio": (0.01, 1),
    "enrollment_self_similarity_min": (-1, 1), "enrollment_max_clipped_ratio": (0, 1),
}


def validate_tuning(value: Mapping[str, Any], *, continuity: float) -> dict[str, float | int]:
    """原子校验；无效组合不可悄悄夹紧后保存。"""
    result: dict[str, float | int] = {}
    for key, default in TUNING_DEFAULTS.items():
        item = value.get(key, default)
        low, high = BOUNDS[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError(f"{key}（声纹参数）必须是有限数字")
        if type(default) is int and int(item) != item:
            raise ValueError(f"{key}（声纹时长或计数）必须是整数")
        if not low <= item <= high:
            raise ValueError(f"{key}（声纹参数）必须在 {low} 至 {high} 之间")
        result[key] = int(item) if type(default) is int else float(item)
    if result["non_owner_threshold"] >= continuity:
        raise ValueError("持续非主人阈值必须低于连续主人阈值")
    if result["continuity_max_ms"] < result["continuity_window_ms"]:
        raise ValueError("最长连续主人时限不能小于进入时限")
    if result["calibration_max_ms"] < result["calibration_speech_ms"]:
        raise ValueError("校准最长时间不能小于目标有效语音量")
    if result["enrollment_min_total_speech_ms"] < result["enrollment_min_segment_speech_ms"]:
        raise ValueError("永久注册总人声要求不能小于单段要求")
    return result

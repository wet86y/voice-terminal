"""声纹可调参数的唯一默认值与校验入口。"""
from __future__ import annotations

import math
from typing import Any, Mapping

# 隐藏参数继续持久化；毫秒字段使用单调时钟或不重复音频时长。
TUNING_DEFAULTS: dict[str, float | int] = {
    "intra_turn_relaxation": 0.10,  # 已确认主人后的轮内放宽量，与跨轮量独立。
    "continuity_window_ms": 5000,  # 连续主人进入时限。
    "continuity_max_ms": 10000,  # 连续主人最长时限。
    "non_owner_ticks": 3,  # 连续有效反证心跳数。
    "context_max_ms": 3200,  # 增长式分析上下文上限。
    "recovery_max_distance": 0.25,  # 跨轮恢复最大余弦距离。
    "pending_ttl_ms": 30000,  # 待确认文本保留期限。
    "pending_max_turns": 8,  # 待确认轮次数量上限。
    "calibration_speech_ms": 3200,  # 临时校准有效语音量。
    "calibration_max_ms": 10000,  # 校准最长时间；独立打断保留校准临时授权。
    "calibration_similarity": 0.65,  # 临时样本与候选质心最低相似度。
    "session_weight": 0.60,  # 会话声纹评分权重。
    "enrollment_min_segment_speech_ms": 3000,  # 永久注册单段最低人声。
    "enrollment_min_total_speech_ms": 15000,  # 永久注册最低总人声。
    "enrollment_min_speech_ratio": 0.50,  # 永久注册最低人声比例。
    "enrollment_self_similarity_min": 0.70,  # 永久注册样本一致性。
    "enrollment_max_clipped_ratio": 0.005,  # 永久注册最大削波比例。
}
BOUNDS = {
    "intra_turn_relaxation": (0, 0.30),
    "continuity_window_ms": (0, 60000), "continuity_max_ms": (0, 120000),
    "non_owner_ticks": (1, 20),
    "context_max_ms": (45, 16000), "recovery_max_distance": (0, 2),
    "pending_ttl_ms": (400, 300000), "pending_max_turns": (1, 64),
    "calibration_speech_ms": (45, 30000), "calibration_max_ms": (400, 60000),
    # 旧配置允许权重至1；兼容读取保留原值，新保存由可见控件范围限制至0.80。
    "calibration_similarity": (-1, 1), "session_weight": (0, 1),
    "enrollment_min_segment_speech_ms": (45, 15000),
    "enrollment_min_total_speech_ms": (45, 45000),
    "enrollment_min_speech_ratio": (0.01, 1),
    "enrollment_self_similarity_min": (-1, 1), "enrollment_max_clipped_ratio": (0, 1),
}


CONTROL_BOUNDS = {  # CONTROL_BOUNDS（四项可见控件的保存范围及中文名称）。
    "confirm_threshold": ("主人确认阈值", 0, 1),
    "intra_turn_relaxation": ("会话内连续放宽余量", 0, 0.30),
    "continuity_relaxation": ("会话间连续放宽余量", 0, 0.20),
    "session_weight": ("临时声纹权重", 0, 0.80),
}


def validate_controls(value: Mapping[str, Any], *, check_weight: bool = True) -> None:
    """校验四项控件；check_weight（检查新提交权重）不用于旧配置读取。"""
    for key, (label, low, high) in CONTROL_BOUNDS.items():
        if key == "session_weight" and not check_weight:
            continue  # 未提交权重的旧客户端继续保留已有值。
        item = value[key]
        if type(item) not in {int, float} or not math.isfinite(item) or not low <= item <= high:
            raise ValueError(f"{label}必须在 {low} 至 {high} 之间")
    confirm = value["confirm_threshold"]
    for key, scope in (("intra_turn_relaxation", "会话内"), ("continuity_relaxation", "会话间")):
        if value[key] > confirm:
            raise ValueError(f"{scope}连续放宽余量不能大于主人确认阈值，否则放宽后的声纹阈值会小于0")


def validate_tuning(value: Mapping[str, Any]) -> dict[str, float | int]:
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
    if result["continuity_max_ms"] < result["continuity_window_ms"]:
        raise ValueError("最长连续主人时限不能小于进入时限")
    if result["calibration_max_ms"] < result["calibration_speech_ms"]:
        raise ValueError("校准最长时间不能小于目标有效语音量")
    if result["enrollment_min_total_speech_ms"] < result["enrollment_min_segment_speech_ms"]:
        raise ValueError("永久注册总人声要求不能小于单段要求")
    return result

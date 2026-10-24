"""声纹诊断：复用异步内容审计通道，不在音频路径同步写文件。"""
from __future__ import annotations

from dataclasses import asdict
import json
import logging
import time

from .settings import TUNING_DEFAULTS

logger = logging.getLogger(__name__)
content_logger = logging.getLogger("voice_terminal.llm_context_audit")

LABELS = {
    "intra_turn_relaxation": "轮次内主人放宽量", "intra_turn_threshold": "派生轮内主人阈值",
    "threshold_scope": "实际阈值适用范围", "continuity": "跨轮连续资格",
    "continuity_grant": "开口时的连续资格快照", "source_turn_id": "资格来源轮次",
    "normal_turn_id": "正常主人证据轮次", "normal_end_at": "正常主人轮次语音终点",
    "continuity_epoch": "连续资格代次", "continuity_deadline": "本次资格截止时刻",
    "captured_at": "实际开口快照时刻", "entry_deadline": "连续进入截止时刻",
    "max_deadline": "连续最长截止时刻", "continuing": "已承接连续资格",
    "boundary_at": "实际语音结束时刻", "boundary_revision": "候选边界版本",
    "boundary_reason": "输入边界来源", "disposition": "资格交接结果",
    "continuity_source": "跨轮资格来源轮次",
    "turn_id": "语音轮次编号", "generation": "输入代次", "activation_id": "激活编号",
    "settings_revision": "设置版本", "sequence": "心跳序号", "phase": "准入阶段",
    "admission_phase": "本轮准入阶段", "initial_identity": "首段身份", "admitted": "本轮获准",
    "owner_confirmed": "主人已确认", "recovered": "初判已翻转",
    "observation_state": "当次身份观察状态", "enrollment_id": "声纹登记事务编号",
    "foreign_voice_suspected": "疑似外来声纹", "reason": "判定原因", "closed": "轮次已结束",
    "positive_ticks": "连续主人次数", "negative_ticks": "连续非主人次数",
    "permanent_score": "永久声纹分数", "session_score": "会话声纹分数",
    "combined_score": "组合分数", "effective_session_weight": "实际会话权重",
    "threshold_used": "实际主人阈值", "confirm_threshold": "声纹放行基准值",
    "continuity_threshold": "派生跨轮主人阈值", "continuity_relaxation": "跨轮次主人放宽量",
    "identity_result": "按当次阈值二分的身份结果，空值表示无有效评分", "non_owner_ticks": "异声连续检测次数",
    "session_weight": "设置的会话声纹权重", "context_max_ms": "滚动上下文上限毫秒",
    "start_sample": "分析起点样本", "end_sample": "分析终点样本",
    "speech_ms": "有效人声毫秒", "new_speech_ms": "新增人声毫秒",
    "turn_speech_ms": "本轮不重复有效人声毫秒", "observation_count": "本轮新音频判断次数",
    "latency_ms": "推理耗时毫秒", "quality": "音频质量", "source": "证据来源",
    "stage": "文字阶段", "text": "完整文字", "previous_text": "前一修订完整文字",
    "revision": "修订编号", "chars": "文字码点数", "changed_start": "变化起点码点",
    "previous_changed_end": "前一修订变化终点", "changed_end": "当前变化终点",
    "parameters": "参数快照", "submitted": "提交值", "saved": "保存值",
    "effective": "运行时应用值", "effects": "参数适用范围与生效时点",
    "calibration_config": "本次激活校准参数", "mode": "声纹模式",
    "owner_only_barge_in": "仅主人可打断", "outcome": "处理结果",
    "at_monotonic": "单调时钟秒", "timings": "轮次时间线", "first": "首段推理",
    "object_kind": "适用对象", "settings_applied": "设置已应用",
    "record_type": "记录类型", "version": "记录格式版本", "kind": "事件类型", "at": "时间戳秒",
    "speaker_summary": "本轮声纹摘要", "interpretation": "声纹参考说明", "raw_text": "原始全文",
    "speech_id": "播报事务编号", "request_id": "请求编号", "status": "处理状态",
    "latency": "延迟测量", "delivery": "实际播放结果", "count": "条目数量",
    "source_revision": "来源修订号", "input_finished_at": "输入完成单调时钟",
    "continuity_window_ms": "连续主人进入时限毫秒", "continuity_max_ms": "连续主人最长时限毫秒",
    "recovery_max_distance": "兼容片段恢复距离", "pending_ttl_ms": "闭段记录期限毫秒",
    "pending_max_turns": "闭段记录数上限", "calibration_speech_ms": "校准有效人声量毫秒",
    "calibration_max_ms": "校准最长时间毫秒", "calibration_similarity": "校准样本一致性阈值",
    "enrollment_min_segment_speech_ms": "登记单段最低人声毫秒",
    "enrollment_min_total_speech_ms": "登记合计最低人声毫秒",
    "enrollment_min_speech_ratio": "登记最低人声比例",
    "enrollment_self_similarity_min": "登记样本最低一致性",
    "enrollment_max_clipped_ratio": "登记最大削波比例",
    "turn_started": "语音轮次开始", "speaker_first_ready": "首段音频就绪",
    "speaker_inference_started": "首段推理开始", "speaker_inference_completed": "首段推理完成",
    "speaker_admitted": "本轮身份放行", "asr_first_text": "识别首文字", "asr_final": "识别定稿",
    "speaker_wait_finished": "身份等待结束（放行或最终拦截）",
    "commit": "输入提交", "last_speech": "最后有效发声", "endpoint_accepted": "断句确认",
    "llm_request": "模型请求开始", "first_token": "模型首词元", "first_tts_text": "首合成文字",
    "first_audio": "合成首音频", "playback": "实际播放开始", "first_pcm_submitted": "播放器首帧",
    "completed": "最终完成",
    "episode_id": "逻辑会话编号", "output_generation": "输出代次", "input_generation": "输入代次",
    "speaker_wait_ms": "身份等待毫秒", "speaker_inference_ms": "首段推理耗时毫秒",
    "total_ms": "整轮完成耗时毫秒", "response_ms": "首音响应耗时毫秒",
    "asr_first_text_ms": "识别首文字耗时毫秒", "speech_end_confirmation_ms": "断句确认耗时毫秒",
    "asr_final_to_commit_ms": "识别定稿到提交毫秒", "llm_first_token_ms": "模型首词元耗时毫秒",
    "llm_request_to_first_tts_text_ms": "模型请求到首合成文字毫秒",
    "tts_text_to_first_audio_ms": "合成文字到首音频毫秒",
    "first_audio_to_playback_ms": "合成首音频到实际播放毫秒",
    "turn_total_ms": "整轮完成耗时毫秒", "first_segment_ms": "首段识别耗时毫秒",
    "wake_detection_ms": "唤醒检测耗时毫秒", "smart_turn_ms": "智能断句耗时毫秒",
    "sensevoice_refine_ms": "兼容离线精修耗时毫秒", "refine_duration_ms": "精修耗时毫秒",
    "refine_to_redecision_ms": "精修完成到重新决策毫秒", "agent_request_to_speak_ms": "模型请求到播报请求毫秒",
    "tts_prewarm_requested_ms": "合成预热请求时刻毫秒", "tts_prewarm_ready_ms": "合成预热就绪耗时毫秒",
    "tts_checkout_ms": "合成预热实例取用耗时毫秒", "tts_cold_start_ms": "合成冷启动耗时毫秒",
    "speech_end_to_playback_ms": "语音结束到实际播放毫秒", "speech_end_to_first_pcm_ms": "语音结束到播放器首帧毫秒",
    "first_pcm_to_player_submit_ms": "首音频到播放器提交毫秒", "replacement_count": "输出替换次数",
    "wake_to_cue_first_pcm_ms": "唤醒到提示音首帧毫秒", "wake_to_llm_request_ms": "唤醒到模型请求毫秒",
    "wake_to_asr_ready_ms": "唤醒到识别就绪毫秒", "wake_to_buffer_drained_ms": "唤醒到缓存回放完成毫秒",
    "wake_to_tts_prewarm_ms": "唤醒到合成预热毫秒", "wake_to_confirmation_first_pcm_ms": "唤醒到确认语首帧毫秒",
    "first_speech_text_to_json_finish_ms": "首播报文字到响应结构完成毫秒",
    "speech_started_before_envelope_commit": "播报早于响应结构提交", "partial_speech_protocol_error": "增量播报协议异常",
    "partial_speech_tool_recovery": "增量播报工具恢复", "speak_requested": "播报请求时刻",
    "tts_prewarm_used": "使用合成预热",
}


def parameter_snapshot(config) -> dict:
    """只收录声纹参数；不遍历系统配置和凭据。"""
    values = asdict(config) if hasattr(config, "__dataclass_fields__") else dict(config) if isinstance(config, dict) else vars(config)
    return {key: values[key] for key in (*TUNING_DEFAULTS, "mode", "confirm_threshold",
            "continuity_threshold", "continuity_relaxation", "owner_only_barge_in") if key in values}


def parameter_effects() -> dict:
    return {
        "next_judgment（下次判断）": ["confirm_threshold", "intra_turn_relaxation", "continuity_relaxation",
            "non_owner_ticks", "session_weight", "context_max_ms"],
        "next_turn_start（下次语音开口）": ["continuity_window_ms", "continuity_max_ms"],
        "next_activation（下次激活）": ["calibration_speech_ms", "calibration_max_ms", "calibration_similarity"],
        "next_enrollment（下次登记）": [key for key in TUNING_DEFAULTS if key.startswith("enrollment_")],
        "closed_pending_records（已闭合待处理记录）": ["pending_ttl_ms", "pending_max_turns"],
        "legacy_observation_only（兼容观察路径，新拦截准入不使用）": [
            "recovery_max_distance"],
    }


def audit(kind: str, *, content: bool = False, **fields) -> None:
    """kind（事件类型）；内容与控制诊断共用关联键，但全文只进内容通道。"""
    try:
        record = {"record_type": "speaker_audit", "version": 1, "kind": kind,
                  "at": time.time(), "at_monotonic": time.monotonic(), **fields}
        keys = set()
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    keys.add(str(key))
                    visit(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)
        visit(record)
        record["field_labels"] = {key: LABELS.get(key, f"{key}（声纹诊断字段）") for key in sorted(keys)}
        # 枚举原值用于机器关联，中文对照用于导出日志人工查阅。
        record["value_labels"] = {
            "normal": "正常基准值", "intra_turn": "本轮放宽", "cross_turn": "跨轮放宽",
            "unified_turn_policy": "正式轮次使用统一会话策略",
            "observing": "观察且不拦截", "normal_owner_boundary": "正常主人轮次终点",
            "continued_without_renewal": "承接放宽但不续期", "no_previous_owner": "没有前轮主人证据",
            "sustained_non_owner": "连续明确非主人撤销后续资格",
            "renewed": "更新正常主人资格", "continued": "承接原资格", "unchanged": "资格原样保留",
            "smart_turn": "智能断句", "hard_silence": "硬静音兜底", "max_duration": "最长时长强制分轮",
            "vad_end": "语音检测闭段", "cancelled": "取消", "stopped": "停止", "reset": "重置", "failed": "输入故障",
            "collecting": "首段积累或延长检查", "inferring": "首段推理中", "held": "声纹暂缓放行",
            "relaxed": "主人确认后放宽", "unconfirmed": "身份未确认但已放行",
            "closed": "已结束", "owner": "主人", "non_owner": "非主人", "unknown": "未知",
            "owner_confirmed": "明确主人", "owner_continuity": "放宽阈值的连续主人",
            "non_owner_confirmed": "明确非主人", "off": "关闭", "observe": "仅标记", "filter": "拦截",
            "speaker_blocked": "声纹拦截结算", "empty_input": "真实空输入", "completed": "已完成",
            "interrupted": "已中断", "initializing": "初始化期间延后应用",
            "ready": "音频可用", "unavailable": "服务不可用", "input": "普通输入",
            "barge": "打断候选", "idle": "空闲观察",
            "too_quiet": "音量过低", "insufficient_audio": "音频不足", "clipped": "削波",
            "first_owner_confirmed": "首段确认主人", "first_non_owner_held": "首段明确非主人暂缓",
            "first_identity_uncertain": "首段身份不确定，等待新增音频",
            "first_identity_timeout": "开口四秒仍未确认身份，执行拦截",
            "first_identity_unresolved_at_end": "闭段仍未确认身份，执行拦截",
            "first_decision_finished": "首段等待已结束，不采用迟到结果",
            "initial_non_owner_overturned": "初判非主人已推翻",
            "non_owner_observed_after_admission": "放行后检测连续异声", "awaiting_first_audio": "等待首段音频",
            "no_new_audio": "没有新增音频", "settings_changed": "设置版本已变化",
            "turn_closed_or_activation_changed": "轮次结束或激活范围变化",
            "closed_record_budget": "已关闭记录达到留存预算", "runtime_apply_failed": "运行设置应用失败",
            "raw_asr": "原始识别全文", "manual": "手动激活", "system": "系统激活",
            "wake": "唤醒激活", "speaker_recovery": "声纹准入恢复", "paraformer": "主识别器",
            "inference_in_progress": "已有推理在途", "not_speech": "没有新增人声", "busy": "推理繁忙",
        }
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        content_logger.info(serialized)
        if not content:
            logger.info("speaker.audit（声纹运行诊断） %s", serialized)
    except Exception:
        logger.warning("speaker.audit_failed（声纹诊断写入失败）", exc_info=True)


def transcript_audit(turn_id: str, stage: str, text: str, previous: str, revision: int, **fields) -> None:
    """保存全文与变化区间；调用方只在文字修订或路由变化时调用。"""
    start = 0
    while start < min(len(text), len(previous)) and text[start] == previous[start]:
        start += 1
    suffix = 0
    while suffix < min(len(text), len(previous)) - start and text[-suffix-1] == previous[-suffix-1]:
        suffix += 1
    audit("transcript（文字流转）", content=True, turn_id=turn_id, stage=stage,
          text=text, previous_text=previous, revision=revision, chars=len(text), changed_start=start,
          previous_changed_end=len(previous)-suffix, changed_end=len(text)-suffix, **fields)

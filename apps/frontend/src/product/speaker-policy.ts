/** 声纹高级设置：默认值与服务端一致，保存时由服务端原子校验。 */
export const SPEAKER_DEFAULTS = {
  "continuity_window_ms": 5000,
  "continuity_max_ms": 10000,
  "non_owner_threshold": 0.35,
  "non_owner_ticks": 3,
  "context_max_ms": 3200,
  "recovery_max_distance": 0.25,
  "pending_ttl_ms": 30000,
  "pending_max_turns": 8,
  "calibration_speech_ms": 3200,
  "calibration_max_ms": 10000,
  "calibration_similarity": 0.65,
  "session_weight": 0.6,
  "enrollment_min_segment_speech_ms": 3000,
  "enrollment_min_total_speech_ms": 15000,
  "enrollment_min_speech_ratio": 0.5,
  "enrollment_self_similarity_min": 0.7,
  "enrollment_max_clipped_ratio": 0.005
}

export type SpeakerTuning = Partial<Record<keyof typeof SPEAKER_DEFAULTS, number>>
export const SPEAKER_FIELDS = [
  { key: "continuity_window_ms", label: "连续主人进入时限（毫秒）", min: 0, max: 60000, step: 1 },
  { key: "continuity_max_ms", label: "连续主人最长时限（毫秒）", min: 0, max: 120000, step: 1 },
  { key: "non_owner_threshold", label: "持续非主人阈值", min: -1, max: 1, step: 0.01 },
  { key: "non_owner_ticks", label: "非主人连续有效心跳次数", min: 1, max: 20, step: 1 },
  { key: "context_max_ms", label: "分析上下文上限（毫秒）", min: 45, max: 16000, step: 1 },
  { key: "recovery_max_distance", label: "跨轮恢复最大余弦距离", min: 0, max: 2, step: 0.01 },
  { key: "pending_ttl_ms", label: "待确认文本保留时间（毫秒）", min: 400, max: 300000, step: 1 },
  { key: "pending_max_turns", label: "待确认轮数上限", min: 1, max: 64, step: 1 },
  { key: "calibration_speech_ms", label: "临时校准有效人声量（毫秒）", min: 45, max: 30000, step: 1 },
  { key: "calibration_max_ms", label: "初始无条件放行最长时间（毫秒）", min: 400, max: 60000, step: 1 },
  { key: "calibration_similarity", label: "临时样本一致性阈值", min: -1, max: 1, step: 0.01 },
  { key: "session_weight", label: "临时会话声纹权重", min: 0, max: 1, step: 0.01 },
  { key: "enrollment_min_segment_speech_ms", label: "永久注册单段最低人声（毫秒）", min: 45, max: 15000, step: 1 },
  { key: "enrollment_min_total_speech_ms", label: "永久注册总人声要求（毫秒）", min: 45, max: 45000, step: 1 },
  { key: "enrollment_min_speech_ratio", label: "永久注册最低人声比例", min: 0.01, max: 1, step: 0.01 },
  { key: "enrollment_self_similarity_min", label: "永久注册声纹一致性阈值", min: -1, max: 1, step: 0.01 },
  { key: "enrollment_max_clipped_ratio", label: "永久注册最大削波比例", min: 0, max: 1, step: 0.001 },
] as const

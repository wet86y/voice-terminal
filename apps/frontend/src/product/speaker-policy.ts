/** 声纹内部参数：保留旧设置，普通页面只提供四项调试控件。 */
export const SPEAKER_DEFAULTS = {
  "intra_turn_relaxation": 0.1,
  "continuity_window_ms": 5000,
  "continuity_max_ms": 10000,
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
/** SPEAKER_CONTROL_DEFAULTS（四项可见参数默认值）：恢复默认只更新这些值。 */
export const SPEAKER_CONTROL_DEFAULTS = {
  confirm_threshold: 0.6,
  intra_turn_relaxation: SPEAKER_DEFAULTS.intra_turn_relaxation,
  continuity_relaxation: 0.1,
  session_weight: SPEAKER_DEFAULTS.session_weight,
}

export const SPEAKER_FIELDS = [
  { key: "confirm_threshold", label: "主人确认阈值", min: 0, max: 1, step: 0.01,
    hint: "正常首段确认与暂缓后的三次恢复使用此基准值。" },
  { key: "intra_turn_relaxation", label: "会话内连续放宽余量", min: 0, max: 0.3, step: 0.01,
    hint: "本轮确认主人后，从基准值减去此余量；已放行文字始终保留。" },
  { key: "continuity_relaxation", label: "会话间连续放宽余量", min: 0, max: 0.2, step: 0.01,
    hint: "下一轮开口时连续资格有效，则首段从基准值减去此余量。" },
  { key: "session_weight", label: "临时声纹权重", min: 0, max: 0.8, step: 0.01,
    hint: "临时声纹建立后参与混分；0仅使用永久模板，0.80表示临时声纹占80%。" },
] as const

type SpeakerControls = Record<keyof typeof SPEAKER_CONTROL_DEFAULTS, number>
/** speakerControlsError（四项草稿校验）：与后端保存规则一致，负阈值不夹紧。 */
export function speakerControlsError(value: SpeakerControls): string {
  for (const field of SPEAKER_FIELDS) {
    const item = value[field.key]
    if (!Number.isFinite(item) || item < field.min || item > field.max)
      return `${field.label}必须在 ${field.min} 至 ${field.max} 之间`
  }
  if (value.intra_turn_relaxation > value.confirm_threshold)
    return '会话内连续放宽余量不能大于主人确认阈值，否则放宽后的声纹阈值会小于0'
  if (value.continuity_relaxation > value.confirm_threshold)
    return '会话间连续放宽余量不能大于主人确认阈值，否则放宽后的声纹阈值会小于0'
  return ''
}

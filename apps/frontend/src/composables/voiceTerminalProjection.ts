import type { AgentTrace, SpeechOutputRecord, TerminalState, TurnDelivery, CurrentTurnSnapshot, LatencyValues, ServiceStatuses, ShadowProductSnapshot, ShadowActivity, ShadowTimelineItem, ServerMessage } from '../types'
import { TERMINAL_STATES } from '../types'
import { isRecord } from '../record'

export const MAX_SHADOW_ACTIVITIES = 96

/** trimShadowActivities（裁剪运行轨迹）：保留待决权限的答复入口。 */
export function trimShadowActivities(items: ShadowActivity[]): ShadowActivity[] {
  const values = [...items]
  while (values.length > MAX_SHADOW_ACTIVITIES) {
    const index = values.findIndex(item => item.stream !== 'approval' || item.phase !== 'started')
    values.splice(index < 0 ? 0 : index, 1)
  }
  return values
}

export function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && Number.isFinite(value) && value >= 0
}

export function isAgentTrace(value: unknown): value is AgentTrace {
  return isRecord(value) && isNonNegativeInteger(value.trace_seq)
    && typeof value.kind === 'string'
    && (value.conversation_id === null || typeof value.conversation_id === 'string')
}

export function isSpeechOutput(value: unknown): value is SpeechOutputRecord {
  return isRecord(value) && typeof value.speech_id === 'string' && value.speech_id.length > 0
    && (value.conversation_id === undefined || value.conversation_id === null || typeof value.conversation_id === 'string')
    && (value.trace_id === undefined || value.trace_id === null || typeof value.trace_id === 'string')
    && (value.output_generation === undefined || isNonNegativeInteger(value.output_generation))
    && (value.status === undefined || typeof value.status === 'string')
}

export function isTerminalState(value: unknown): value is TerminalState {
  return typeof value === 'string' && TERMINAL_STATES.includes(value as TerminalState)
}

export function readString(payload: Record<string, unknown>, key: string): string | null {
  return typeof payload[key] === 'string' ? payload[key] as string : null
}

export function readDelivery(value: unknown): TurnDelivery | undefined {
  return isRecord(value) ? value as TurnDelivery : undefined
}

export function createEmptyCurrentTurn(): CurrentTurnSnapshot {
  return {
    partial_text: '',
    final_text: '',
    assistant_text: '',
    tts_text: '',
    tts_status: '',
    audio_buffer_ms: null,
    tts_returned_ms: 0,
    tts_played_ms: 0,
    tts_output_estimated_ms: 0,
    tts_remaining_ms: 0,
    delivery: {},
    audio_processing_status: 'DISABLED',
    audio_processing_error: '',
    capture_diagnostics: {},
    playback_diagnostics: {},
    tts_transport: 'websocket',
    tts_scheduling: 'auto',
    input_runtime: true, output_generation: 0, committed: false,
    agent_phase: 'IDLE', paraformer_final: '', refined_text: '',
    refinement_available: false, tts_prewarm_started: false, tts_prewarm_status: 'NOT_REQUESTED',
    segments: [], tools: [], audio_cues: [], analysis: {}, speaker_decision: {},
  }
}

export function createEmptyLatency(): LatencyValues {
  return {
    wake_detection_ms: null,
    asr_first_text_ms: null,
    speech_end_confirmation_ms: null,
    llm_first_token_ms: null,
    llm_request_to_first_tts_text_ms: null,
    tts_text_to_first_audio_ms: null,
    first_audio_to_playback_ms: null,
    speech_end_to_playback_ms: null,
    turn_total_ms: null,
    first_segment_ms: null, smart_turn_ms: null,
    sensevoice_refine_ms: null, commit_to_final_llm_ms: null,
    asr_final_to_commit_ms: null, refine_duration_ms: null,
    refine_to_redecision_ms: null, agent_request_to_speak_ms: null,
    tts_prewarm_requested_ms: null, tts_prewarm_ready_ms: null,
    tts_checkout_ms: null, tts_cold_start_ms: null,
    speech_end_to_first_pcm_ms: null, first_pcm_to_player_submit_ms: null,
    replacement_count: null,
  }
}

export function createOfflineStatuses(): ServiceStatuses {
  return {
    audio: 'OFFLINE',
    kws: 'OFFLINE',
    asr: 'OFFLINE',
    speaker: 'OFFLINE',
    llm: 'OFFLINE',
    tts: 'OFFLINE',
    web_search: 'DISABLED',
    analysis: 'DISABLED',
  }
}

export function createEmptyShadowProduct(): ShadowProductSnapshot {
  return {
    available: false,
    active: false,
    phase: 'unavailable',
    user_input_required: false,
    focus: '',
    summary: '',
    timeline: [],
    activities: [],
    context: {},
    provider: {},
  }
}

export function readShadowActivity(value: unknown): ShadowActivity | null {
  if (!isRecord(value)
    || !isNonNegativeInteger(value.run_seq)
    || typeof value.item_id !== 'string'
    || !value.item_id.trim()
    || !['lifecycle', 'thinking', 'tool', 'event', 'output', 'approval'].includes(String(value.stream))
    || (value.phase !== 'started' && value.phase !== 'delta' && value.phase !== 'completed' && value.phase !== 'failed')
    || typeof value.timestamp !== 'string') return null
  const result: ShadowActivity = {
    run_seq: value.run_seq,
    item_id: value.item_id.slice(0, 160),
    stream: value.stream as ShadowActivity['stream'],
    phase: value.phase,
    timestamp: value.timestamp,
  }
  for (const key of ['text', 'delta', 'tool_name', 'tool_status', 'approval_id'] as const) {
    if (typeof value[key] === 'string') result[key] = value[key].slice(0, key === 'delta' ? 2000 : key === 'approval_id' ? 160 : 12000)
  }
  if (value.event_kind === 'progress' || value.event_kind === 'information'
    || value.event_kind === 'input_required' || value.event_kind === 'result'
    || value.event_kind === 'no_action'
    || value.event_kind === 'failed') {
    result.event_kind = value.event_kind
  }
  return result
}

export function readShadowProduct(value: unknown): ShadowProductSnapshot | null {
  if (!isRecord(value)
    || typeof value.available !== 'boolean'
    || typeof value.active !== 'boolean'
    || typeof value.phase !== 'string'
    || typeof value.user_input_required !== 'boolean'
    || typeof value.focus !== 'string'
    || typeof value.summary !== 'string'
    || !Array.isArray(value.timeline)) return null
  const timeline: ShadowTimelineItem[] = []
  for (const item of value.timeline.slice(-5)) {
    if (!isRecord(item)
      || typeof item.state !== 'string'
      || typeof item.label !== 'string'
      || typeof item.text !== 'string'
      || typeof item.timestamp !== 'string') continue
    timeline.push({
      state: item.state,
      label: item.label.slice(0, 120),
      text: item.text.slice(0, 400),
      timestamp: item.timestamp,
    })
  }
  const activities = Array.isArray(value.activities)
    ? trimShadowActivities(value.activities.map(readShadowActivity).filter((item): item is ShadowActivity => item !== null))
    : undefined
  const readTelemetry = (candidate: unknown, keys: string[]) => {
    if (!isRecord(candidate)) return {}
    const result: Record<string, number | null> = {}
    for (const key of keys) {
      const field = candidate[key]
      if (field === null || (typeof field === 'number' && Number.isFinite(field) && field >= 0)) {
        result[key] = field as number | null
      }
    }
    return result
  }
  const result: ShadowProductSnapshot = {
    available: value.available,
    active: value.active,
    phase: value.phase,
    user_input_required: value.user_input_required,
    focus: value.focus.slice(0, 400),
    summary: value.summary.slice(0, 400),
    timeline,
    context: readTelemetry(value.context, ['used_tokens', 'limit_tokens', 'usage_percent']),
    provider: readTelemetry(value.provider, ['input_tokens', 'cached_tokens', 'cache_hit_ratio']),
  }
  if (value.backend === 'native' || value.backend === 'dsh') result.backend = value.backend
  if (value.model_management === 'voice_terminal' || value.model_management === 'dsh_harness') {
    result.model_management = value.model_management
  }
  if (activities !== undefined) result.activities = activities
  if (value.last_activity_at === null || typeof value.last_activity_at === 'string') {
    result.last_activity_at = value.last_activity_at
  }
  return result
}

export function parseServerMessage(raw: unknown): ServerMessage | null {
  if (typeof raw !== 'string') return null

  try {
    const parsed: unknown = JSON.parse(raw)
    if (!isRecord(parsed)) return null
    if (typeof parsed.type !== 'string') return null
    if (typeof parsed.seq !== 'number' || !Number.isInteger(parsed.seq) || !Number.isFinite(parsed.seq)) return null
    if (typeof parsed.timestamp !== 'string') return null
    if (typeof parsed.conversation_id !== 'string' || !parsed.conversation_id) return null
    for (const key of ['turn_id', 'speech_id']) {
      if (parsed[key] !== undefined && parsed[key] !== null && typeof parsed[key] !== 'string') return null
    }
    if (!isRecord(parsed.payload)) return null
    return parsed as unknown as ServerMessage
  } catch {
    return null
  }
}

export function copyLatencyValues(input: unknown): LatencyValues {
  const result = createEmptyLatency()
  if (!isRecord(input)) return result

  for (const key of Object.keys(result) as Array<keyof LatencyValues>) {
    const value = input[key]
    if (value === null || (typeof value === 'number' && Number.isFinite(value))) {
      result[key] = value as number | null
    }
  }
  return result
}

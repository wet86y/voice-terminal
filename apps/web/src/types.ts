export const TERMINAL_STATES = [
  'STARTING',
  'IDLE',
  'LISTENING',
  'FINALIZING',
  'THINKING',
  'SPEAKING',
  'INTERRUPTING',
  'ERROR',
] as const

export type TerminalState = (typeof TERMINAL_STATES)[number]
export type ConnectionState = 'offline' | 'connecting' | 'connected'

export type DeliveryStatus = 'full' | 'partial' | 'none' | 'unknown' | (string & {})
export type TurnTermination = 'completed' | 'interrupted' | 'failed' | 'stopped' | (string & {})

export interface TurnDelivery {
  /** How much of the generated response reached the local output path. */
  status?: DeliveryStatus
  /** Why the turn ended.  This is separate from ``status``. */
  termination?: TurnTermination
  heard_text?: string
  uncertain_text?: string
  unheard_text?: string
  played_ms?: number | null
  callback_played_ms?: number | null
  output_tail_estimated_ms?: number | null
  speech_onset_ms?: number | null
  interrupt_confirmed_ms?: number | null
  speech_onset_to_confirm_ms?: number | null
  confirm_to_stop_ms?: number | null
  playback_stop_ms?: number | null
  cancel_confirmed_ms?: number | null
  precision?: string
  playback_precision?: string
  reason?: string
}

export type ServiceStatus =
  | 'OFFLINE'
  | 'STARTING'
  | 'READY'
  | 'RUNNING'
  | 'STREAMING'
  | 'ERROR'
  | (string & {})

export interface ServiceStatuses {
  analysis?: ServiceStatus
  web_search?: ServiceStatus
  shadow_agent?: ServiceStatus
  audio: ServiceStatus
  kws: ServiceStatus
  asr: ServiceStatus
  speaker?: ServiceStatus
  llm: ServiceStatus
  tts: ServiceStatus
}

export interface CaptureDiagnostics {
  reason?: string
  expected_start_sample?: number | null
  actual_start_sample?: number | null
  dropped_chunks?: number | null
  asr_queue_depth?: number | null
  last_drop_at?: number | null
  last_error?: string | null
}

export interface PlaybackDiagnostics {
  configured_device?: string | number | null
  resolved_device?: string | number | null
  sample_rate?: number
  channels?: number
  active_key?: string | null
  first_rendered_key?: string | null
  first_rendered_at?: number | null
  callback_status?: string | null
  buffered_ms?: number
}

export interface CurrentTurnSnapshot {
  turn_id?: string | null
  input_runtime?: boolean
  output_generation?: number
  committed?: boolean
  agent_phase?: string
  paraformer_final?: string
  refined_text?: string
  refinement_available?: boolean
  tts_prewarm_started?: boolean
  tts_prewarm_status?: string
  segments?: Array<{ text: string }>
  tools?: Array<{ name?: string; status?: string; reused?: boolean; disabled?: boolean; service_status?: string; error?: string; overlap_ms?: number; wait_ms?: number; selected_at_ms?: number; execution_start_ms?: number | null; finished_at_ms?: number; execution_ms?: number }>
  audio_cues?: Array<{ start_ms: number; end_ms: number; emotion: string; language: string; events: string[] }>
  analysis?: Record<string, { status?: string; error?: string; probability?: number; fallback?: string; model?: string; model_loaded?: boolean }>
  partial_text: string
  final_text: string
  assistant_text: string
  tts_text: string
  tts_status: string
  audio_buffer_ms: number | null
  tts_returned_ms: number | null
  tts_played_ms: number | null
  /** Conservative device output estimate; callback consumption stays above. */
  tts_output_estimated_ms: number | null
  tts_remaining_ms: number | null
  delivery?: TurnDelivery
  audio_processing_status?: string
  audio_processing_error?: string
  capture_diagnostics?: CaptureDiagnostics
  playback_diagnostics?: PlaybackDiagnostics
  tts_transport?: string
  tts_scheduling?: string
  speaker_decision?: {
    turn_id?: string
    generation?: number
    user_id?: string | null
    state?: string
    similarity?: number | null
    threshold_used?: number | null
    speech_ms?: number
    observation_count?: number
    continuity_source?: string | null
    route?: string
    reason?: string
    degraded?: boolean
    anti_spoofing?: string
  }
}

export interface LatencyValues {
  first_segment_ms?: number | null
  smart_turn_ms?: number | null
  sensevoice_refine_ms?: number | null
  commit_to_final_llm_ms?: number | null
  asr_final_to_commit_ms?: number | null
  refine_duration_ms?: number | null
  refine_to_redecision_ms?: number | null
  agent_request_to_speak_ms?: number | null
  tts_prewarm_requested_ms?: number | null
  tts_prewarm_ready_ms?: number | null
  tts_checkout_ms?: number | null
  tts_cold_start_ms?: number | null
  speech_end_to_first_pcm_ms?: number | null
  first_pcm_to_player_submit_ms?: number | null
  replacement_count?: number | null
  wake_detection_ms: number | null
  asr_first_text_ms: number | null
  speech_end_confirmation_ms: number | null
  llm_first_token_ms: number | null
  llm_request_to_first_tts_text_ms: number | null
  tts_text_to_first_audio_ms: number | null
  first_audio_to_playback_ms: number | null
  input_end_to_playback_ms?: number | null
  speech_end_to_playback_ms: number | null
  turn_total_ms: number | null
}

export interface TurnLatencyValues extends LatencyValues {
  turn_id?: string | null
  input_id?: string | null
  speech_id?: string | null
  asr_final_ms?: number | null
  finalized_ms?: number | null
  speak_requested_ms?: number | null
  tts_first_audio_ms?: number | null
  player_first_pcm_submitted_ms?: number | null
  refinement_ms?: number | null
  prewarm_status?: string | null
  prewarm_used?: boolean
}

export type ConversationEntryKind = 'user_turn' | 'assistant_speech' | 'system_speech'

/**
 * One line of the server-ordered conversation log.
 *
 * The order is the server's ``seq``: the rail renders this projection directly
 * instead of merging ``recent_inputs``, the episode outputs and the playback
 * queue on the client, so a system-owned line (the KWS wake acknowledgement)
 * keeps the place it really happened in.
 */
export interface ConversationEntry {
  seq: number
  kind: ConversationEntryKind
  conversation_id: string
  turn_id: string
  speech_id: string
  text: string
  status: string
  delivery: TurnDelivery
  termination: string
  at: string
}

export interface ConversationSnapshotPayload {
  entries?: ConversationEntry[]
  latest_seq?: number
}

export interface RuntimeSnapshotPayload {
  agent?: AgentEpisodeSnapshot
  recent_inputs?: InputRecord[]
  conversation?: ConversationSnapshotPayload
  state: TerminalState
  listening: boolean
  can_start: boolean
  wake_always_on?: boolean
  input_owner?: 'none' | 'kws' | 'asr'
  reasoning_mode?: ReasoningMode
  services: ServiceStatuses
  current: CurrentTurnSnapshot
  shadow?: ShadowProductSnapshot
  latency?: {
    complete: boolean
    values: Partial<LatencyValues>
  }
}

export interface SessionStatePayload {
  state: TerminalState
  listening: boolean
  can_start: boolean
}

export interface TranscriptPayload {
  text: string
}

export interface AssistantDeltaPayload {
  text_delta: string
}

export interface LlmContextMessage {
  role: string
  content: string
}

export interface ShadowTimelineItem {
  state: 'active' | 'done' | 'error' | string
  label: string
  text: string
  timestamp: string
}

export type ShadowActivityStream = 'lifecycle' | 'thinking' | 'tool' | 'event'
export type ShadowActivityPhase = 'started' | 'delta' | 'completed' | 'failed'
/** 影子活动事件类型：progress 仅显示界面，information 是可替换 Voice 信息，其余为终态。 */
export type ShadowActivityEventKind = 'progress' | 'information' | 'input_required' | 'result' | 'no_action' | 'failed'

export interface ShadowActivity {
  /** 运行序号：同一影子激活内单调递增。 */
  run_seq: number
  /** 活动条目标识：用于产品界面更新已存在的活动项。 */
  item_id: string
  /** 活动流类型：生命周期、思考、工具或事件。 */
  stream: ShadowActivityStream
  /** 活动阶段：开始、增量、完成或失败。 */
  phase: ShadowActivityPhase
  /** 活动正文：展示给产品界面的文本。 */
  text?: string
  /** 增量正文：活动文本的新增部分。 */
  delta?: string
  /** 工具名称：仅工具活动使用。 */
  tool_name?: string
  /** 工具状态：工具执行阶段说明。 */
  tool_status?: string
  /** 事件类型：Bridge 2.0 判别联合的产品界面投影。 */
  event_kind?: ShadowActivityEventKind
  /** 发生时间：活动事件的 ISO 8601 时间戳。 */
  timestamp: string
}

export interface ShadowProductSnapshot {
  backend?: 'native' | 'dsh'
  model_management?: 'voice_terminal' | 'dsh_harness'
  available: boolean
  active: boolean
  phase: string
  user_input_required: boolean
  focus: string
  summary: string
  timeline: ShadowTimelineItem[]
  activities?: ShadowActivity[]
  last_activity_at?: string | null
  context?: {
    used_tokens?: number | null
    limit_tokens?: number | null
    usage_percent?: number | null
  }
  provider?: {
    input_tokens?: number | null
    cached_tokens?: number | null
    cache_hit_ratio?: number | null
  }
}

export interface LlmContextPayload {
  messages: LlmContextMessage[]
  message_count: number
  context_chars?: number
  context_limit_tokens?: number
  context_token_estimate?: number
  provider_prompt_tokens?: number | null
  provider_input_tokens?: number | null
  provider_cached_tokens?: number | null
  provider_cache_miss_tokens?: number | null
  provider_cache_hit_ratio?: number | null
  context_usage_percent?: number
  conversation_id?: string
  request_id?: string
}

export interface TtsStatusPayload {
  provider: string
  status: string
  waiting_text: string
  audio_buffer_ms: number | null
  audio_returned_ms?: number | null
  audio_played_ms?: number | null
  audio_output_estimated_ms?: number | null
  audio_remaining_ms?: number | null
  pacing_enabled?: boolean
  lead_ms?: number
  trigger?: string | null
}

export interface LatencySnapshotPayload {
  complete: boolean
  values: Partial<LatencyValues>
}

export interface TurnCompletedPayload {
  conversation_id?: string | null
  details?: Record<string, unknown>
  transcript: string
  response: string
  completed_at: string
  latency?: TurnLatencyValues
  status?: TurnTermination
  delivery?: TurnDelivery
}

export interface ServerErrorPayload {
  code: string
  message: string
  recoverable: boolean
}

export type ServerMessageType =
  | 'runtime.snapshot' | 'session.state' | 'metrics.snapshot'
  | 'input.started' | 'input.updated' | 'input.finalized' | 'input.completed'
  | 'agent.state' | 'agent.trace' | 'agent.reasoning' | 'context.snapshot' | 'tool.state' | 'speak.preview'
  | 'speech.queued' | 'speech.started' | 'speech.delta' | 'speech.progress' | 'speech.completed'
  | 'conversation.append' | 'conversation.snapshot'
  | 'shadow.product' | 'shadow.activity' | 'error'

export type ReasoningMode = 'fast' | 'balanced' | 'deep'
export type CommandName = 'subscribe' | 'session.start' | 'session.stop' | 'session.reset' | 'session.reasoning_mode'

export interface MessageEnvelope<TType extends string = string, TPayload = unknown> {
  type: TType
  seq: number
  timestamp: string
  conversation_id: string
  turn_id?: string | null
  speech_id?: string | null
  payload: TPayload
}

export type ServerMessage = MessageEnvelope<string, unknown>

export interface CommandMessage {
  type: 'command'
  request_id: string
  command: CommandName
  payload: Record<string, unknown>
}

export interface InputRecord {
  conversation_id?: string | null
  details?: Record<string, unknown>
  turn_id: string
  transcript: string
  response: string
  completed_at: string
  latency: LatencyValues
  status?: TurnTermination
  delivery?: TurnDelivery
}

export interface AgentTrace {
  trace_seq: number
  kind: string
  conversation_id: string | null
  trace_id?: string | null
  request_id?: string | null
  turn_id?: string | null
  text?: string
  reason?: string
  name?: string
  status?: string
  speech_id?: string
  [key: string]: unknown
}

export interface SpeechOutputRecord {
  speech_id: string
  conversation_id: string
  trace_id?: string
  turn_id: string
  request_id?: string
  output_generation?: number
  text: string
  status: string
  delivery?: TurnDelivery
  latency?: Partial<LatencyValues>
  turn_latency?: Partial<TurnLatencyValues>
  emotion_requested?: string
  emotion_applied?: string
  emotion_transport?: string
  emotion_status?: string
  kind?: 'formal'
}

export type PlaybackQueueState = 'waiting' | 'playing'

export interface PlaybackQueueItem {
  speech_id: string
  conversation_id: string
  request_id?: string
  turn_id: string
  output_generation?: number
  text: string
  state: PlaybackQueueState
  emotion_requested?: string
  emotion_applied?: string
}

export type PlaybackResultStatus = 'finished' | 'failed' | 'interrupted' | 'replaced'

export interface LastPlaybackResult {
  speech_id: string
  conversation_id: string
  turn_id: string
  text: string
  result: PlaybackResultStatus
}

export interface SpeakPreview {
  conversation_id: string
  request_id: string
  turn_id?: string | null
  state?: 'speak'
  text?: string
  text_delta?: string
  committed?: boolean
  status?: 'committed' | 'discarded'
}

export interface AgentEpisodeSnapshot {
  conversation_id: string | null
  trace_id?: string | null
  request_id?: string | null
  context_limited?: boolean
  context_limit_reason?: string | null
  context_chars?: number
  context_limit_tokens?: number
  context_token_estimate?: number
  provider_prompt_tokens?: number | null
  provider_input_tokens?: number | null
  provider_cached_tokens?: number | null
  provider_cache_miss_tokens?: number | null
  provider_cache_hit_ratio?: number | null
  static_context_digest?: string | null
  history_context_digest?: string | null
  schema_digest?: string | null
  context_usage_percent?: number
  agent_busy: boolean
  user_speaking: boolean | null
  current_action?: string | null
  request_count?: number
  max_request_concurrency?: number
  playback_state?: string
  context_compacted?: boolean
  context_compaction?: Record<string, unknown>
  active_speech?: SpeechOutputRecord | null
  recovery_pending?: number
  speech_queue?: SpeechOutputRecord[]
  speak_snapshot?: { queue_status?: 'empty' | 'queued' | 'suspended' | 'speaking' | string; text?: string; text_only_queue?: boolean }
  reasoning?: { text?: string } | null
  trace?: AgentTrace[]
  outputs?: SpeechOutputRecord[]
}

/**
 * Development-only state injection used by the local log replay view.
 * It mirrors the public runtime projection rather than exposing raw prompts
 * or internal provider/tool payloads.
 */
export interface VoiceReplayFixture {
  state: TerminalState
  listening: boolean
  canStart?: boolean
  wakeAlwaysOn?: boolean
  inputOwner?: 'none' | 'kws' | 'asr'
  reasoningMode?: ReasoningMode
  services?: Partial<ServiceStatuses>
  current: Partial<CurrentTurnSnapshot>
  currentLatency?: Partial<LatencyValues>
  currentLatencyComplete?: boolean
  recentInputs: InputRecord[]
  shadow: ShadowProductSnapshot
  agent?: AgentEpisodeSnapshot | null
  agentTrace?: AgentTrace[]
  speechOutputs?: SpeechOutputRecord[]
  playbackQueue?: PlaybackQueueItem[]
  lastPlayback?: LastPlaybackResult | null
  reasoning?: string
}

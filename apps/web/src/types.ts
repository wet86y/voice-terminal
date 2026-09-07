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
  audio: ServiceStatus
  kws: ServiceStatus
  asr: ServiceStatus
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
  epoch?: number
  committed?: boolean
  agent_phase?: string
  paraformer_final?: string
  refined_text?: string
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
}

export interface LatencyValues {
  first_segment_ms?: number | null
  smart_turn_ms?: number | null
  sensevoice_refine_ms?: number | null
  commit_to_final_llm_ms?: number | null
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

export interface RuntimeSnapshotPayload {
  agent?: AgentEpisodeSnapshot
  recent_inputs?: InputRecord[]
  state: TerminalState
  listening: boolean
  can_start: boolean
  services: ServiceStatuses
  current: CurrentTurnSnapshot
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
  tool_calls?: unknown[]
  tool_call_id?: string
}

export interface LlmContextPayload {
  messages: LlmContextMessage[]
  message_count: number
  context_chars?: number
  context_limit_tokens?: number
  context_token_estimate?: number
  provider_prompt_tokens?: number | null
  context_usage_percent?: number
  conversation_id?: string
  epoch?: number
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
  latency?: LatencyValues
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
  | 'agent.state' | 'agent.trace' | 'agent.prefill' | 'agent.reasoning' | 'context.snapshot' | 'tool.state'
  | 'speech.queued' | 'speech.started' | 'speech.delta' | 'speech.progress' | 'speech.completed'
  | 'error'

export type CommandName = 'subscribe' | 'session.start' | 'session.stop' | 'session.reset'

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
  epoch?: number
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
  epoch: number
  text: string
  status: string
  delivery?: TurnDelivery
  latency?: Partial<LatencyValues>
  emotion_requested?: string
  emotion_applied?: string
  emotion_transport?: string
  emotion_status?: string
  kind?: 'prefill' | 'formal'
}

export interface AgentEpisodeSnapshot {
  conversation_id: string | null
  trace_id?: string | null
  epoch: number
  context_limited?: boolean
  context_limit_reason?: string | null
  context_chars?: number
  context_limit_tokens?: number
  context_token_estimate?: number
  provider_prompt_tokens?: number | null
  context_usage_percent?: number
  agent_busy: boolean
  decision_mode: string
  user_speaking: boolean | null
  current_action?: string | null
  request_count?: number
  max_request_concurrency?: number
  playback_state?: string
  context_compacted?: boolean
  context_compaction?: Record<string, unknown>
  active_speech?: SpeechOutputRecord | null
  pending_prefill?: Record<string, unknown> | null
  speech_queue?: SpeechOutputRecord[]
  reasoning?: { text?: string } | null
  trace?: AgentTrace[]
  outputs?: SpeechOutputRecord[]
}

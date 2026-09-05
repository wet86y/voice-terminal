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
  audio: ServiceStatus
  kws: ServiceStatus
  asr: ServiceStatus
  llm: ServiceStatus
  tts: ServiceStatus
}

export interface CurrentTurnSnapshot {
  turn_id?: string | null
  v02?: boolean
  input_revision?: number
  generation_id?: number
  prediction_revision?: number | null
  committed?: boolean
  agent_phase?: string
  paraformer_final?: string
  refined_text?: string
  segments?: Array<{ text: string }>
  prediction?: { decision?: string; intent?: string; status?: string; error?: string }
  tools?: Array<{ name?: string; status?: string; reused?: boolean; error?: string; overlap_ms?: number; wait_ms?: number; selected_at_ms?: number; execution_start_ms?: number | null; finished_at_ms?: number; execution_ms?: number }>
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
  tts_remaining_ms: number | null
}

export interface LatencyValues {
  first_segment_ms?: number | null
  first_prediction_request_ms?: number | null
  first_prediction_ms?: number | null
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
  speech_end_to_playback_ms: number | null
  turn_total_ms: number | null
}

export interface RuntimeSnapshotPayload {
  recent_turns?: TurnRecord[]
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
}

export interface LlmContextPayload {
  messages: LlmContextMessage[]
  message_count: number
}

export interface TtsStatusPayload {
  provider: string
  status: string
  waiting_text: string
  audio_buffer_ms: number | null
  audio_returned_ms?: number | null
  audio_played_ms?: number | null
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
  transcript: string
  response: string
  completed_at: string
  latency: LatencyValues
}

export interface ServerErrorPayload {
  code: string
  message: string
  recoverable: boolean
}

export type ServerMessageType =
  | 'turn.started' | 'turn.resumed' | 'turn.finalizing' | 'turn.committed'
  | 'turn.endpoint_candidate' | 'turn.semantic_state' | 'transcript.segment'
  | 'transcript.refined' | 'speech.emotion' | 'agent.prediction' | 'agent.tool'
  | 'agent.prediction.requested' | 'agent.prediction.discarded'
  | 'runtime.snapshot'
  | 'session.state'
  | 'transcript.partial'
  | 'transcript.final'
  | 'assistant.delta'
  | 'llm.context'
  | 'tts.status' | 'tts.progress'
  | 'latency.snapshot'
  | 'turn.completed'
  | 'error'

export type CommandName = 'subscribe' | 'session.start' | 'session.stop'

export interface MessageEnvelope<TType extends string = string, TPayload = unknown> {
  type: TType
  seq: number
  timestamp: string
  turn_id: string | null
  payload: TPayload
}

export type ServerMessage = MessageEnvelope<ServerMessageType, unknown>

export interface CommandMessage {
  type: 'command'
  request_id: string
  command: CommandName
  payload: Record<string, unknown>
}

export interface TurnRecord {
  turn_id: string
  transcript: string
  response: string
  completed_at: string
  latency: LatencyValues
}

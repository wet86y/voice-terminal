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
  audio: ServiceStatus
  kws: ServiceStatus
  asr: ServiceStatus
  llm: ServiceStatus
  tts: ServiceStatus
}

export interface CurrentTurnSnapshot {
  partial_text: string
  final_text: string
  assistant_text: string
  tts_text: string
  tts_status: string
  audio_buffer_ms: number | null
}

export interface LatencyValues {
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

export interface TtsStatusPayload {
  provider: string
  status: string
  waiting_text: string
  audio_buffer_ms: number | null
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
  | 'runtime.snapshot'
  | 'session.state'
  | 'transcript.partial'
  | 'transcript.final'
  | 'assistant.delta'
  | 'tts.status'
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

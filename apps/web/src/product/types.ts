import type {
  ConversationEntry,
  CurrentTurnSnapshot,
  InputRecord,
  LatencyValues,
  ReasoningMode,
  ServiceStatuses,
  ShadowProductSnapshot,
  TerminalState,
} from '../types'

export type ProductRuntimeState = 'offline' | 'standby' | 'listening' | 'thinking' | 'speaking' | 'error'
export type ProductCapability = 'llm' | 'tts' | 'local_kws' | 'local_asr' | 'local_vad' | 'local_analysis_endpoint' | 'local_analysis_refine' | string
export type ProductModelOrigin = 'runtime_config' | 'local_scan' | 'product_managed' | string

export interface ProductRuntimeSnapshot {
  state: TerminalState
  listening: boolean
  can_start: boolean
  wake_always_on: boolean
  input_owner: string
  reasoning_mode: ReasoningMode
  services: ServiceStatuses
  latency: {
    complete?: boolean
    values?: Partial<LatencyValues>
  }
}

export interface ProductSettingsSnapshot {
  schema_version: number
  wake: {
    enabled: boolean
    /** Primary phrase; mirrors `keywords[0]` for older consumers. */
    keyword: string
    keyword_label?: string
    /** Every accepted spelling of the wake word (1–5 entries). */
    keywords: string[]
    sensitivity: number
  }
  silence: {
    seconds: number
    smart_turn_hard_silence_ms: number
  }
  shadow: {
    experimental_context_gate_enabled: boolean
  }
  speaker: {
    mode: 'off' | 'observe' | 'filter' | string
    confirm_threshold: number
    continuity_threshold: number
  }
  persona: Record<'instructions' | 'personality' | 'user' | 'shadow', string>
  warnings: string[]
}

export interface ProductProvider {
  id: string
  name: string
  origin: ProductModelOrigin
  protocol: string
  base_url?: string | null
  capabilities?: string[]
  credential_ref?: string | null
  credential_state?: string
  last_validation?: string | null
  last_validated_at?: string | null
  [key: string]: unknown
}

export interface ProductModel {
  id: string
  name: string
  model_id: string
  provider_id: string
  capability: ProductCapability
  origin: ProductModelOrigin
  verification: string
  health: string
  local_path?: string | null
  fingerprint?: string | null
  hidden?: boolean
  max_context_tokens?: number
  allowed_reasoning?: string[]
  [key: string]: unknown
}

export interface ProductTtsProfile {
  id: string
  name: string
  model_id: string
  origin: ProductModelOrigin
  verification: string
  voice_id?: string
  emotion_mode?: string
  default_emotion?: string
  output_format?: string
  sample_rate?: number
  transport?: string
  scheduling?: string
  subtitle_enable?: boolean
  subtitle_type?: string
  [key: string]: unknown
}

export interface ProductAssignments {
  voice_llm_model_id?: string | null
  shadow_llm_model_id?: string | null
  status_llm_model_id?: string | null
  tts_profile_id?: string | null
  /** Runtime preference within the selected Shadow model's shared policy. */
  shadow_reasoning_effort?: string | null
  policy_mode?: 'model' | 'legacy' | string
  role_settings?: Record<string, Record<string, unknown>>
}

export interface ProductModelRegistry {
  credential_vault?: { available?: boolean; diagnostic?: string | null }
  providers: ProductProvider[]
  models: ProductModel[]
  tts_profiles: ProductTtsProfile[]
  assignments: ProductAssignments
}

export interface ProductConversationSnapshot {
  recent: InputRecord[]
  /** Server-ordered conversation log; absent on older backends. */
  entries?: ConversationEntry[]
  latest_seq?: number
  current: Partial<CurrentTurnSnapshot>
}

export interface DshRuntimeStatus {
  enabled: boolean
  state: 'disabled' | 'starting' | 'ready' | 'unavailable' | 'exited'
  version: string | null
  profile: 'web'
  ui_origin: string | null
  message: string
}

export interface ProductSnapshot {
  schema_version: number
  runtime: ProductRuntimeSnapshot
  conversation: ProductConversationSnapshot
  shadow: ShadowProductSnapshot
  dsh: DshRuntimeStatus
  settings: ProductSettingsSnapshot
  models: ProductModelRegistry
  warnings?: string[]
}

export interface ProductMutationResult {
  settings?: ProductSettingsSnapshot | Record<string, unknown>
  effective_from?: 'next_session' | 'next_episode' | string
  pending?: boolean
  message?: string
  [key: string]: unknown
}

export interface ProductApiErrorShape {
  status: number
  message: string
}

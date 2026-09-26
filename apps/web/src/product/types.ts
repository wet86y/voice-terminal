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
/** 本地模型 device mode（设备模式）：自动、仅 CPU（中央处理器）或 CUDA（NVIDIA GPU）。 */
export type ComputeDeviceMode = 'auto' | 'cpu' | 'cuda'
/** 可调度的非 TTS 本地模型标识。 */
export type ProductLocalModelId = 'paraformer' | 'sensevoice' | 'voice_vad' | 'kws' | 'smart_turn' | 'wespeaker'

export interface ProductComputeSettings {
  /** devices（设备选项）：每个本地模型选择自动、仅 CPU 或 GPU。 */
  devices: Record<ProductLocalModelId, ComputeDeviceMode>
  /** fallback_devices（回退设备）：保存时记录的上次成功设备，供启动失败时回退。 */
  fallback_devices?: Record<ProductLocalModelId, 'cpu' | 'cuda'>
}

export interface ProductComputeModelStatus {
  /** requested_device（请求设备）：设置中保存的设备偏好。 */
  requested_device: ComputeDeviceMode
  /** device（实际设备）：当前模型报告的运行设备。 */
  device: string | null
  /** service（服务状态）：模型当前的加载与服务状态。 */
  service: string
  /** load_error（加载诊断）：启动回退原因或当前加载错误。 */
  load_error?: string | null
  /** process_memory_rss_bytes（进程常驻内存）：所在工作进程当前占用的 RAM。 */
  process_memory_rss_bytes?: number | null
  /** gpu_available（GPU 可用性）：当前模型的 GPU 推理后端是否已安装并可用。 */
  gpu_available: boolean
  /** gpu_unavailable_reason（GPU 不可用原因）：后端未安装或设备不兼容的解释。 */
  gpu_unavailable_reason?: string | null
  /** internal_vad_device（内部 VAD 设备）：SenseVoiceSmall 内部 FSMN-VAD 的实际设备。 */
  internal_vad_device?: string | null
}

export interface ProductComputeStatus {
  hardware: {
    system_memory: { available_bytes: number | null; total_bytes: number | null }
    gpu: { available: boolean; name: string | null; free_bytes: number | null; total_bytes: number | null }
    backends: {
      pytorch_cuda: boolean
      onnxruntime_providers: string[]
      sherpa_onnx_version: string | null
      sherpa_onnx_cuda: boolean
    }
    models: Record<ProductLocalModelId, { gpu_available: boolean; reason: string | null }>
  }
  models: Record<ProductLocalModelId, ProductComputeModelStatus>
}

export interface ProductRuntimeSnapshot {
  state: TerminalState
  listening: boolean
  can_start: boolean
  wake_always_on: boolean
  input_owner: string
  reasoning_mode: ReasoningMode
  services: ServiceStatuses
  /** CosyVoice Worker（本地语音工作进程）的非敏感运行状态。 */
  tts_worker?: { state: 'stopped' | 'starting' | 'ready' | 'draining' | 'error'; message?: string | null; model_present?: boolean }
  latency: {
    complete?: boolean
    values?: Partial<LatencyValues>
  }
}

export interface ProductSettingsSnapshot {
  schema_version: number
  /** TTS backend（语音后端）固定为 MiniMax API（在线语音接口）。 */
  tts: { backend: 'minimax' }
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
  /** compute（本地模型设备偏好）：不包含 TTS（语音合成）。 */
  compute: ProductComputeSettings
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

/** One Voice Terminal project wake task（一个语音项目定时唤醒任务）. */
export interface WakeTask {
  /** task_id（任务标识）. */
  task_id: string
  /** target（唤醒目标）：voice_llm、shadow_agent 或 both. */
  target: 'voice_llm' | 'shadow_agent' | 'both'
  /** when（触发时间）：按项目本地时区解释. */
  when: string | null
  /** repeat（固定重复间隔）；null 表示一次性任务. */
  repeat: string | null
  /** text（触发时传递的任务说明）. */
  text: string
  /** status（任务状态）. */
  status: 'pending' | 'archived' | 'cancelled' | string
  /** created_at（创建时间）. */
  created_at: string | null
  /** updated_at（更新时间）. */
  updated_at: string | null
  /** last_triggered_at（最近触发时间）. */
  last_triggered_at: string | null
  /** occurrence（当前循环序号）. */
  occurrence: number
  /** has_reference（是否保存了冻结参考）. */
  has_reference: boolean
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
  /** restart_required（需要重启）：修改计算设备后必须重启整个应用。 */
  restart_required?: boolean
  message?: string
  [key: string]: unknown
}

export interface ProductApiErrorShape {
  status: number
  message: string
}

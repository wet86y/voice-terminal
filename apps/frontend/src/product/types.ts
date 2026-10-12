import type { SpeakerTuning } from "./speaker-policy"
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
export type ProductLocalModelId = 'paraformer' | 'sensevoice' | 'voice_vad' | 'kws' | 'smart_turn' | 'campplus'

export interface ProductComputeSettings {
  /** devices（设备选项）：每个本地模型选择自动、仅 CPU 或 GPU。 */
  devices: Record<ProductLocalModelId, ComputeDeviceMode>
  /** fallback_devices（回退设备）：保存时记录的上次成功设备，供启动失败时回退。 */
  fallback_devices?: Record<ProductLocalModelId, 'cpu' | 'cuda'>
}

export interface ProductComputeModelStatus {
  /** requested_device（本次加载请求设备）：与保存但待重启的偏好分别呈现。 */
  requested_device: ComputeDeviceMode
  /** device（实际设备）：当前模型报告的运行设备。 */
  device: string | null
  /** service（服务状态）：模型当前的加载与服务状态。 */
  service: string
  /** load_error（加载诊断）：启动回退原因或当前加载错误。 */
  load_error?: string | null
  /** runtime_error（运行故障）：通信或推理失败的原因，与设备加载回退诊断分开。 */
  runtime_error?: string | null
  /** process_memory_rss_bytes（进程常驻内存）：所在工作进程当前占用的 RAM。 */
  process_memory_rss_bytes?: number | null
  /** resource_status（实际进程快照）：两种内存和进程标识来自同一次采样。 */
  resource_status?: ResourceProcessStatus | null
  /** runtime（实际加载引擎）：加载失败或旧工作进程不提供时为空。 */
  runtime?: string | null
  /** gpu_available（GPU 可用性）：当前模型的 GPU 推理后端是否已安装并可用。 */
  gpu_available: boolean
  /** gpu_unavailable_reason（GPU 不可用原因）：后端未安装或设备不兼容的解释。 */
  gpu_unavailable_reason?: string | null
  /** internal_vad_device（内部 VAD 设备）：SenseVoiceSmall 内部 FSMN-VAD 的实际设备。 */
  internal_vad_device?: string | null
}

export interface ResourceProcessStatus {
  /** gpu_dedicated_bytes（所在进程专用显存字节）：未知时为空。 */
  gpu_dedicated_bytes?: number | null
  /** pid（进程标识）与 created_at（创建时间）共同确认身份。 */
  pid: number
  created_at: number
  /** private_commit_bytes（私有提交字节）：未知时为空，不等于物理内存。 */
  private_commit_bytes: number | null
  /** rss_bytes（物理常驻字节）及 thread_count（线程数量）。 */
  rss_bytes: number
  thread_count: number
  /** managed_cache_bytes（已登记缓存字节）与 active_leases（活动租约数）。 */
  managed_cache_bytes: number
  active_leases: number
}

export interface ResourceStatus {
  /** gpu_dedicated_bytes（按进程去重的专用显存总量）。 */
  gpu_dedicated_bytes?: number | null
  /** complete（采样覆盖完整）及 missing_roles（缺失的活动角色）；旧工作进程可能不提供资源指标。 */
  complete: boolean
  missing_roles: string[]
  /** processes（按进程身份去重后的采样清单）。 */
  processes: ResourceProcessStatus[]
  /** private_commit_bytes（合计私有提交字节）：任一进程未知则为空。 */
  private_commit_bytes: number | null
  /** rss_bytes（合计常驻字节），共享页面可能被多个进程计入。 */
  rss_bytes: number
  /** thread_count（线程总数）、managed_cache_bytes（登记缓存总字节）、active_leases（活动租约总数）。 */
  thread_count: number
  managed_cache_bytes: number
  active_leases: number
}

export interface ProductComputeStatus {
  /** resource_status（资源状态）：旧服务可以省略此字段。 */
  resource_status?: ResourceStatus
  hardware: {
    system_memory: {
      available_bytes: number | null
      total_bytes: number | null
      /** commit_limit_bytes（系统提交内存上限，字节）：未提供时保留未知，不用物理内存代替。 */
      commit_limit_bytes?: number | null
    }
    gpu: { available: boolean; name: string | null; free_bytes: number | null; total_bytes: number | null }
    backends: {
      pytorch_cuda: boolean
      onnxruntime_providers: string[]
      /** onnxruntime_cuda_verified（模型推理运行时的实际显卡计算检查是否通过）。 */
      onnxruntime_cuda_verified?: boolean
      sherpa_onnx_version: string | null
      sherpa_onnx_cuda: boolean
    }
    models: Record<ProductLocalModelId, { gpu_available: boolean; reason: string | null }>
  }
  /** models（模型采样）：所属进程切换状态时，暂时移除旧采样，等待重新读取。 */
  models: Partial<Record<ProductLocalModelId, ProductComputeModelStatus>>
}

export interface ProductRuntimeSnapshot {
  /** quiet_hours（被动静默状态）：passive_quiet（抑制主动语音）、user_interaction（用户交互窗口）、within_quiet_hours（处于配置时段），不包含待传达正文。 */
  quiet_hours?: { passive_quiet: boolean; user_interaction: boolean; within_quiet_hours: boolean }
  state: TerminalState
  listening: boolean
  can_start: boolean
  wake_always_on: boolean
  input_owner: string
  reasoning_mode: ReasoningMode
  services: ServiceStatuses
  /** 兼容旧字段：归档后的本地 TTS（语音合成）工作进程始终为停止状态。 */
  tts_worker?: { state: 'stopped' }
  latency: {
    complete?: boolean
    values?: Partial<LatencyValues>
  }
}

export interface CustomInstruction {
  /** id（稳定标识）。 */
  id: string
  /** trigger（自然语言触发语义）。 */
  trigger: string
  /** target（执行位置）。 */
  target: 'voice' | 'shadow'
  /** instruction（执行目标）。 */
  instruction: string
  /** enabled（启用状态）。 */
  enabled: boolean
}

export interface ProductSettingsSnapshot {
  /** quiet_hours（每日被动静默）：enabled（启用）、start（开始时间）、end（结束时间）。 */
  quiet_hours?: { enabled: boolean; start: string; end: string }
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
  speaker: SpeakerTuning & {
    mode: 'off' | 'observe' | 'filter' | string
    confirm_threshold: number // confirm_threshold（主人判断阈值）。
    continuity_threshold: number // continuity_threshold（兼容旧配置的连续绝对阈值）。
    continuity_relaxation?: number // continuity_relaxation（连续主人放宽量）：0–0.20。
      /** owner_only_barge_in（主人打断开关）：仅主人确认后允许停止播放。 */
      owner_only_barge_in: boolean
  }
  /** compute（本地模型设备偏好）：不包含 TTS（语音合成）。 */
  compute: ProductComputeSettings
  /** custom_instructions（用户自定义指令条目）。 */
  custom_instructions: CustomInstruction[]
  persona: Record<'personality' | 'user', string>
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

/** FixedModelCredentials（固定供应商密钥状态）：前端只接收是否已配置。 */
export interface FixedModelCredentials {
  providers: { deepseek: { configured: boolean }; minimax: { configured: boolean } }; // providers（两家固定服务的配置状态）。
  vault_available: boolean; // vault_available（本机凭据存储是否可用）。
  message?: string; // message（保存结果说明）。
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
  /** creation_source（创建来源）：手动、代理或来源未知的历史任务。 */
  creation_source?: 'manual' | 'agent' | 'unknown'
  /** task_id（任务标识）. */
  task_id: string
  /** target（唤醒目标）：voice_llm、shadow_agent 或 both. */
  target: 'voice_llm' | 'shadow_agent' | 'both'
  /** when（触发时间）：按项目本地时区解释. */
  when: string | null
  /** repeat（固定重复间隔）；null 表示一次性任务. */
  repeat: string | null
  /** rule_kind（规则类型）与 rule（原生调度规则）。 */
  rule_kind?: 'after' | 'at' | 'every' | 'daily' | 'weekly' | 'cron'
  rule?: Record<string, unknown>
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
  effective_from?: 'immediate' | 'components_ready' | 'next_request' | 'retry_required' | 'application_restart' | string // 生效时机：即时、加载结束、后续请求、重试或重启。
  pending?: boolean
  /** restart_required（需要重启）：修改计算设备后必须重启整个应用。 */
  restart_required?: boolean
  message?: string
  [key: string]: unknown
}

export interface ProductApiErrorShape {
  status: number
  message: string
  code?: string // code（业务错误码）。
  detail?: Record<string, unknown> | string // detail（服务端错误详情）。
}

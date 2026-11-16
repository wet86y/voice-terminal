import type {
  DshRuntimeStatus,
  ProductApiErrorShape,
  ProductComputeStatus,
  ProductMutationResult,
  ProductComputeDeviceSwitch,
  ProductSnapshot,
  WakeTask, ProductTtsSettings, ProductLocalTtsModelId, ProductOnlineTtsModelId, ProductLocalTtsStatus, ProductMinimaxVoiceName, ProductTtsVoice, ProductTtsAsset, ProductTtsOperation,
} from './types'

/** AnifLiveImportFile（模型导入文件）：扫描结果中的单个文件及仓库相对路径。 */
export interface AnifLiveImportFile {
  file_id: string // file_id（文件候选标识）：绑定一次扫描清单中的候选文件。
  path: string // path（来源相对路径）：用于选择、展示和转换前读取。
  size?: number | null // size（文件字节数）：未知时为空。
  role?: 'gpt' | 'sovits' | 'unknown' // role（推测权重角色）：只帮助排序，不决定最终兼容性。
  revision?: string | null // revision（候选文件修订）：公开仓库扫描时保留不可变修订。
}

/** AnifLiveImportScan（模型来源扫描）：网页仓库版本在扫描时固定。 */
export interface AnifLiveImportScan {
  scan_id: string // scan_id（扫描编号）：后续提交只使用本次清单。
  source_type: 'local' | 'huggingface' | 'modelscope' // source_type（来源类型）：本地文件夹或公开模型仓库。
  source: string // source（本地目录或仓库编号）。
  revision?: string | null // revision（固定仓库修订）：远程仓库的版本线索。
  files: AnifLiveImportFile[] // files（发现的全部文件清单）。
  candidates: { weights: AnifLiveImportFile[]; audio: AnifLiveImportFile[]; transcripts: AnifLiveImportFile[]; configs?: AnifLiveImportFile[] } // candidates（按常见扩展名整理的候选）。
}

/** AnifLiveImportRequest（模型导入请求）：只提交用户在扫描预览中选择的候选。 */
export interface AnifLiveImportRequest {
  scan_id: string // scan_id（扫描编号）：引用已展示的模型文件清单。
  selected_gpt: string // selected_gpt（选定 GPT 权重候选）。
  selected_sovits: string // selected_sovits（选定 SoVITS 权重候选）。
  model_id: string // model_id（新模型英文编号）：要求当前未登记且不覆盖已有模型。
  name: string // name（新模型界面名称）。
  reference_audio?: string // reference_audio（可选参考音频候选）。
  selected_reference_files?: string[] // selected_reference_files（同时登记的参考录音候选）。
  reference_text_file?: string // reference_text_file（可选转写旁注候选）。
  reference_text?: string // reference_text（人工填写的准确转写）。
  reference_language: 'zh' | 'yue' | 'en' | 'ja' | 'ko' // reference_language（参考语言）。
}

/** AnifLiveImportJob（模型导入任务）：转换进度由桌面受管任务补充。 */
export interface AnifLiveImportJob {
  task_id: string // task_id（导入任务编号）。
  scan_id?: string // scan_id（来源扫描清单编号）：用于追溯未选中的文件候选。
  model_id?: string // model_id（新模型编号）。
  revision?: string | null // revision（来源仓库修订）：导入完成后仍用于追溯来源。
  name?: string // name（界面模型名称）。
  status: string // status（暂存、待补参考、待转换、转换中或终态）。
  phase: string // phase（当前阶段说明）。
  progress?: number // progress（扫描、下载和暂存的百分比）。
  error?: string | null // error（用户可见失败原因）。
  reference_text_draft?: string | null // reference_text_draft（待校对转写草稿）。
  reference_audio_staged?: boolean // reference_audio_staged（暂存中是否已有参考录音）。
  stage_ready?: boolean // stage_ready（完整转换输入已暂存）：仅此状态允许重试正式转换。
  reference_candidates?: { audio: AnifLiveImportFile[]; transcripts: AnifLiveImportFile[] } // reference_candidates（全部音频及旁注候选）：保留多条素材的来源路径和修订元数据。
  reference_registration?: { ready: number; pending: number; voice_id: string; revision: number; default_registered?: boolean; warning?: string } // reference_registration（参考登记统计、默认状态及提示）。
  job_kind?: 'reference_import' // job_kind（已有模型参考资料导入任务）。
}

export class ProductApiError extends Error implements ProductApiErrorShape {
  readonly status: number
  readonly code?: string // code（业务错误码）：声纹采集回退等业务判断使用。
  readonly detail?: Record<string, unknown> | string // detail（服务端错误详情）。

  constructor(message: string, status = 0, detail?: Record<string, unknown> | string, code?: string) {
    super(message)
    this.name = 'ProductApiError'
    this.status = status
    this.detail = detail
    this.code = code || (typeof detail === 'object' && typeof detail.code === 'string' ? detail.code : undefined)
  }
}

/** DevicePairingRequest（待批准配对请求）：不向页面暴露公钥、随机数或配对码。 */
export interface DevicePairingRequest {
  request_id: string // request_id（待批准请求编号）。
  device_id: string // device_id（申请设备编号）。
  fingerprint: string // fingerprint（申请设备公钥 SHA-256 指纹）。
  created_at_ms: number // created_at_ms（请求建立时间，Unix 毫秒）。
  expires_at_ms: number // expires_at_ms（请求到期时间，Unix 毫秒）。
}

/** DeviceTrustSnapshot（设备信任快照）：不含私钥或完整公钥。 */
export interface DeviceTrustSnapshot {
  schema_version: number // schema_version（信任登记格式版本）。
  state: 'missing' | 'valid' | 'invalid' // state（登记文件状态）。
  devices: Array<{
    device_id: string // device_id（稳定设备编号）。
    fingerprint: string // fingerprint（设备公钥 SHA-256 指纹）。
    state: 'trusted' | 'revoked' // state（可信或已撤销）。
    trusted_at_ms: number // trusted_at_ms（主机批准时间，Unix 毫秒）。
    revoked_at_ms: number | null // revoked_at_ms（主机撤销时间，Unix 毫秒）。
  }> // devices（公开设备登记列表）。
  error?: string // error（无法安全读取登记时的原因）。
}

/** DevicePairingStatus（主机配对状态）：配对码只在开启操作的单次回执出现。 */
export interface DevicePairingStatus {
  window_open: boolean // window_open（主机配对窗口是否开放）。
  window_expires_at_ms: number | null // window_expires_at_ms（配对窗口到期时间，Unix 毫秒）。
  failed_attempts_remaining: number // failed_attempts_remaining（配对窗口自动关闭前剩余错误码尝试次数）。
  pending_requests: DevicePairingRequest[] // pending_requests（已验签但尚未批准的设备）。
  trust: DeviceTrustSnapshot // trust（设备信任登记状态）。
  fields: Record<string, string> // fields（字段中文说明）。
}

export interface DevicePairingOpenResult extends DevicePairingStatus {
  pairing_code: string // pairing_code（仅此次开启配对窗口回传的一次性配对码）。
}

/** ProductDeviceSnapshot（核心设备快照）：本机管理页用于明确选择语音交互设备。 */
export interface ProductDeviceSnapshot {
  devices: Array<{
    device_id: string // device_id（稳定设备编号；local 表示核心主机）。
    name: string // name（设备显示名称）。
    connected: boolean // connected（设备是否在线）。
    connection_generation: number // connection_generation（设备连接代次）。
    executor_generation: number // executor_generation（设备执行器代次）。
    capabilities: string[] // capabilities（当前已登记的设备能力）。
    error: string | null // error（设备错误摘要）。
    interaction_active: boolean // interaction_active（是否持有语音交互租约）。
  }> // devices（当前核心登记的设备列表）。
  active_device_id: string | null // active_device_id（当前获准语音交互的设备编号）。
  interaction_generation: number // interaction_generation（语音设备切换代次）。
  capability_labels: Record<string, string> // capability_labels（能力编号的中文说明）。
  fields: Record<string, string> // fields（字段中文说明）。
}

function apiBaseUrl() {
  const configuration = typeof window === 'undefined' ? undefined : window.__VOICE_APPLICATION__
  if (configuration?.business_ready === false) throw new Error('应用宿主尚未允许业务请求。')
  const configuredUrl = configuration?.voice_api_url
  if (!configuredUrl) throw new Error('应用宿主尚未提供语音服务接口地址。')
  return configuredUrl.replace(/\/$/, '')
}

async function readError(response: Response): Promise<ProductApiError> {
  try {
    const payload: unknown = await response.json()
    if (typeof payload === 'object' && payload !== null) {
      const record = payload as Record<string, unknown>
      const code = typeof record.code === 'string' ? record.code : undefined
      if (typeof record.detail === 'string') return new ProductApiError(record.detail, response.status, record.detail, code)
      if (typeof record.detail === 'object' && record.detail !== null) {
        const detail = record.detail as Record<string, unknown>
        if (typeof detail.message === 'string') return new ProductApiError(detail.message, response.status, detail, code)
        return new ProductApiError(`请求失败（HTTP ${response.status}）。`, response.status, detail, code)
      }
      if (typeof record.message === 'string') return new ProductApiError(record.message, response.status, undefined, code)
    }
  } catch {
    // Fall through to the stable HTTP status below.
  }
  return new ProductApiError(`请求失败（HTTP ${response.status}）。`, response.status)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...(init.headers || {}),
      },
    })
  } catch (error) {
    throw new ProductApiError(error instanceof Error ? error.message : '无法连接 Voice Server。')
  }
  if (!response.ok) throw await readError(response)
  return await response.json() as T
}

function jsonBody(value: unknown): RequestInit {
  return { method: 'PUT', body: JSON.stringify(value) }
}

/** readPairingResponse（配对回执校验）：静态管理页返回 HTML 时报告宿主未就绪，不回显页面正文或秘密。 */
async function readPairingResponse<T extends DevicePairingStatus>(response: Response): Promise<T> {
  if (!response.ok) throw await readError(response)
  if (!response.headers.get('content-type')?.toLowerCase().includes('application/json')) {
    throw new ProductApiError('主机配对接口尚未就绪。请在完整工作区启动共享主机核心后重试。', response.status)
  }
  try {
    return await response.json() as T
  } catch {
    throw new ProductApiError('主机配对接口返回了无效数据，请刷新主机状态后重试。', response.status)
  }
}

export const productApi = {
  hotwords: () => request<{ settings: import('./hotwords').HotwordSettings; status: string }>('/api/v1/product/settings/hotwords'), // hotwords（读取热词）。
  saveHotwords: (value: import('./hotwords').HotwordSettings) => request<{ settings: import('./hotwords').HotwordSettings; message: string }>('/api/v1/product/settings/hotwords', jsonBody(value)), // saveHotwords（保存词表并预建索引）。
  importHotwords: (text: string) => request<{ entries: import('./hotwords').HotwordEntry[] }>('/api/v1/product/hotwords/import', { method: 'POST', body: JSON.stringify({ text }) }), // importHotwords（解析导入，不自动保存）。
  previewHotwords: (settings: import('./hotwords').HotwordSettings, text: string) => request<import('./hotwords').HotwordPreview>('/api/v1/product/hotwords/preview', { method: 'POST', body: JSON.stringify({ settings, text }) }), // previewHotwords（试测当前草稿）。
  /** decideDshApproval（权限答复）：经桌面同源宿主转发，连续授权只限当前语音窗口。 */
  decideDshApproval: async (value: { approval_id: string; decision: 'allowed-once' | 'allowed-until-silence' | 'rejected'; run_seq: number }) => {
    const response = await fetch('/api/v1/application/dsh-approval', { method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(value) })
    if (!response.ok) throw await readError(response)
    return await response.json() as { result: string }
  },
  /** devicePairingStatus（读取本地主机配对状态）：通过已验证桌面会话转发，不缓存秘密。 */
  devicePairingStatus: async () => {
    const response = await fetch('/api/v1/application/device-pairing', {
      method: 'GET', credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
    })
    return await readPairingResponse<DevicePairingStatus>(response)
  },
  /** openDevicePairing（开启主机配对窗口）：配对码仅由本次回执返回。 */
  openDevicePairing: async (ttl_seconds: number) => {
    const response = await fetch('/api/v1/application/device-pairing', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation: 'open', ttl_seconds }),
    })
    return await readPairingResponse<DevicePairingOpenResult>(response)
  },
  /** closeDevicePairing（关闭主机配对窗口）：清除配对码和待批准请求。 */
  closeDevicePairing: async () => devicePairingMutation({ operation: 'close' }),
  /** approveDevicePairing（批准设备配对）：只有主机用户点击批准才会登记公钥。 */
  approveDevicePairing: async (request_id: string) => devicePairingMutation({ operation: 'approve', request_id }),
  /** rejectDevicePairing（拒绝设备配对）：删除待批准请求但不改动可信登记。 */
  rejectDevicePairing: async (request_id: string) => devicePairingMutation({ operation: 'reject', request_id }),
  /** revokeDeviceTrust（撤销设备信任）：仅撤销对应稳定设备编号。 */
  revokeDeviceTrust: async (device_id: string) => devicePairingMutation({ operation: 'revoke', device_id }),
  /** deviceSnapshot（读取核心设备）：只读状态，不暴露信任密钥或任务内容。 */
  deviceSnapshot: () => request<ProductDeviceSnapshot>('/api/v1/devices'),
  /** selectInteractionDevice（明确切换语音设备）：使用已显示的代次防止覆盖并发切换。 */
  selectInteractionDevice: (device_id: string, expected_interaction_generation: number) => request<{
    protocol_version: 1 // protocol_version（设备协议版本）。
    result: 'active' | 'pending_media' // result（已切换或等待媒体设备就绪）。
    device_id: string // device_id（用户明确选择的固定设备编号）。
    interaction_generation: number // interaction_generation（主机授予的交互占用代次）。
    fields: Record<string, string> // fields（字段中文说明）。
  }>('/api/v1/devices/interaction', {
    method: 'POST',
    body: JSON.stringify({ device_id, expected_interaction_generation }),
  }),
  /** createWakeTask（手动创建任务）：同源宿主校验桌面会话，内部凭据不进入前端。 */
  createWakeTask: async (value: {
    text: string
    at?: string
    daily?: { time: string; time_zone: string }
    weekly?: { time: string; time_zone: string; weekdays: number[] }
    /** every_seconds（间隔秒数）：整分钟，范围为 60 至 86400。 */
    every_seconds?: number
    /** loop（是否持续循环）：关闭时必须提供 trigger_count（总触发次数）。 */
    loop?: boolean
    /** trigger_count（总触发次数）：包含第一次触发。 */
    trigger_count?: number
  }) => {
    const response = await fetch('/api/v1/application/wake-tasks', {
      method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(value),
    })
    if (!response.ok) throw await readError(response)
    return await response.json() as WakeTask
  },
  saveInference: (backend: import('./types').InferenceBackend) => request<import('./types').InferenceSnapshot>('/api/v1/settings/inference', { method: 'PUT', body: JSON.stringify({ backend }) }), // saveInference（在请求边界保存并切换主语音推理）。
  snapshot: () => request<ProductSnapshot>('/api/v1/product/snapshot'),
  dshStatus: () => request<DshRuntimeStatus>('/api/v1/dsh/status'),
  launchDsh: () => request<{ url: string }>('/api/v1/dsh/ui-launch', { method: 'POST', body: '{}' }),
  wakeTasks: () => request<{ tasks: WakeTask[] }>('/api/v1/wake-tasks'),
  updateWakeTask: (id: string, value: unknown) => request<WakeTask>(`/api/v1/wake-tasks/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(value) }),
  deleteWakeTask: (id: string) => request<{ deleted: boolean }>(`/api/v1/wake-tasks/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  cancelWakeTask: (id: string) => request<WakeTask>(`/api/v1/wake-tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: '{}' }),
  saveWake: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/wake', jsonBody(value)),
  saveSilence: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/silence', jsonBody(value)),
  saveQuietHours: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/quiet-hours', jsonBody(value)),
  saveShadow: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/shadow', jsonBody(value)),
  saveApprovals: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/approvals', jsonBody(value)), // saveApprovals（保存授权确认偏好）。
  savePersona: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/persona', jsonBody(value)),
  saveCustomInstructions: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/custom-instructions', jsonBody(value)),
  saveSpeaker: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/speaker', jsonBody(value)),
  /** 获取本地模型设备、硬件能力与工作进程资源状态。 */
  computeStatus: (refresh = false) => request<ProductComputeStatus>(`/api/v1/product/compute-status${refresh ? '?refresh=true' : ''}`),
  /** 保存并即时切换当前详情页支持的本地模型设备。 */
  saveCompute: (value: ProductComputeDeviceSwitch) => request<ProductMutationResult>('/api/v1/product/settings/compute', jsonBody(value)),
  /** saveAudioEnhancements（事务切换音频增强）。 */
  saveAudioEnhancements: (value: Record<string, boolean>) => request<{ settings: NonNullable<import('./types').ProductSettingsSnapshot['audio_enhancements']>; message: string }>('/api/v1/product/settings/audio-enhancements', jsonBody(value)),
  /** 读取 TTS（语音合成）固定在线 API（程序接口）的只读状态。 */
  saveTts: (value: Partial<ProductTtsSettings>) => request<ProductMutationResult>('/api/v1/product/settings/tts', jsonBody(value)),
  /** minimaxVoiceName（查询 MiniMax 在线音色名称）：密钥仅由 Voice Server 使用。 */
  minimaxVoiceName: (voice_id: string) => request<ProductMinimaxVoiceName>('/api/v1/tts/minimax/voice-name', { method: 'POST', body: JSON.stringify({ voice_id }) }),
  ttsVoices: (model: ProductLocalTtsModelId) => request<{ voices: ProductTtsVoice[] }>(`/api/v1/tts/voices/${model}`),
  createTtsVoice: (model: ProductLocalTtsModelId, voice: Partial<ProductTtsVoice>) => request<{ voice: ProductTtsVoice }>(`/api/v1/tts/voices/${model}`, { ...jsonBody(voice), method: 'POST' }),
  updateTtsVoice: (model: ProductLocalTtsModelId, id: string, voice: Partial<ProductTtsVoice>, revision: number) => request<{ voice: ProductTtsVoice }>(`/api/v1/tts/voices/${model}/${id}`, jsonBody({ ...voice, expected_revision: revision })),
  deleteTtsVoice: (model: ProductLocalTtsModelId, id: string) => request(`/api/v1/tts/voices/${model}/${id}`, { method: 'DELETE' }),
  selectTtsVoice: (model: ProductLocalTtsModelId, selection: { voice_id: string; revision: number } | null) => request(`/api/v1/tts/voices/${model}/selection`, jsonBody({ selection })),
  restoreDefaultTtsVoice: (model: ProductLocalTtsModelId) => request(`/api/v1/tts/voices/${model}/restore-default`, { method: 'POST' }),
  uploadTtsAsset: (model: ProductLocalTtsModelId, file: File) => request<{ asset: ProductTtsAsset }>(`/api/v1/tts/assets/${model}/import?filename=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file }),
  uploadTtsSource: (model: ProductLocalTtsModelId, file: File) => request<{ asset: ProductTtsAsset }>(`/api/v1/tts/assets/${model}/source?filename=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file }),
  clipTtsAsset: (model: ProductLocalTtsModelId, asset_id: string, start_ms: number, end_ms: number) => request<{ asset: ProductTtsAsset }>(`/api/v1/tts/assets/${model}/${encodeURIComponent(asset_id)}/clip`, { ...jsonBody({ start_ms, end_ms }), method: 'POST' }),
  startAnifLiveReferenceImport: (model: ProductLocalTtsModelId) => request<AnifLiveImportJob>(`/api/v1/tts/aniflive/models/${encodeURIComponent(model)}/references/import`, { ...jsonBody({}), method: 'POST' }),
  /** ttsAssetAudioUrl（参考音频地址）：沿用桌面宿主提供的语音服务地址。 */
  ttsAssetAudioUrl: (model: ProductLocalTtsModelId, asset_id: string) => `${apiBaseUrl()}/api/v1/tts/assets/${model}/${encodeURIComponent(asset_id)}/audio`,
  startTtsPreview: (model: ProductLocalTtsModelId, voice: Partial<ProductTtsVoice>, text: string) => request<ProductTtsOperation>('/api/v1/tts/operations/preview', { ...jsonBody({ model, voice, text }), method: 'POST' }),
  /** startMinimaxTtsPreview（试听 MiniMax 在线音色）：服务端使用固定默认试音文本并通过程序播放器播放。 */
  startMinimaxTtsPreview: (model: ProductOnlineTtsModelId, voiceId: string) => request<ProductTtsOperation>('/api/v1/tts/operations/minimax-preview', { ...jsonBody({ online_model: model, online_voice_id: voiceId }), method: 'POST' }),
  startTtsPlay: (model: ProductLocalTtsModelId, asset_id: string) => request<ProductTtsOperation>('/api/v1/tts/operations/play', { ...jsonBody({ model, asset_id }), method: 'POST' }),
  startTtsTranscribe: (model: ProductLocalTtsModelId, asset_id: string) => request<ProductTtsOperation>('/api/v1/tts/operations/transcribe', { ...jsonBody({ model, asset_id }), method: 'POST' }),
  getTtsOperation: (id: string) => request<ProductTtsOperation>(`/api/v1/tts/operations/${id}`),
  cancelTtsOperation: (id: string) => request<ProductTtsOperation>(`/api/v1/tts/operations/${id}/cancel`, { method: 'POST' }),
  ttsStatus: () => request<ProductLocalTtsStatus>('/api/v1/tts/status'),
  /** setDefaultTtsReference（模型默认参考选择）：空值取消登记，参考记录与历史版本保留。 */
  setDefaultTtsReference: (model: ProductLocalTtsModelId, voice: string, reference: string | null, revision: number) => request<{ voice: ProductTtsVoice }>(`/api/v1/tts/voices/${encodeURIComponent(model)}/${encodeURIComponent(voice)}/default-reference`, { method: 'PUT', body: JSON.stringify({ reference_id: reference, expected_revision: revision }) }),
  /** renameAnifLiveModel（重命名已登记 AnifLive 模型）：保留 model_id（稳定模型标识）及其版本身份。 */
  renameAnifLiveModel: (modelId: ProductLocalTtsModelId, name: string) => request<{
    id: ProductLocalTtsModelId // id（稳定模型标识）。
    name: string // name（已保存的模型显示名称）。
  }>(`/api/v1/tts/aniflive/models/${encodeURIComponent(modelId)}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  /** scanAnifLiveImport（只读扫描 AnifLive 输入）：不会下载网页仓库文件。 */
  scanAnifLiveImport: (source_type: AnifLiveImportScan['source_type'], source: string) => request<AnifLiveImportScan>('/api/v1/tts/aniflive/import/scan', { method: 'POST', body: JSON.stringify({ source_type, source }) }),
  /** startAnifLiveImport（确认后暂存选中文件）：网页来源此时才开始下载。 */
  startAnifLiveImport: (value: AnifLiveImportRequest) => request<AnifLiveImportJob>('/api/v1/tts/aniflive/import/jobs', { method: 'POST', body: JSON.stringify(value) }),
  getAnifLiveImport: (task_id: string) => request<AnifLiveImportJob>(`/api/v1/tts/aniflive/import/jobs/${encodeURIComponent(task_id)}`),
  pendingAnifLiveImports: () => request<{ jobs: AnifLiveImportJob[] }>('/api/v1/tts/aniflive/import/pending'),
  /** completeAnifLiveImportReference（补齐参考录音与人工校对后的转写）。 */
  completeAnifLiveImportReference: (task_id: string, value: { reference_asset_id?: string; reference_text: string; reference_language: string }) => request<AnifLiveImportJob>(`/api/v1/tts/aniflive/import/jobs/${encodeURIComponent(task_id)}/reference`, { method: 'POST', body: JSON.stringify(value) }),
  speakerStatus: () => request<Record<string, unknown>>('/api/v1/speaker/status'),
  speakerProfile: () => request<Record<string, unknown>>('/api/v1/speaker/profile'),
  startSpeakerEnrollment: () => request<Record<string, unknown>>('/api/v1/speaker/enrollment/start', { method: 'POST' }),
  addSpeakerEnrollmentSegment: (id: string, value: unknown) => request<Record<string, unknown>>(`/api/v1/speaker/enrollment/${encodeURIComponent(id)}/segment`, { method: 'POST', body: JSON.stringify(value) }),
  captureSpeakerEnrollmentSegment: (id: string, value: unknown, signal?: AbortSignal) => request<Record<string, unknown>>(`/api/v1/speaker/enrollment/${encodeURIComponent(id)}/capture`, { method: 'POST', body: JSON.stringify(value), signal }),
  captureSpeakerTest: (value: unknown, signal?: AbortSignal) => request<Record<string, unknown>>('/api/v1/speaker/test/capture', { method: 'POST', body: JSON.stringify(value), signal }),
  /** stopSpeakerCapture（停止或取消采集）：后台释放本次流，页面卸载时也尽力送达。 */
  stopSpeakerCapture: (owner: string | null, captureId: string, cancel = false) => request<Record<string, unknown>>(owner ? `/api/v1/speaker/enrollment/${encodeURIComponent(owner)}/capture/stop` : '/api/v1/speaker/test/capture/stop', { method: 'POST', body: JSON.stringify({ capture_id: captureId, cancel }), keepalive: true }),
  commitSpeakerEnrollment: (id: string) => request<Record<string, unknown>>(`/api/v1/speaker/enrollment/${encodeURIComponent(id)}/commit`, { method: 'POST' }),
  cancelSpeakerEnrollment: (id: string) => request<Record<string, unknown>>(`/api/v1/speaker/enrollment/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  deleteSpeakerTemplate: (id: string) => request<Record<string, unknown>>(`/api/v1/speaker/templates/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  setSpeakerTemplateCentroid: (id: string, include: boolean) => request<Record<string, unknown>>(`/api/v1/speaker/templates/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ include_in_centroid: include }) }),
  testSpeaker: (value: unknown) => request<Record<string, unknown>>('/api/v1/speaker/test', { method: 'POST', body: JSON.stringify(value) }),
  registry: () => request<ProductSnapshot['models']>('/api/v1/models/registry'),
  fixedCredentials: () => request<import('./types').FixedModelCredentials>('/api/v1/models/credentials'),
  /** fixedProviderUsage（服务商账户用量）：密钥留在后端，只返回余额或订阅余量。 */
  fixedProviderUsage: (provider: 'deepseek' | 'minimax') => request<import('./types').FixedProviderUsageResponse>(`/api/v1/models/credentials/${provider}/usage`),
  saveFixedCredentials: (value: import('./types').FixedModelCredentialUpdate) => request<import('./types').FixedModelCredentials>('/api/v1/models/credentials', { method: 'PUT', body: JSON.stringify(value) }),
  scanModels: () => request<ProductSnapshot['models']>('/api/v1/models/scan', { method: 'POST' }),
  validateProvider: (value: unknown) => request<Record<string, unknown>>('/api/v1/models/validate', { method: 'POST', body: JSON.stringify(value) }),
  addProvider: (value: unknown) => request<ProductSnapshot['models']>('/api/v1/models/providers', { method: 'POST', body: JSON.stringify(value) }),
  updateModel: (id: string, value: unknown) => request<ProductSnapshot['models']>(`/api/v1/models/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(value) }),
  deleteModel: (id: string) => request<ProductSnapshot['models']>(`/api/v1/models/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  hideModel: (id: string) => request<ProductSnapshot['models']>(`/api/v1/models/${encodeURIComponent(id)}/hide`, { method: 'POST' }),
  restoreModel: (id: string) => request<ProductSnapshot['models']>(`/api/v1/models/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
  applyAssignments: (assignments: Record<string, unknown>, confirmed: boolean) => request<Record<string, unknown>>('/api/v1/models/assignments/apply', { method: 'POST', body: JSON.stringify({ assignments, confirmed }) }),
}

async function devicePairingMutation(value: Record<string, unknown>): Promise<DevicePairingStatus> {
  const response = await fetch('/api/v1/application/device-pairing', {
    method: 'POST', credentials: 'same-origin', cache: 'no-store',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(value),
  })
  return await readPairingResponse<DevicePairingStatus>(response)
}

export { apiBaseUrl }

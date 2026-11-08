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
  reference_registration?: { ready: number; pending: number; voice_id: string; revision: number } // reference_registration（参考登记统计与音色版本）。
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

export { apiBaseUrl }

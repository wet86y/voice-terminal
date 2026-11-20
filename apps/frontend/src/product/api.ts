import type {
  DshRuntimeStatus,
  ProductApiErrorShape,
  ProductComputeStatus,
  ProductMutationResult,
  ProductComputeDeviceSwitch,
  ProductSnapshot,
  ProductSettingsSnapshot,
  WakeTask, ProductTtsSettings, ProductLocalTtsModelId, ProductOnlineTtsModelId, ProductLocalTtsStatus, ProductMinimaxVoiceName, ProductTtsVoice, ProductTtsAsset, ProductTtsOperation,
} from './types'
import { locateNativeInvoker } from '../runtime/desktop-preflight'
import { nativeDeviceApi, type RemoteManagementOperation } from './native-device-api'

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
  if (configuration?.remote_mode === true) {
    throw new ProductApiError('远程产品请求不能使用客户端语音服务地址。', 501, {
      code: 'REMOTE_LOCAL_HTTP_DISABLED', // code（业务错误码）：禁止远程页面使用客户端本机 HTTP。
    }, 'REMOTE_LOCAL_HTTP_DISABLED')
  }
  if (configuration?.business_ready === false) throw new Error('应用宿主尚未允许业务请求。')
  const configuredUrl = configuration?.voice_api_url
  if (!configuredUrl) throw new Error('应用宿主尚未提供语音服务接口地址。')
  return configuredUrl.replace(/\/$/, '')
}

const REMOTE_PRODUCT_OPERATIONS: Record<string, RemoteManagementOperation> = {
  'GET /api/v1/product/snapshot': 'product.snapshot',
  'GET /api/v1/devices': 'product.devices',
  'POST /api/v1/devices/interaction': 'product.devices.select',
  'GET /api/v1/product/compute-status': 'product.compute_status',
  'GET /api/v1/product/compute-status?refresh=true': 'product.compute_status.refresh',
  'GET /api/v1/dsh/status': 'product.dsh_status',
  'POST /api/v1/application/dsh-approval': 'dsh.approval.decide', // dsh.approval.decide（提交服务端权限答复）。
  'GET /api/v1/wake-tasks': 'product.wake_tasks',
  'POST /api/v1/application/wake-tasks': 'wake_task.create', // wake_task.create（服务端创建定时任务）。
  'GET /api/v1/product/settings/hotwords': 'settings.hotwords.read',
  'PUT /api/v1/product/settings/hotwords': 'settings.hotwords.save',
  'POST /api/v1/product/hotwords/import': 'hotwords.import',
  'POST /api/v1/product/hotwords/preview': 'hotwords.preview',
  'PUT /api/v1/product/settings/wake': 'settings.wake.save',
  'PUT /api/v1/product/settings/silence': 'settings.silence.save',
  'PUT /api/v1/product/settings/quiet-hours': 'settings.quiet_hours.save',
  'PUT /api/v1/product/settings/shadow': 'settings.shadow.save',
  'PUT /api/v1/product/settings/approvals': 'settings.approvals.save',
  'PUT /api/v1/product/settings/persona': 'settings.persona.save',
  'PUT /api/v1/product/settings/custom-instructions': 'settings.custom_instructions.save',
  'PUT /api/v1/product/settings/speaker': 'settings.speaker.save',
  'PUT /api/v1/product/settings/compute': 'settings.compute.save',
  'PUT /api/v1/product/settings/audio-enhancements': 'settings.audio_enhancements.save',
  'PUT /api/v1/product/settings/screen-capture': 'settings.screen_capture.save',
  'PUT /api/v1/product/settings/tts': 'settings.tts.save',
  'PUT /api/v1/settings/inference': 'settings.inference.save',
  'GET /api/v1/models/registry': 'models.registry.read',
  'POST /api/v1/models/scan': 'models.scan',
  'POST /api/v1/models/validate': 'models.validate',
  'POST /api/v1/models/providers': 'models.providers.add',
  'POST /api/v1/models/assignments/apply': 'models.assignments.apply',
  'GET /api/v1/models/credentials': 'models.credentials.read',
  'PUT /api/v1/models/credentials': 'models.credentials.save',
  'GET /api/v1/tts/status': 'tts.status',
  'GET /api/v1/speaker/status': 'speaker.status',
  'GET /api/v1/speaker/profile': 'speaker.profile',
  'POST /api/v1/tts/minimax/voice-name': 'tts.minimax.voice_name',
  'POST /api/v1/tts/operations/preview': 'tts.operation.preview',
  'POST /api/v1/tts/operations/minimax-preview': 'tts.operation.minimax_preview',
  'POST /api/v1/tts/operations/play': 'tts.operation.play',
  'POST /api/v1/tts/operations/transcribe': 'tts.operation.transcribe',
  'GET /api/v1/tts/aniflive/import/pending': 'tts.aniflive.import.pending',
  'POST /api/v1/tts/aniflive/import/scan': 'tts.aniflive.import.scan',
  'POST /api/v1/tts/aniflive/import/jobs': 'tts.aniflive.import.start',
  'POST /api/v1/speaker/enrollment/start': 'speaker.enrollment.start',
  'POST /api/v1/speaker/test/capture': 'speaker.test.capture',
  'POST /api/v1/speaker/test/capture/stop': 'speaker.test.capture_stop',
  'POST /api/v1/speaker/test': 'speaker.test.run',
}

async function remoteModeSelected(): Promise<boolean> {
  const application = typeof window === 'undefined' ? undefined : window.__VOICE_APPLICATION__
  const configuredRemoteMode = application?.remote_mode === true
  if (configuredRemoteMode && application?.business_ready === false) {
    throw new ProductApiError('服务端语音核心或产品业务尚未就绪；请等待服务端状态恢复后再操作。', 503, {
      code: 'REMOTE_BUSINESS_NOT_READY', // code（业务错误码）：服务端产品业务当前不可访问。
    }, 'REMOTE_BUSINESS_NOT_READY')
  }
  if (!locateNativeInvoker()) {
    if (configuredRemoteMode) {
      throw new ProductApiError('远程模式缺少原生管理通道，已取消产品请求。', 0, {
        code: 'REMOTE_TARGET_UNAVAILABLE', // code（业务错误码）：远程目标无法安全确认。
      }, 'REMOTE_TARGET_UNAVAILABLE')
    }
    return false
  }
  try {
    const status = await nativeDeviceApi.deviceConfigStatus()
    if (status?.runtime?.mode === 'remote') return true
    if (status?.runtime?.mode === 'local') return false
    throw new Error('设备配置没有提供有效的运行模式。')
  } catch {
    throw new ProductApiError(
      '无法确认产品请求的目标主机，已取消请求。',
      0,
      { code: 'REMOTE_TARGET_UNAVAILABLE' }, // code（业务错误码）：远程目标无法安全确认。
      'REMOTE_TARGET_UNAVAILABLE',
    )
  }
}

async function remoteProductResponse(path: string, init: RequestInit): Promise<Response> {
  const method = (init.method ?? (init.body === undefined ? 'GET' : 'POST')).toUpperCase()
  const pathWithoutQuery = path.split('?', 1)[0]
  let operation = REMOTE_PRODUCT_OPERATIONS[`${method} ${path}`]
    ?? REMOTE_PRODUCT_OPERATIONS[`${method} ${pathWithoutQuery}`]
  let pathArguments: Record<string, unknown> = {}
  let bodyRequired = operation === 'dsh.approval.decide'
  if (!operation) {
    const wakeTask = /^\/api\/v1\/wake-tasks\/([^/]+)(\/cancel)?$/.exec(pathWithoutQuery)
    const model = /^\/api\/v1\/models\/([^/]+)(?:\/(hide|restore))?$/.exec(pathWithoutQuery)
    const providerUsage = /^\/api\/v1\/models\/credentials\/(deepseek|minimax)\/usage$/.exec(pathWithoutQuery)
    const ttsVoice = /^\/api\/v1\/tts\/voices\/([^/]+)(?:\/([^/]+))?$/.exec(pathWithoutQuery)
    const ttsOperation = /^\/api\/v1\/tts\/operations\/([^/]+)(\/cancel)?$/.exec(pathWithoutQuery)
    const ttsDefaultReference = /^\/api\/v1\/tts\/voices\/([^/]+)\/([^/]+)\/default-reference$/.exec(pathWithoutQuery)
    const ttsClip = /^\/api\/v1\/tts\/assets\/([^/]+)\/([^/]+)\/clip$/.exec(pathWithoutQuery)
    const ttsAnifReferenceImport = /^\/api\/v1\/tts\/aniflive\/models\/([^/]+)\/references\/import$/.exec(pathWithoutQuery)
    const ttsAnifImportJob = /^\/api\/v1\/tts\/aniflive\/import\/jobs\/([^/]+)(\/reference)?$/.exec(pathWithoutQuery)
    const ttsAnifModel = /^\/api\/v1\/tts\/aniflive\/models\/([^/]+)$/.exec(pathWithoutQuery)
    const speakerEnrollment = /^\/api\/v1\/speaker\/enrollment\/([^/]+)(?:\/(capture\/stop|segment|capture|commit))?$/.exec(pathWithoutQuery)
    const speakerTemplate = /^\/api\/v1\/speaker\/templates\/([^/]+)$/.exec(pathWithoutQuery)
    try {
      if (wakeTask) {
        const taskId = decodeURIComponent(wakeTask[1])
        if (wakeTask[2] === '/cancel' && method === 'POST') {
          operation = 'wake_task.cancel'
          pathArguments = { task_id: taskId }
        } else if (!wakeTask[2] && method === 'DELETE') {
          operation = 'wake_task.delete'
          pathArguments = { task_id: taskId }
        } else if (!wakeTask[2] && method === 'PATCH') {
          operation = 'wake_task.update'
          pathArguments = { task_id: taskId }
          bodyRequired = true
        }
      } else if (providerUsage && method === 'GET') {
        operation = 'provider.usage'
        pathArguments = { provider: providerUsage[1] }
      } else if (model && method === 'PATCH' && !model[2]) {
        operation = 'model.update'
        pathArguments = { model_id: decodeURIComponent(model[1]) }
        bodyRequired = true
      } else if (model && method === 'DELETE' && !model[2]) {
        operation = 'model.delete'
        pathArguments = { model_id: decodeURIComponent(model[1]) }
      } else if (model && method === 'POST' && model[2] === 'hide') {
        operation = 'model.hide'
        pathArguments = { model_id: decodeURIComponent(model[1]) }
      } else if (model && method === 'POST' && model[2] === 'restore') {
        operation = 'model.restore'
        pathArguments = { model_id: decodeURIComponent(model[1]) }
      } else if (ttsDefaultReference && method === 'PUT') {
        operation = 'tts.voices.default_reference'
        pathArguments = { model_id: decodeURIComponent(ttsDefaultReference[1]), voice_id: decodeURIComponent(ttsDefaultReference[2]) }
        bodyRequired = true
      } else if (ttsClip && method === 'POST') {
        operation = 'tts.assets.clip'
        pathArguments = { model_id: decodeURIComponent(ttsClip[1]), asset_id: decodeURIComponent(ttsClip[2]) }
        bodyRequired = true
      } else if (ttsVoice && ttsVoice[2] === 'selection' && method === 'PUT') {
        operation = 'tts.voices.select'
        pathArguments = { model_id: decodeURIComponent(ttsVoice[1]) }
        bodyRequired = true
      } else if (ttsVoice && ttsVoice[2] === 'restore-default' && method === 'POST') {
        operation = 'tts.voices.restore_default'
        pathArguments = { model_id: decodeURIComponent(ttsVoice[1]) }
      } else if (ttsVoice && ttsVoice[2] === 'capabilities' && method === 'GET') {
        operation = 'tts.voices.capabilities'
        pathArguments = { model_id: decodeURIComponent(ttsVoice[1]) }
      } else if (ttsVoice && ttsVoice[2] && method === 'DELETE') {
        operation = 'tts.voices.delete'
        pathArguments = { model_id: decodeURIComponent(ttsVoice[1]), voice_id: decodeURIComponent(ttsVoice[2]) }
      } else if (ttsVoice && ttsVoice[2] && method === 'PUT') {
        operation = 'tts.voices.update'
        pathArguments = { model_id: decodeURIComponent(ttsVoice[1]), voice_id: decodeURIComponent(ttsVoice[2]) }
        bodyRequired = true
      } else if (ttsVoice && !ttsVoice[2] && method === 'POST') {
        operation = 'tts.voices.create'
        pathArguments = { model_id: decodeURIComponent(ttsVoice[1]) }
        bodyRequired = true
      } else if (ttsVoice && !ttsVoice[2] && method === 'GET') {
        operation = 'tts.voices.read'
        pathArguments = { model_id: decodeURIComponent(ttsVoice[1]) }
      } else if (ttsOperation && ttsOperation[2] === '/cancel' && method === 'POST') {
        operation = 'tts.operation.cancel'
        pathArguments = { operation_id: decodeURIComponent(ttsOperation[1]) }
      } else if (ttsOperation && !ttsOperation[2] && method === 'GET') {
        operation = 'tts.operation.read'
        pathArguments = { operation_id: decodeURIComponent(ttsOperation[1]) }
      } else if (ttsAnifReferenceImport && method === 'POST') {
        operation = 'tts.aniflive.references.import'
        pathArguments = { model_id: decodeURIComponent(ttsAnifReferenceImport[1]) }
        bodyRequired = true
      } else if (ttsAnifModel && method === 'PATCH') {
        operation = 'tts.aniflive.model.rename'
        pathArguments = { model_id: decodeURIComponent(ttsAnifModel[1]) }
        bodyRequired = true
      } else if (ttsAnifImportJob && ttsAnifImportJob[2] === '/reference' && method === 'POST') {
        operation = 'tts.aniflive.import.reference'
        pathArguments = { task_id: decodeURIComponent(ttsAnifImportJob[1]) }
        bodyRequired = true
      } else if (ttsAnifImportJob && !ttsAnifImportJob[2] && method === 'GET') {
        operation = 'tts.aniflive.import.read'
        pathArguments = { task_id: decodeURIComponent(ttsAnifImportJob[1]) }
      } else if (speakerEnrollment && speakerEnrollment[2] === 'segment' && method === 'POST') {
        operation = 'speaker.enrollment.segment'
        pathArguments = { enrollment_id: decodeURIComponent(speakerEnrollment[1]) }
        bodyRequired = true
      } else if (speakerEnrollment && speakerEnrollment[2] === 'capture' && method === 'POST') {
        operation = 'speaker.enrollment.capture'
        pathArguments = { enrollment_id: decodeURIComponent(speakerEnrollment[1]) }
        bodyRequired = true
      } else if (speakerEnrollment && speakerEnrollment[2] === 'capture/stop' && method === 'POST') {
        operation = 'speaker.enrollment.capture_stop'
        pathArguments = { enrollment_id: decodeURIComponent(speakerEnrollment[1]) }
        bodyRequired = true
      } else if (speakerEnrollment && speakerEnrollment[2] === 'commit' && method === 'POST') {
        operation = 'speaker.enrollment.commit'
        pathArguments = { enrollment_id: decodeURIComponent(speakerEnrollment[1]) }
      } else if (speakerEnrollment && !speakerEnrollment[2] && method === 'DELETE') {
        operation = 'speaker.enrollment.delete'
        pathArguments = { enrollment_id: decodeURIComponent(speakerEnrollment[1]) }
      } else if (speakerTemplate && method === 'DELETE') {
        operation = 'speaker.template.delete'
        pathArguments = { template_id: decodeURIComponent(speakerTemplate[1]) }
      } else if (speakerTemplate && method === 'PATCH') {
        operation = 'speaker.template.update'
        pathArguments = { template_id: decodeURIComponent(speakerTemplate[1]) }
        bodyRequired = true
      }
    } catch {
      throw new ProductApiError('远程产品资源编号编码无效。', 400)
    }
  }
  if (!operation) {
    throw new ProductApiError('此远程产品操作尚未接入服务端固定操作接口。', 501, {
      code: 'REMOTE_OPERATION_UNAVAILABLE', operation: `${method} ${pathWithoutQuery}`,
    })
  }
  let argumentsValue = pathArguments
  if (operation === 'dsh.approval.decide') {
    if (typeof init.body !== 'string') {
      throw new ProductApiError('远程 DSH 权限答复缺少结构化 JSON 参数。', 400)
    }
    let value: unknown
    try { value = JSON.parse(init.body) as unknown }
    catch { throw new ProductApiError('远程 DSH 权限答复不是有效 JSON。', 400) }
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new ProductApiError('远程 DSH 权限答复必须是 JSON 对象。', 400)
    }
    const fields = value as Record<string, unknown>
    const keys = Object.keys(fields).sort()
    if (keys.join(',') !== 'approval_id,decision,run_seq'
      || typeof fields.approval_id !== 'string' || fields.approval_id.length < 1 || fields.approval_id.length > 160
      || !['allowed-once', 'allowed-until-silence', 'rejected'].includes(String(fields.decision))
      || !Number.isSafeInteger(fields.run_seq) || (fields.run_seq as number) < 0) {
      throw new ProductApiError('远程 DSH 权限答复字段或取值无效。', 400)
    }
    argumentsValue = fields
  } else if (init.body !== undefined && init.body !== null) {
    if (typeof init.body !== 'string') {
      throw new ProductApiError('远程产品操作只接受结构化 JSON 参数。', 415)
    }
    try {
      const value: unknown = JSON.parse(init.body)
      if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('shape')
      if (Object.keys(pathArguments).length > 0) {
        if (bodyRequired) argumentsValue = { ...pathArguments, value }
        else if (Object.keys(value).length === 0) argumentsValue = pathArguments
        else throw new Error('unexpected-body')
      } else {
        argumentsValue = { value }
      }
    } catch {
      throw new ProductApiError('远程产品操作参数不是有效 JSON 对象或字段不匹配。', 400)
    }
  } else if (bodyRequired) {
    throw new ProductApiError('远程产品操作缺少结构化 JSON 参数。', 400)
  }
  if (operation === 'tts.aniflive.import.scan') {
    const source = argumentsValue.value
    if (typeof source === 'object' && source !== null && !Array.isArray(source)
      && (source as Record<string, unknown>).source_type === 'local') {
      throw new ProductApiError('客户端本机目录须通过受授权文件传输上传；不会把客户端路径发送给服务端。', 501, {
        code: 'REMOTE_FILE_TRANSFER_REQUIRED', operation: 'tts.aniflive.import.scan',
      })
    }
  }
  const result = await nativeDeviceApi.remoteManagementRequest(operation, argumentsValue)
  return new Response(JSON.stringify(result.body), {
    status: result.status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  })
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

function productRequestScope() {
  const application = typeof window === 'undefined' ? undefined : window.__VOICE_APPLICATION__
  return JSON.stringify([application?.remote_mode === true, application?.host_id,
    application?.endpoint_revision, application?.voice_api_url])
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const scope = productRequestScope() // scope（本次请求的服务端及连接代次）。
  const checkScope = () => {
    if (scope !== productRequestScope()) throw new ProductApiError(
      '请求目标已变化，请刷新当前服务端状态；原请求结果以原服务端为准。', 409,
      undefined, 'STALE_SERVICE_TARGET')
  }
  let response: Response
  try {
    const remote = await remoteModeSelected()
    checkScope()
    if (remote) {
      response = await remoteProductResponse(path, init)
    } else {
      response = await fetch(`${apiBaseUrl()}${path}`, {
        ...init,
        headers: {
          Accept: 'application/json',
          ...(init.body ? { 'Content-Type': 'application/json' } : {}),
          ...(init.headers || {}),
        },
      })
    }
  } catch (error) {
    if (error instanceof ProductApiError) throw error
    throw new ProductApiError(error instanceof Error ? error.message : '无法连接 Voice Server。')
  }
  checkScope()
  if (!response.ok) throw await readError(response)
  const result = await response.json() as T
  checkScope()
  return result
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
  decideDshApproval: async (value: {
    approval_id: string // approval_id（待处理的服务端权限请求编号）。
    decision: 'allowed-once' | 'allowed-until-silence' | 'rejected' // decision（用户对权限请求的答复）。
    run_seq: number // run_seq（服务端运行序号，用于拒绝已过期请求）。
  }) => {
    if (await remoteModeSelected()) {
      return request<{ result: string }>('/api/v1/application/dsh-approval', {
        method: 'POST', body: JSON.stringify(value),
      })
    }
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
    text: string // text（保存的任务说明）。
    at?: string // at（单次触发的 ISO（国际标准化组织）时间）。
    daily?: {
      time: string // time（本地时区执行时刻）。
      time_zone: string // time_zone（执行时区编号）。
    } // daily（每日执行规则）。
    weekly?: {
      time: string // time（本地时区执行时刻）。
      time_zone: string // time_zone（执行时区编号）。
      weekdays: number[] // weekdays（执行星期，1 为周一至 7 为周日）。
    } // weekly（每周执行规则）。
    /** every_seconds（间隔秒数）：整分钟，范围为 60 至 86400。 */
    every_seconds?: number
    /** loop（是否持续循环）：关闭时必须提供 trigger_count（总触发次数）。 */
    loop?: boolean
    /** trigger_count（总触发次数）：包含第一次触发。 */
    trigger_count?: number
  }) => {
    const init: RequestInit = {
      method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(value),
    }
    // 远程客户端通过固定管理操作创建服务端任务；本地模式保留同源桌面会话校验。
    if (await remoteModeSelected()) return request<WakeTask>('/api/v1/application/wake-tasks', init)
    const response = await fetch('/api/v1/application/wake-tasks', {
      ...init, credentials: 'same-origin',
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
  saveAudioEnhancements: (value: Record<string, boolean>) => request<{ settings: NonNullable<import('./types').ProductSettingsSnapshot['audio_enhancements']>; message: string; settings_snapshot?: ProductSettingsSnapshot }>('/api/v1/product/settings/audio-enhancements', jsonBody(value)),
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

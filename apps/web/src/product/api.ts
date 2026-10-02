import type {
  DshRuntimeStatus,
  ProductApiErrorShape,
  ProductComputeStatus,
  ProductMutationResult,
  ProductSnapshot,
  WakeTask,
} from './types'

const DEFAULT_API_URL = 'http://127.0.0.1:8765'

export class ProductApiError extends Error implements ProductApiErrorShape {
  readonly status: number

  constructor(message: string, status = 0) {
    super(message)
    this.name = 'ProductApiError'
    this.status = status
  }
}

function apiBaseUrl() {
  return ((typeof window !== 'undefined' && window.__VOICE_APPLICATION__?.voice_api_url) || import.meta.env.VITE_VOICE_API_URL || DEFAULT_API_URL).replace(/\/$/, '')
}

async function readError(response: Response): Promise<string> {
  try {
    const payload: unknown = await response.json()
    if (typeof payload === 'object' && payload !== null) {
      const record = payload as Record<string, unknown>
      if (typeof record.detail === 'string') return record.detail
      if (typeof record.detail === 'object' && record.detail !== null) {
        const detail = record.detail as Record<string, unknown>
        if (typeof detail.message === 'string') return detail.message
      }
      if (typeof record.message === 'string') return record.message
    }
  } catch {
    // Fall through to the stable HTTP status below.
  }
  return `请求失败（HTTP ${response.status}）。`
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
  if (!response.ok) throw new ProductApiError(await readError(response), response.status)
  return await response.json() as T
}

function jsonBody(value: unknown): RequestInit {
  return { method: 'PUT', body: JSON.stringify(value) }
}

export const productApi = {
  snapshot: () => request<ProductSnapshot>('/api/v1/product/snapshot'),
  dshStatus: () => request<DshRuntimeStatus>('/api/v1/dsh/status'),
  launchDsh: () => request<{ url: string }>('/api/v1/dsh/ui-launch', { method: 'POST', body: '{}' }),
  wakeTasks: () => request<{ tasks: WakeTask[] }>('/api/v1/wake-tasks'),
  updateWakeTask: (id: string, value: unknown) => request<WakeTask>(`/api/v1/wake-tasks/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(value) }),
  deleteWakeTask: (id: string) => request<{ deleted: boolean }>(`/api/v1/wake-tasks/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  saveWake: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/wake', jsonBody(value)),
  saveSilence: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/silence', jsonBody(value)),
  saveQuietHours: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/quiet-hours', jsonBody(value)),
  saveShadow: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/shadow', jsonBody(value)),
  savePersona: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/persona', jsonBody(value)),
  saveCustomInstructions: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/custom-instructions', jsonBody(value)),
  saveSpeaker: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/speaker', jsonBody(value)),
  /** 获取本地模型设备、硬件能力与工作进程资源状态。 */
  computeStatus: () => request<ProductComputeStatus>('/api/v1/product/compute-status'),
  /** 保存本地模型设备偏好；返回值会标明需要重启应用。 */
  saveCompute: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/compute', jsonBody(value)),
  /** 读取 TTS（语音合成）固定在线 API（程序接口）的只读状态。 */
  ttsStatus: () => request<{ backend: 'minimax'; worker: { state: 'stopped' } }>('/api/v1/tts/status'),
  speakerStatus: () => request<Record<string, unknown>>('/api/v1/speaker/status'),
  speakerProfile: () => request<Record<string, unknown>>('/api/v1/speaker/profile'),
  startSpeakerEnrollment: () => request<Record<string, unknown>>('/api/v1/speaker/enrollment/start', { method: 'POST' }),
  addSpeakerEnrollmentSegment: (id: string, value: unknown) => request<Record<string, unknown>>(`/api/v1/speaker/enrollment/${encodeURIComponent(id)}/segment`, { method: 'POST', body: JSON.stringify(value) }),
  captureSpeakerEnrollmentSegment: (id: string, value: unknown) => request<Record<string, unknown>>(`/api/v1/speaker/enrollment/${encodeURIComponent(id)}/capture`, { method: 'POST', body: JSON.stringify(value) }),
  captureSpeakerTest: (value: unknown) => request<Record<string, unknown>>('/api/v1/speaker/test/capture', { method: 'POST', body: JSON.stringify(value) }),
  commitSpeakerEnrollment: (id: string) => request<Record<string, unknown>>(`/api/v1/speaker/enrollment/${encodeURIComponent(id)}/commit`, { method: 'POST' }),
  cancelSpeakerEnrollment: (id: string) => request<Record<string, unknown>>(`/api/v1/speaker/enrollment/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  deleteSpeakerTemplate: (id: string) => request<Record<string, unknown>>(`/api/v1/speaker/templates/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  setSpeakerTemplateCentroid: (id: string, include: boolean) => request<Record<string, unknown>>(`/api/v1/speaker/templates/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ include_in_centroid: include }) }),
  testSpeaker: (value: unknown) => request<Record<string, unknown>>('/api/v1/speaker/test', { method: 'POST', body: JSON.stringify(value) }),
  ambientContext: (limit = 20) => request<Record<string, unknown>>(`/api/v1/ambient-context?limit=${limit}`),
  clearAmbientContext: () => request<Record<string, unknown>>('/api/v1/ambient-context', { method: 'DELETE' }),
  registry: () => request<ProductSnapshot['models']>('/api/v1/models/registry'),
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

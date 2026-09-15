import type {
  ProductApiErrorShape,
  ProductMutationResult,
  ProductSnapshot,
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
  return (import.meta.env.VITE_VOICE_API_URL || DEFAULT_API_URL).replace(/\/$/, '')
}

async function readError(response: Response): Promise<string> {
  try {
    const payload: unknown = await response.json()
    if (typeof payload === 'object' && payload !== null) {
      const record = payload as Record<string, unknown>
      if (typeof record.detail === 'string') return record.detail
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
  saveWake: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/wake', jsonBody(value)),
  saveSilence: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/silence', jsonBody(value)),
  savePersona: (value: unknown) => request<ProductMutationResult>('/api/v1/product/settings/persona', jsonBody(value)),
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

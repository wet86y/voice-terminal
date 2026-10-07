/** Voice API（语音接口）：项目回调鉴权、超时与取消，返回已验证的 JSON（结构化数据）对象。 */
import { CALLBACK_ORIGIN, TOKEN } from '../shared/environment.js'
import { voiceFetch } from '../shared/transport.js'
import type { Json } from '../shared/protocol.js'

export async function voiceApi(path: string, method: string, body?: unknown, signal?: AbortSignal): Promise<Record<string, Json>> {
  if (!CALLBACK_ORIGIN.startsWith('http://127.0.0.1:') || !TOKEN) throw new Error('Voice Bridge（语音桥接）不可用。')
  const response = await voiceFetch(path, { method, body, signal, timeoutMs: 15_000, acceptJson: true })
  const data: unknown = await response.json().catch(() => ({}))
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Voice 回调必须返回 JSON（结构化数据）对象。')
  const record = data as Record<string, Json>
  if (!response.ok) throw new Error(String(record.detail ?? `HTTP ${response.status}`))
  return record
}

/** Voice callback（语音回调）：验证本机回执，不持有会话状态。 */
import { BRIDGE_PROTOCOL_VERSION } from './protocol.js'
import type { Json } from './protocol.js'
import { CALLBACK_ORIGIN } from './environment.js'
import { voiceFetch } from './transport.js'

/**
 * Publish a Bridge callback（发送桥接回调并校验结构化回执）。
 * @param path - Callback route（回调路由）。
 * @param payload - Protocol fields（协议字段）。
 * @param signal - Caller cancellation（调用者取消信号）。
 * @returns Voice receipt（语音端回执对象）。
 */
export async function callback(path: string, payload: Record<string, Json>, signal?: AbortSignal): Promise<Record<string, Json>> {
  if (!CALLBACK_ORIGIN || !CALLBACK_ORIGIN.startsWith('http://127.0.0.1:')) throw new Error('callback unavailable')
  const response = await voiceFetch(path, {
    method: 'POST', signal,
    body: { protocol_version: BRIDGE_PROTOCOL_VERSION, ...payload },
  })
  const value: unknown = await response.json()
  if (!response.ok || value === null || typeof value !== 'object' || Array.isArray(value)) {
    const status = value && typeof value === 'object' && !Array.isArray(value)
      && typeof (value as Record<string, unknown>).status === 'string'
      ? `: ${(value as Record<string, unknown>).status}` : ''
    throw new Error(`callback rejected (${response.status})${status}`)
  }
  return value as Record<string, Json>
}

/** CallbackScope（回调生命周期）：卸载时取消并等待所有已发出的回调。 */
export class CallbackScope {
  private readonly controller = new AbortController()
  private readonly pending = new Set<Promise<unknown>>()
  readonly signal = this.controller.signal

  send(path: string, payload: Record<string, Json>, caller?: AbortSignal): Promise<Record<string, Json>> {
    const signal = caller ? AbortSignal.any([this.signal, caller]) : this.signal
    const request = callback(path, payload, signal)
    this.pending.add(request)
    void request.then(() => this.pending.delete(request), () => this.pending.delete(request))
    return request
  }

  async dispose(): Promise<void> {
    this.controller.abort()
    await Promise.allSettled([...this.pending])
  }
}

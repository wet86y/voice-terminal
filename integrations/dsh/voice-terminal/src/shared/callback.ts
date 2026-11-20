/** Voice callback（语音回调）：验证本机回执，不持有会话状态。 */
import { BRIDGE_PROTOCOL_VERSION } from './protocol.js'
import type { Json } from './protocol.js'
import { CALLBACK_ORIGIN } from './environment.js'
import { voiceFetch } from './transport.js'
import { AttachmentBody } from './attachment-transport.js'

async function callbackResponse(path: string, payload: Record<string, Json>, signal?: AbortSignal): Promise<Response> {
  if (!CALLBACK_ORIGIN || !CALLBACK_ORIGIN.startsWith('http://127.0.0.1:')) throw new Error('callback unavailable')
  return voiceFetch(path, {
    method: 'POST', signal,
    body: { protocol_version: BRIDGE_PROTOCOL_VERSION, ...payload },
  })
}

function callbackError(response: Response, value: unknown): Error {
  const details = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined
  const message = typeof details?.detail === 'string' ? details.detail
    : typeof details?.status === 'string' ? details.status : ''
  const suffix = message ? `: ${message.slice(0, 320)}` : ''
  return new Error(`callback rejected (${response.status})${suffix}`)
}

/**
 * Publish a Bridge callback（发送桥接回调并校验结构化回执）。
 * @param path - Callback route（回调路由）。
 * @param payload - Protocol fields（协议字段）。
 * @param signal - Caller cancellation（调用者取消信号）。
 * @returns Voice receipt（语音端回执对象）。
 */
export async function callback(path: string, payload: Record<string, Json>, signal?: AbortSignal): Promise<Record<string, Json>> {
  const response = await callbackResponse(path, payload, signal)
  const value: unknown = await response.json()
  if (!response.ok || value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw callbackError(response, value)
  }
  return value as Record<string, Json>
}

export type AttachmentConsumer<T> = (body: AttachmentBody, signal?: AbortSignal) => Promise<T>

export async function attachmentCallback<T>(path: string, payload: Record<string, Json>,
  consume: AttachmentConsumer<T>, signal?: AbortSignal): Promise<T> {
  const response = await callbackResponse(path, payload, signal)
  if (!response.ok) throw callbackError(response, await response.json().catch(() => undefined))
  if (response.headers.get('content-type') !== 'application/octet-stream' || !response.body) {
    await response.body?.cancel()
    throw new Error('附件回调缺少二进制响应。')
  }
  const body = new AttachmentBody(response.body.getReader())
  try {
    return await consume(body, signal)
  } finally {
    await body.close()
  }
}

/** CallbackScope（回调生命周期）：卸载时取消并等待所有已发出的回调。 */
export class CallbackScope {
  private readonly controller = new AbortController()
  private readonly pending = new Set<Promise<unknown>>()
  readonly signal = this.controller.signal

  send(path: string, payload: Record<string, Json>, caller?: AbortSignal): Promise<Record<string, Json>> {
    return this.track(callback(path, payload, caller ? AbortSignal.any([this.signal, caller]) : this.signal))
  }

  receiveAttachment<T>(path: string, payload: Record<string, Json>, consume: AttachmentConsumer<T>,
    caller?: AbortSignal): Promise<T> {
    return this.track(attachmentCallback(path, payload, consume,
      caller ? AbortSignal.any([this.signal, caller]) : this.signal))
  }

  private track<T>(request: Promise<T>): Promise<T> {
    this.pending.add(request)
    void request.then(() => this.pending.delete(request), () => this.pending.delete(request))
    return request
  }

  async dispose(): Promise<void> {
    this.controller.abort()
    await Promise.allSettled([...this.pending])
  }
}

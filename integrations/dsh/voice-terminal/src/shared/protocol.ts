import { createHash, timingSafeEqual } from 'node:crypto'
import type { IncomingMessage, ServerResponse } from 'node:http'

export const BRIDGE_PROTOCOL_VERSION = '2.0'
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
type ResultCode = 'accepted' | 'duplicate' | 'stale' | 'resync_required' | 'conflict' | 'not_found' | 'unauthorized'

/** Bridge 2.0 shadow.event 正文（影子事件正文）的严格判别联合。 */
export type ShadowEventBody =
  | { /** 事件类型：仅更新产品界面，不触发语音推理。 */ type: 'progress'; /** 进度文本：去除首尾空白后 1～512 字符。 */ text: string }
  | { /** 事件类型：更新 Voice 信息单槽并触发一次可合并推理。 */ type: 'information'; /** 信息文本：去除首尾空白后 1～2000 字符。 */ text: string }
  | { /** 事件类型：请求用户澄清，是激活终态。 */ type: 'input_required'; /** 问题文本：去除首尾空白后最多 1000 字符。 */ question: string; /** 选项列表：最多 8 项，每项最多 200 字符。 */ options: string[] }
  | { /** 事件类型：返回自包含最终结果，是激活终态。 */ type: 'result'; /** 最终结果文本：包含完整后续事实，最多 8000 字符。 */ text: string }
  | { /** 事件类型：确认无需动作，是无语义历史的激活终态。 */ type: 'no_action'; /** 内部原因：可选，不作为 Voice 语义内容，最多 500 字符。 */ reason?: string }
  | { /** 事件类型：仅 Bridge 内部生成的基础设施失败终态。 */ type: 'failed'; /** 失败说明：最多 2000 字符。 */ message: string; /** 失败代码：最多 128 字符。 */ code: string; /** 基础设施标记：说明是否为基础设施故障。 */ infrastructure: boolean }

export interface Envelope {
  /** 协议版本：固定为 2.0。 */
  protocol_version: string
  /** 请求幂等标识：同一操作重试时保持不变。 */
  request_id: string
  /** Voice 会话标识：绑定的语音主会话。 */
  voice_session_id: string
  /** DSH 会话标识：绑定的影子执行会话。 */
  dsh_session_id: string
  /** 会话代次：阻止旧会话事件进入新上下文。 */
  epoch: number
  /** 上下文修订号：事件所依据的权威上下文版本。 */
  context_revision?: number
  /** 激活标识：仅标记当前有效的影子任务激活。 */
  activation_id?: string
  [key: string]: unknown
}

import { TOKEN } from './environment.js'
export { TOKEN } from './environment.js'
export function json(res: ServerResponse, status: number, body: Json): void {
  const encoded = JSON.stringify(body)
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(encoded)
}

export function result(code: ResultCode, extra: Record<string, Json> = {}): Json {
  return { protocol_version: BRIDGE_PROTOCOL_VERSION, result: code, ...extra }
}

/**
 * Whether the Voice Server committed a delivered event. `accepted` is the first
 * delivery; `duplicate` acknowledges a replay whose `request_id` was already
 * stored, and the server answers both with HTTP 200.
 * @param response - parsed callback body.
 * @returns whether the Voice mailbox holds the event.
 */
export function isDelivered(response: Json): boolean {
  if (response === null || typeof response !== 'object' || Array.isArray(response)) return false
  const code = (response as { result?: unknown }).result
  return code === 'accepted' || code === 'duplicate'
}

export function authorized(req: IncomingMessage): boolean {
  if (!TOKEN || req.socket.remoteAddress === undefined) return false
  const remote = req.socket.remoteAddress.replace(/^::ffff:/, '')
  if (remote !== '127.0.0.1' && remote !== '::1') return false
  const actual = req.headers.authorization ?? ''
  const expected = `Bearer ${TOKEN}`
  const a = Buffer.from(actual)
  const b = Buffer.from(expected)
  return a.length === b.length && timingSafeEqual(a, b)
}

export async function readBody(req: IncomingMessage, maxBytes: number): Promise<Record<string, unknown>> {
  if ((req.headers['content-type'] ?? '').split(';', 1)[0]?.trim().toLowerCase() !== 'application/json') {
    throw new Error('content-type')
  }
  const chunks: Buffer[] = []
  let size = 0
  for await (const chunk of req) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    size += bytes.length
    if (size > maxBytes) throw new Error('body-too-large')
    chunks.push(bytes)
  }
  const value: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('body')
  return value as Record<string, unknown>
}

export function digest(value: unknown): string {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex')
}

export function redactSensitive(value: string): string {
  return value
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+/gi, 'Bearer [REDACTED]')
    .replace(/\b(authorization|api[_-]?key|cookie|bridge[_-]?token|launch[_-]?token)\b\s*[:=]\s*[^\s,;]+/gi, '$1=[REDACTED]')
}

export function assertEnvelope(body: Record<string, unknown>): asserts body is Envelope {
  if (body.protocol_version !== BRIDGE_PROTOCOL_VERSION
    || typeof body.request_id !== 'string' || body.request_id.length < 8
    || typeof body.voice_session_id !== 'string' || !body.voice_session_id
    || typeof body.dsh_session_id !== 'string' || !body.dsh_session_id
    || !Number.isSafeInteger(body.epoch) || Number(body.epoch) < 1) {
    throw new Error('envelope')
  }
}

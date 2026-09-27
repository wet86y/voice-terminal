import { createHash, timingSafeEqual } from 'node:crypto'
import type { IncomingMessage, ServerResponse } from 'node:http'

export const BRIDGE_PROTOCOL_VERSION = '1.0'
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
type ResultCode = 'accepted' | 'duplicate' | 'stale' | 'resync_required' | 'conflict' | 'not_found' | 'unauthorized'

export interface Envelope {
  protocol_version: string
  request_id: string
  voice_session_id: string
  dsh_session_id: string
  epoch: number
  context_revision?: number
  activation_id?: string
  [key: string]: unknown
}

export const TOKEN = process.env.VOICE_DSH_BRIDGE_TOKEN ?? ''
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

import type { Context } from '../shared/services.js'
import { record } from '../shared/json-record.js'
/** Observer component（执行观察组件）：分页归档、有界执行摘要与模型用量。 */
import type { Json } from '../shared/protocol.js'
import { redactSensitive } from '../shared/protocol.js'
import type { Binding } from './contracts.js'
/** session export（会话完整导出）；offset（起点）、limit（页大小）、total（记录上界）。 */
export function exportSessionPage(binding: Pick<Binding, 'handle' | 'exportSnapshot'>, body: Record<string, unknown>): Record<string, Json> {
  const id = body.export_id
  if (typeof id !== 'string' || !id || id.length > 128) throw new Error('invalid export_id（导出标识）')
  if (binding.exportSnapshot?.id !== id) {
    binding.exportSnapshot = { id, records: JSON.parse(JSON.stringify(binding.handle!.agent.session.snapshotEvents())) }
  }
  const events = binding.exportSnapshot.records
  const offset = Number(body.offset ?? 0), limit = Number(body.limit ?? 100)
  const total = body.total == null ? events.length : Number(body.total)
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit)
      || limit < 1 || limit > 500 || !Number.isSafeInteger(total)
      || total < offset || total > events.length) throw new Error('invalid export range（导出范围）')
  const end = Math.min(offset + limit, total)
  return { records: events.slice(offset, end) as Json, export_id: id,
           total, next_offset: end < total ? end : null }
}


/** readSessionTelemetry（会话用量）：仅导出插件获得的上下文词元和供应商缓存计数。 */
export function readSessionTelemetry(binding: Pick<Binding, 'handle'>, meter?: Context['tokenMeter']): Record<string, Json> {
  const session = binding.handle!.agent.session
  const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
  const events = session.snapshotEvents()
  let usage: Record<string, unknown> | undefined
  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index]
    if (!event) continue
    if (!['assistant/message', 'assistant/attempt'].includes(event.type)) continue
    // stream（持久流记录）的 usage（供应商用量）以原始 chunk（数据块）保存。
    const data = record(event.data)
    const stream = Array.isArray(data.stream) ? data.stream : []
    const chunk = record(stream.findLast(value => record(value).type === 'chunk' && record(record(value).chunk).type === 'usage'))
    const sample = data.usage ?? record(chunk.chunk).usage
    usage = sample === undefined ? undefined : record(sample)
    if (usage) break
  }
  const telemetry: Record<string, Json> = {}
  // DSH 的 inputTokens（未缓存输入词元）与 cacheReadTokens（缓存读取词元）分开计数。
  if (usage && count(usage.inputTokens)) {
    const cached = count(usage.cacheReadTokens) ? usage.cacheReadTokens : undefined
    const written = count(usage.cacheWriteTokens) ? usage.cacheWriteTokens : 0
    const total = usage.inputTokens + (cached ?? 0) + written
    if (count(total)) {
      telemetry.provider_input_tokens = total // provider_input_tokens（最近请求的全部输入词元）。
      telemetry.context_token_estimate = total // context_token_estimate（供应商测得的最近上下文用量）。
      if (cached !== undefined) {
        telemetry.provider_cached_tokens = cached // provider_cached_tokens（最近请求的缓存命中词元）。
        if (total > 0) telemetry.provider_cache_hit_ratio = cached / total // provider_cache_hit_ratio（缓存命中比例）。
      }
    }
  }
  try {
    const measurement = meter?.measure(session)
    if (count(measurement?.totalTokens)) telemetry.context_token_estimate = measurement.totalTokens
  } catch { /* 用量采样失败不能影响代理执行，继续使用最近供应商样本。 */ }
  const limit = session.requestContext?.()?.contextWindow
  if (count(limit) && limit > 0) {
    telemetry.context_limit_tokens = limit // context_limit_tokens（插件当前模型的上下文容量）。
    const used = telemetry.context_token_estimate
    if (typeof used === 'number') telemetry.context_usage_percent = used / limit * 100 // context_usage_percent（上下文占用百分比）。
  }
  return telemetry
}
export function observerData(event: { type: string; data: unknown }, binding?: Binding): Json {
  const type = event.type
  const data = record(event.data)
  const message = record(data.message)
  if (type === 'assistant/message') {
    const content = message.content ?? data.content
    const text = Array.isArray(content)
      ? content.map(record).filter(item => item.type === 'text' || item.type === 'output_text').map(item => String(item.text ?? '')).join('\n')
      : typeof content === 'string' ? content : ''
    return { committed: true, text: redactSensitive(text).slice(0, 4096) }
  }
  if (type === 'tool/call') return { name: String(data.name ?? ''), phase: 'started' }
  if (type === 'tool/result') {
    const block = record(Array.isArray(message.content) ? message.content[0] : undefined)
    const callId = String(block?.toolCallId ?? record(message.source).callId ?? '')
    const name = binding?.pendingTools.get(callId) ?? ''
    const summary = block?.content === undefined ? '' : JSON.stringify(block.content)
    return { name, phase: 'finished', summary: redactSensitive(summary).slice(0, 2048) }
  }
  if (type === 'turn/end') {
    const reason = record(data.reason)
    const kind = typeof reason?.kind === 'string' ? reason.kind : 'unknown'
    const error = record(reason.error)
    return {
      committed: true,
      reason: {
        kind,
        ...(typeof error?.code === 'string' ? { code: redactSensitive(error.code).slice(0, 128) } : {}),
        ...(typeof error?.message === 'string' ? { message: redactSensitive(error.message).slice(0, 512) } : {}),
      },
    }
  }
  if (['turn/start', 'step/end'].includes(type)) return { committed: true }
  return { committed: true }
}

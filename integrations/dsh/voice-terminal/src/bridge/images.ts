/** Images（图片上下文）：程序加载并收纳附件，模型无需再次调用读图工具。 */
import { createHash, randomUUID } from 'node:crypto'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import type { ImageAttachmentRef, ImageMediaType } from '@deepseek-ai/dsh-attachment'
import type { Json } from '../shared/protocol.js'
import { digest } from '../shared/protocol.js'
import { record } from '../shared/json-record.js'
import { bindingCallback } from './callbacks.js'
import type { Binding } from './contracts.js'
import type { Context } from '../shared/services.js'
import { LlmError, IMAGE_OFFLOAD_REQUIRED_CODE } from '@deepseek-ai/dsh-llm'

/** 最终请求按实际路由检查能力，阻止模型切换后把既有图片静默降为文字占位。 */
export function installImageCapabilityGuard(ctx: Context, binding: Binding): void {
  const ownsImages = (messages: readonly { role: string; source: unknown; content: readonly ContentBlock[] }[]) => messages.some(message => message.content.some(part => part.type === 'image'))
    && (knownFrames(binding).size > 0 || messages.some(message => message.role === 'user'
      && record(record(message.source).voice_context).voice_session_id === binding.voiceSessionId))
  ctx.on('agent/request-error', async ({ agent, failure }, next) => {
    if (failure.code === IMAGE_OFFLOAD_REQUIRED_CODE && ownsImages(agent.session.deriveMessages())) {
      // 截止本次错误，保留原材料；整段上下文仍可走既有正常整理，不能单独卸载图片。
      console.warn('[voice-shadow-bridge] image budget exceeded; visual history retained')
      return undefined
    }
    return next()
  }, { prepend: true })
  ctx.on('llm/stream', async function* (options, next) {
    const owned = knownFrames(binding).size > 0 || options.messages.some(message => message.role === 'user'
      && record(record(message.source).voice_context).voice_session_id === binding.voiceSessionId)
    if (owned && options.messages.some(message => message.content.some(part => part.type === 'image' && part.offloaded !== true))) {
      const info = await ctx.llm.resolveModelInfo(options.provider, options.model, options.signal)
      if (info.inputModalities !== undefined && !info.inputModalities.includes('image')) {
        throw new LlmError('当前 DSH 模型不支持图片，无法执行视觉上下文请求', 'UNSUPPORTED_CONTENT')
      }
    }
    yield* next()
  })
}

/** 全局整理回调未必处在根代理作用域，按实际会话绑定阻止图片单独卸载。 */
export function registerVoiceImageSummaryGuard(ctx: Context, bindings: Map<string, Binding>): void {
  ctx.on('compaction/summary-error', ({ session, error }, next) => {
    const binding = [...bindings.values()].find(item => item.handle?.agent.session === session)
    if (binding && error instanceof LlmError && error.code === IMAGE_OFFLOAD_REQUIRED_CODE
      && session.deriveMessages().some(message => message.content.some(part => part.type === 'image'))) return false
    return next()
  }, { prepend: true })
}

/** frameKey（消息身份）：修订号标识观察事件，摘要只用于校验及字节去重。 */
export const frameKey = (binding: Binding, frame: Json) => {
  const value = record(frame), messageId = record(value.content).message_id
  return value.kind === 'observation' && typeof messageId === 'string'
    ? `${binding.epoch}:observation:${messageId}` : `${binding.epoch}:${value.revision}`
}
export function knownFrames(binding: Binding): Map<string, string> {
  if (binding.contextFrames) return binding.contextFrames
  const seen = binding.restoreContextFrames?.() ?? new Map<string, string>()
  for (const message of binding.handle?.agent.session?.deriveMessages?.() ?? []) {
    if (message.role !== 'user') continue
    const source = record(message.source)
    const marker = record(source.voice_context)
    if (marker.epoch !== binding.epoch) continue
    for (const [key, hash] of Object.entries(record(marker.frames))) if (typeof hash === 'string') seen.set(key, hash)
  }
  binding.contextFrames = seen
  return seen
}

export function freshFrames(binding: Binding, frames: Json[], allowPending = false): Json[] {
  const seen = knownFrames(binding)
  const queued = new Set(allowPending ? binding.pendingContext?.frames.map(f => frameKey(binding, f)) : [])
  return frames.filter(frame => {
    const key = frameKey(binding, frame), hash = digest(frame), prior = seen.get(key)
    if (prior && prior !== hash && Number(record(frame).revision) !== 0) throw new Error('共享历史消息摘要冲突')
    return !prior || prior !== hash || queued.has(key)
  })
}

export async function prepareContextImages(binding: Binding, frames: Json[]): Promise<void> {
  const epoch = binding.epoch
  for (const frame of freshFrames(binding, frames, true)) {
    const value = record(frame), content = record(value.content)
    if (value.kind !== 'observation' || !Array.isArray(content.parts)) continue
    for (const raw of content.parts) {
      const part = record(raw)
      if (part.type !== 'image') continue
      const aid = String(part.attachment_id), hash = String(part.sha256)
      binding.imageAttachments ??= new Map()
      if (binding.imageAttachments.has(aid)) {
        if (binding.imageAttachments.get(aid)!.hash !== hash) throw new Error('图片附件摘要冲突')
        continue
      }
      const sameBytes = [...binding.imageAttachments.values()].find(item => item.hash === hash)
      if (sameBytes) {
        binding.imageAttachments.set(aid, sameBytes)
        continue
      }
      if (!binding.attachments) throw new Error('DSH 图片附件服务未挂载')
      const reply = await bindingCallback(binding, '/api/internal/dsh-bridge/attachment.read', {
        request_id: randomUUID(), voice_session_id: binding.voiceSessionId,
        dsh_session_id: binding.dshSessionId, epoch, attachment_id: aid,
      })
      const image = record(reply.attachment)
      if (reply.result !== 'accepted' || image.sha256 !== hash || image.mime_type !== part.mime_type || typeof image.base64 !== 'string') throw new Error('图片附件读取失败')
      const bytes = Buffer.from(image.base64, 'base64')
      if (bytes.length !== part.bytes || createHash('sha256').update(bytes).digest('hex') !== hash) throw new Error('图片附件字节校验失败')
      const refs = await binding.attachments.saveImages([{ data: bytes, mediaType: String(part.mime_type) as ImageMediaType, name: aid }])
      if (binding.state === 'retired' || binding.epoch !== epoch || binding.callbacks?.signal.aborted) throw new Error('图片所属会话已失效')
      if (!refs[0]) throw new Error('图片附件收纳失败')
      binding.imageAttachments.set(aid, { hash, ref: refs[0] })
    }
  }
}

export function imageContent(binding: Binding, frames: Json[]): ContentBlock[] {
  const result: ContentBlock[] = []
  for (const frame of frames) {
    const value = record(frame), content = record(value.content)
    if (value.kind !== 'observation' || !Array.isArray(content.parts)) {
      result.push({ type: 'text', text: JSON.stringify(frame) }); continue
    }
    for (const raw of content.parts) {
      const part = record(raw)
      if (part.type === 'text') result.push({ type: 'text', text: String(part.text) })
      else if (part.type === 'image') {
        const ref: ImageAttachmentRef | undefined = binding.imageAttachments?.get(String(part.attachment_id))?.ref
        if (!ref) throw new Error('图片尚未完成自动加载')
        result.push({ type: 'image', attachment: ref })
      } else throw new Error('未知共享内容类型')
    }
  }
  return result
}

export function rememberFrames(binding: Binding, frames: Json[]): void {
  const seen = knownFrames(binding)
  for (const frame of frames) seen.set(frameKey(binding, frame), digest(frame))
}

import type { ImageAttachment } from '../types'

/** 图片沿用当前会话连接；缓存仅保存正在显示的内存对象地址。 */
let send: ((conversationId: string, attachmentId: string) => boolean) | undefined
type Pending = { parts: Map<number, string>; total: number; descriptor: ImageAttachment; resolve: (url: string) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> }
const pending = new Map<string, Pending>()
const cache = new Map<string, { url: string; refs: number; promise?: Promise<string> }>()
const keyOf = (a: ImageAttachment) => `${a.conversation_id}\u0000${a.attachment_id}`
const reconnectListeners = new Set<() => void>()
/** onImageReconnect（取图恢复通知）：成功图片保留，失败项重发。 */
export function onImageReconnect(listener: () => void) { reconnectListeners.add(listener); return () => { reconnectListeners.delete(listener) } }
export function resumeImageChannel() { reconnectListeners.forEach(listener => listener()) }
export function suspendImageChannel() {
  for (const [key, row] of pending) { clearTimeout(row.timer); cache.delete(key); row.reject(new Error('图片连接已断开，重连后自动重试')) }
  pending.clear()
}

export function configureImageChannel(sender?: typeof send) { send = sender }
export function clearImageChannel() {
  for (const row of pending.values()) { clearTimeout(row.timer); row.reject(new Error('图片连接已结束')) }
  pending.clear()
  for (const row of cache.values()) if (row.url) URL.revokeObjectURL(row.url)
  cache.clear()
}

export function acquireImage(a: ImageAttachment): Promise<string> {
  if (a.preview_url) return Promise.resolve(a.preview_url)
  const key = keyOf(a)
  const existing = cache.get(key)
  if (existing) { existing.refs++; return existing.url ? Promise.resolve(existing.url) : existing.promise! }
  const row = { url: '', refs: 1, promise: undefined as Promise<string> | undefined }
  cache.set(key, row)
  row.promise = new Promise<string>((resolve, reject) => {
    const fail = (message: string) => { pending.delete(key); cache.delete(key); reject(new Error(message)) }
    const timer = setTimeout(() => fail('图片加载超时，请重试'), 15000)
    pending.set(key, { parts: new Map(), total: 0, descriptor: a, resolve, reject, timer })
    if (!send?.(a.conversation_id, a.attachment_id)) { clearTimeout(timer); fail('图片连接尚未就绪') }
  }).then(url => { row.url = url; return url })
  return row.promise
}

export function releaseImage(a: ImageAttachment) {
  const key = keyOf(a), row = cache.get(key)
  if (!row || --row.refs > 0) return
  if (row.url) URL.revokeObjectURL(row.url)
  cache.delete(key)
  const job = pending.get(key)
  if (job) { clearTimeout(job.timer); pending.delete(key); job.reject(new Error('图片已退出显示范围')) }
}

export async function acceptImageChunk(p: Record<string, unknown>) {
  const key = `${p.conversation_id}\u0000${p.attachment_id}`, job = pending.get(key)
  if (!job) return
  const owner = cache.get(key)
  const fail = (message: string) => { clearTimeout(job.timer); if (pending.get(key) === job) pending.delete(key); if (cache.get(key) === owner) cache.delete(key); job.reject(new Error(message)) }
  if (p.error) { fail(String(p.error)); return }
  if (!Number.isInteger(p.index) || !Number.isInteger(p.total) || Number(p.total) < 1 || Number(p.total) > 1024
    || Number(p.index) < 0 || Number(p.index) >= Number(p.total) || typeof p.data !== 'string' || p.data.length > 65536
    || p.mime_type !== job.descriptor.mime_type || (job.total && job.total !== p.total)) { fail('图片分块格式无效'); return }
  job.total = Number(p.total); job.parts.set(Number(p.index), p.data)
  if (job.parts.size !== job.total) return
  clearTimeout(job.timer); pending.delete(key)
  try {
    const encoded = Array.from({ length: job.total }, (_, i) => job.parts.get(i)!).join('')
    const binary = atob(encoded), bytes = Uint8Array.from(binary, c => c.charCodeAt(0))
    if (bytes.length !== job.descriptor.bytes) throw new Error('图片长度不一致')
    if (globalThis.crypto?.subtle) {
      const hash = await crypto.subtle.digest('SHA-256', bytes)
      const actual = Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('')
      if (actual !== job.descriptor.sha256) throw new Error('图片摘要不一致')
    }
    if (!owner || cache.get(key) !== owner) { job.reject(new Error('图片已退出显示范围')); return }
    // 摘要计算期间可能卸载再重建同一附件；旧请求不能占用新缓存或遗留对象地址。
    owner.url = URL.createObjectURL(new Blob([bytes], { type: job.descriptor.mime_type }))
    job.resolve(owner.url)
  } catch (e) { fail(e instanceof Error ? e.message : '图片解码失败') }
}

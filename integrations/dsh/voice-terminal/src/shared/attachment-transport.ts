/** Attachment transport（回环附件传输）：从读取边界逐段提取原始字节。 */
import { createHash } from 'node:crypto'

const MAX_MANIFEST_BYTES = 16 * 1024 // MAX_MANIFEST_BYTES（截图清单字节上限）。

export class AttachmentBody {
  private pending: Uint8Array = new Uint8Array(0)

  constructor(private readonly reader: ReadableStreamDefaultReader<Uint8Array>) {}

  private async take(limit: number): Promise<Uint8Array> {
    while (this.pending.length === 0) {
      const next = await this.reader.read()
      if (next.done) return this.pending
      this.pending = next.value
    }
    const result = this.pending.subarray(0, limit)
    this.pending = this.pending.subarray(result.length)
    return result
  }

  async readBytes(size: number): Promise<Uint8Array> {
    const output = new Uint8Array(size)
    for (let offset = 0; offset < size;) {
      const chunk = await this.take(size - offset)
      if (chunk.length === 0) throw new Error('附件二进制响应提前结束。')
      output.set(chunk, offset)
      offset += chunk.length
    }
    return output
  }

  /** readManifest（读取清单）：网络字节序的长度仅限定结构化元数据。 */
  async readManifest(): Promise<unknown> {
    const prefix = await this.readBytes(4)
    const size = new DataView(prefix.buffer, prefix.byteOffset, prefix.byteLength).getUint32(0)
    if (size === 0 || size > MAX_MANIFEST_BYTES) throw new Error('截图附件清单长度无效。')
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(await this.readBytes(size))) as unknown
  }

  async expectEnd(): Promise<void> {
    if ((await this.take(1)).length > 0) throw new Error('附件二进制响应超过声明长度。')
  }

  async readVerifiedBytes(size: number, sha256: string): Promise<Uint8Array> {
    const data = await this.readBytes(size)
    if (createHash('sha256').update(data).digest('hex') !== sha256) throw new Error('附件内容摘要不匹配。')
    return data
  }

  /** verifiedStream（核验流）：只在完整长度和摘要匹配时完成入库来源。 */
  async *verifiedStream(size: number, sha256: string): AsyncIterable<Uint8Array> {
    const digest = createHash('sha256')
    for (let remaining = size; remaining > 0;) {
      const chunk = await this.take(remaining)
      if (chunk.length === 0) throw new Error('附件二进制响应提前结束。')
      digest.update(chunk)
      remaining -= chunk.length
      yield chunk
    }
    await this.expectEnd()
    if (digest.digest('hex') !== sha256) throw new Error('附件内容摘要不匹配。')
  }

  async close(): Promise<void> {
    try { await this.reader.cancel() } catch { /* 读取失败或取消后仍须释放读取器。 */ }
    finally { this.reader.releaseLock() }
  }
}

const OPENING = '<signature>' // opening marker（签名块开标签）。
const CLOSING = '</signature>' // closing marker（签名块闭标签）。

type FilterMode = 'opening' | 'signature' | 'newlines' | 'passthrough' | 'finished'

/**
 * LeadingSignatureFilter（前置签名过滤器）：跨流分片隐藏正文前的完整签名块。
 * 普通正文中间的标签会原样保留；结束时未闭合的前置标签及签名内容会丢弃。
 */
export class LeadingSignatureFilter {
  private mode: FilterMode = 'opening' // mode（流处理阶段）：区分开标签、签名、换行和正文。
  private openingBuffer = '' // openingBuffer（开标签缓冲）：保留可能构成开标签的分片。
  private closingBuffer = '' // closingBuffer（闭标签缓冲）：保留签名末尾可能构成闭标签的分片。
  private pendingCarriageReturn = false // pendingCarriageReturn（待定回车）：判断跨分片 CRLF（回车换行）。

  /** push（追加分片）：返回当前新产生的可见文本。 */
  push(text: string): string {
    if (!text || this.mode === 'finished') return ''
    if (this.mode === 'passthrough') return text
    if (this.mode === 'opening') {
      const candidate = this.openingBuffer + text
      this.openingBuffer = ''
      if (candidate.startsWith(OPENING)) {
        this.mode = 'signature'
        return this.consumeSignature(candidate.slice(OPENING.length))
      }
      if (OPENING.startsWith(candidate)) {
        this.openingBuffer = candidate
        return ''
      }
      this.mode = 'passthrough'
      return candidate
    }
    if (this.mode === 'signature') return this.consumeSignature(text)
    return this.consumeLeadingNewlines(text)
  }

  /** finish（结束流）：丢弃未闭合的前置标记，并保留签名块后的孤立回车。 */
  finish(): string {
    if (this.mode === 'finished') return ''
    let trailing = ''
    if (this.mode === 'newlines' && this.pendingCarriageReturn) trailing = '\r'
    this.openingBuffer = ''
    this.closingBuffer = ''
    this.pendingCarriageReturn = false
    this.mode = 'finished'
    return trailing
  }

  private consumeSignature(text: string): string {
    const candidate = this.closingBuffer + text
    const closingIndex = candidate.indexOf(CLOSING)
    if (closingIndex >= 0) {
      this.closingBuffer = ''
      this.mode = 'newlines'
      return this.consumeLeadingNewlines(candidate.slice(closingIndex + CLOSING.length))
    }
    let retained = Math.min(candidate.length, CLOSING.length - 1)
    while (retained > 0 && !CLOSING.startsWith(candidate.slice(-retained))) retained -= 1
    this.closingBuffer = retained ? candidate.slice(-retained) : ''
    return ''
  }

  private consumeLeadingNewlines(text: string): string {
    let offset = 0
    if (this.pendingCarriageReturn) {
      this.pendingCarriageReturn = false
      if (text[0] === '\n') offset = 1
      else {
        this.mode = 'passthrough'
        return `\r${text}`
      }
    }
    while (offset < text.length) {
      if (text[offset] === '\n') {
        offset += 1
        continue
      }
      if (text[offset] === '\r') {
        if (offset + 1 === text.length) {
          this.pendingCarriageReturn = true
          return ''
        }
        if (text[offset + 1] === '\n') {
          offset += 2
          continue
        }
      }
      this.mode = 'passthrough'
      return text.slice(offset)
    }
    return ''
  }
}

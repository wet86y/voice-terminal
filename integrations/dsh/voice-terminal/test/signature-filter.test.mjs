import assert from 'node:assert/strict'
import test from 'node:test'
import { LeadingSignatureFilter } from '../../../../.local/build/dsh/bundles/voice-terminal-test/lib/shared/signature-filter.js'

test('完整前置签名块被隐藏并清理紧随其后的连续换行', () => {
  const filter = new LeadingSignatureFilter()
  assert.equal(filter.push('<signature>private reasoning</signature>\n\n正文'), '正文')
  assert.equal(filter.finish(), '')
})

test('开标签、闭标签和 CRLF（回车换行）均可跨分片识别', () => {
  const filter = new LeadingSignatureFilter()
  assert.equal(filter.push('<sig'), '')
  assert.equal(filter.push('nature>private</sign'), '')
  assert.equal(filter.push('ature>\r'), '')
  assert.equal(filter.push('\n\n正文'), '正文')
  assert.equal(filter.finish(), '')
})

test('正文开始不匹配开标签时原样保留普通文本', () => {
  const filter = new LeadingSignatureFilter()
  assert.equal(filter.push('<sig'), '')
  assert.equal(filter.push('naturex 是普通文本'), '<signaturex 是普通文本')
  assert.equal(filter.finish(), '')
})

test('未闭合的前置开标签或签名块在流结束时丢弃', () => {
  const partialOpening = new LeadingSignatureFilter()
  assert.equal(partialOpening.push('<sign'), '')
  assert.equal(partialOpening.finish(), '')
  const unclosedBlock = new LeadingSignatureFilter()
  assert.equal(unclosedBlock.push('<signature>private</sign'), '')
  assert.equal(unclosedBlock.finish(), '')
})

test('只移除开头签名块，正文中间的标记与后续普通文本保留', () => {
  const filter = new LeadingSignatureFilter()
  assert.equal(filter.push('普通文本 <signature>inline</signature> 保留'), '普通文本 <signature>inline</signature> 保留')
  assert.equal(filter.finish(), '')
})

test('只清理紧随签名块的 LF（换行）及 CRLF，孤立 CR（回车）作为正文保留', () => {
  const filter = new LeadingSignatureFilter()
  assert.equal(filter.push('<signature>x</signature>\n\r\n\r'), '')
  assert.equal(filter.finish(), '\r')
})

test('每个思考流实例独立缓冲开标签', () => {
  const first = new LeadingSignatureFilter()
  const second = new LeadingSignatureFilter()
  assert.equal(first.push('<sig'), '')
  assert.equal(second.push('另一条思考'), '另一条思考')
  assert.equal(first.push('nature>secret</signature>回复'), '回复')
  assert.equal(second.finish(), '')
  assert.equal(first.finish(), '')
})

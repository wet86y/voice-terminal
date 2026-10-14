import assert from 'node:assert/strict'
import test from 'node:test'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { prepareContextImages, freshFrames, installImageCapabilityGuard, frameKey, registerVoiceImageSummaryGuard } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/images.js'
import { enqueueAuthoritativeContext } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/context.js'
import { voiceContextProjection } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/image-history.js'
import { installRootTools } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/tools.js'
const { LlmError } = createRequire(new URL('../../../../../.local/build/dsh/bundles/voice-terminal-test/package.json', import.meta.url))('@deepseek-ai/dsh-llm')

function fixture() {
  const queue = [], history = [], saved = [], reads = []
  const data = Buffer.from([1, 2, 3]), hash = createHash('sha256').update(data).digest('hex')
  const agent = { session: { deriveMessages: () => history },
    inbox: { replace(id, value) { const index = queue.findIndex(m => m.id === id); if (index < 0) return false; queue[index] = value; return true } },
    inject(value) { queue.push(value) }, followup(value) { queue.push(value) } }
  const binding = { state: 'active', voiceSessionId: 'voice', dshSessionId: 'dsh', epoch: 1, handle: { agent },
    queuedMessages: new Map(), records: [], traceRevision: 0,
    callbacks: { signal: new AbortController().signal, async send(path, body) {
      reads.push({ path, body }); return { result: 'accepted', attachment: { base64: data.toString('base64'), sha256: hash, mime_type: 'image/png' } }
    } },
    attachments: { async saveImages(inputs) { saved.push(inputs); return inputs.map(() => ({ attachmentId: `sha256:${hash}`, mediaType: 'image/png', bytes: 3, width: 1, height: 1 })) } } }
  const frame = (revision, capture) => ({ revision, origin: 'external_system', kind: 'observation',
    content: { message_id: capture, conversation_id: 'voice', parts: [{ type: 'text', text: `观察 ${capture}` },
      ...['global', 'local'].map(role => ({ type: 'image', role, attachment_id: `${capture}-${role}`, mime_type: 'image/png', bytes: 3, sha256: hash }))] } })
  const body = (revision, frames, from_revision = 0) => ({ protocol_version: '2.0', request_id: `request-${revision}`, voice_session_id: 'voice', dsh_session_id: 'dsh', epoch: 1, context_revision: revision, from_revision, frames })
  return { binding, queue, history, reads, saved, frame, body }
}

test('程序自动加载双图，首次上下文包含原生图片块且未新增读图决策', async () => {
  const f = fixture(), frames = [f.frame(1, 'capture-a')]
  await prepareContextImages(f.binding, frames)
  enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  assert.equal(f.queue.length, 1)
  assert.deepEqual(f.queue[0].content.slice(1).map(p => p.type), ['text', 'image', 'image'])
  assert.equal(f.reads.length, 1); assert.equal(f.saved.length, 1)
  assert.ok(f.saved.every(batch => batch[0].data.equals(Buffer.from([1, 2, 3]))))
  assert.ok(!JSON.stringify(f.queue).includes('AQID'))
})

test('重复快照替换未领取消息，领取后及冷恢复不重复追加或重新收纳', async () => {
  const f = fixture(), frames = [f.frame(1, 'capture-a')]
  await prepareContextImages(f.binding, frames)
  enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  const id = f.queue[0].id
  await prepareContextImages(f.binding, frames)
  enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  assert.equal(f.queue.length, 1); assert.equal(f.queue[0].id, id)
  f.history.push(f.queue.shift())
  enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  assert.equal(f.queue.length, 0)
  delete f.binding.contextFrames; delete f.binding.pendingContext; delete f.binding.imageAttachments
  await prepareContextImages(f.binding, frames)
  enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  assert.equal(f.queue.length, 0); assert.equal(f.saved.length, 1)
})

test('相同字节的不同观察仍追加独立事件，消息顺序不变', async () => {
  const f = fixture(), first = f.frame(1, 'capture-a'), second = f.frame(2, 'capture-b')
  await prepareContextImages(f.binding, [first]); enqueueAuthoritativeContext(f.binding, f.body(1, [first]), 'snapshot')
  f.history.push(f.queue.shift())
  await prepareContextImages(f.binding, [second]); enqueueAuthoritativeContext(f.binding, f.body(2, [second], 1), 'delta')
  assert.equal(f.queue.length, 1); assert.equal(f.history.length, 1)
  assert.ok(f.queue[0].content.some(p => p.type === 'text' && p.text.includes('capture-b')))
  assert.equal(f.history[0].content.filter(p => p.type === 'image').length, 2)
  assert.equal(f.saved.length, 1) // 同字节只收纳一次，不合并不同的观察事件。
})

test('同一消息身份出现不同内容拒绝，加载失败不会注入半包', async () => {
  const f = fixture(), frames = [f.frame(1, 'capture-a')]
  await prepareContextImages(f.binding, frames); enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  const changed = structuredClone(frames); changed[0].content.parts[0].text = '已被改写'
  assert.throws(() => freshFrames(f.binding, changed), /摘要冲突/)
  const other = fixture(); other.binding.callbacks.send = async () => ({ result: 'accepted', attachment: { base64: 'AQID', sha256: 'bad', mime_type: 'image/png' } })
  await assert.rejects(prepareContextImages(other.binding, [other.frame(1, 'capture-x')]), /读取失败/)
  assert.equal(other.queue.length, 0)
})

test('最终实际模型路由为纯文本时阻止图片请求，视觉路由正常继续', async () => {
  const f = fixture(), frames = [f.frame(1, 'capture-a')]
  await prepareContextImages(f.binding, frames); enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  let guard, modalities = ['text'], selected
  const ctx = { on(event, callback) { if (event === 'llm/stream') guard = callback },
    llm: { async resolveModelInfo(provider, model) { selected = { provider, model }; return { inputModalities: modalities } } } }
  installImageCapabilityGuard(ctx, f.binding)
  let calls = 0
  const next = async function* () { calls++; yield { type: 'text-delta', text: '已理解图片' } }
  const options = { provider: 'final-provider', model: 'changed-route', messages: f.queue }
  await assert.rejects(async () => { for await (const chunk of guard(options, next)) assert.fail(chunk) }, /不支持图片/)
  assert.equal(calls, 0); assert.deepEqual(selected, { provider: 'final-provider', model: 'changed-route' })
  modalities = ['text', 'image']
  const chunks = []; for await (const chunk of guard(options, next)) chunks.push(chunk)
  assert.equal(calls, 1); assert.equal(chunks[0].text, '已理解图片')
})

test('图片预算错误不会进入默认卸载重试，保留已收到的视觉材料', async () => {
  const f = fixture(), handlers = new Map(), frames = [f.frame(1, 'capture-a')]
  await prepareContextImages(f.binding, frames); enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  const ctx = { on(event, callback, options) { handlers.set(event, callback); if (event !== 'llm/stream') assert.equal(options.prepend, true) } }
  installImageCapabilityGuard(ctx, f.binding)
  let delegated = 0
  const agent = { session: { deriveMessages: () => f.queue } }
  const answer = await handlers.get('agent/request-error')({ agent, failure: { code: 'IMAGE_OFFLOAD_REQUIRED', offloadImages: 1 } }, async () => { delegated++; return { kind: 'retry' } })
  assert.equal(answer, undefined); assert.equal(delegated, 0)
  assert.equal(f.queue[0].content.filter(p => p.type === 'image').length, 2)
  await handlers.get('agent/request-error')({ agent, failure: { code: 'TIMEOUT' } }, async () => { delegated++; return undefined })
  assert.equal(delegated, 1)
})

test('持久事件投影在普通整理抹去旧表面后仍阻止冷恢复重复图片', async () => {
  const f = fixture(), frame = f.frame(1, 'capture-a')
  await prepareContextImages(f.binding, [frame]); enqueueAuthoritativeContext(f.binding, f.body(1, [frame]), 'snapshot')
  const message = f.queue.shift(), before = voiceContextProjection.init()
  const persisted = voiceContextProjection.apply(before, { type: 'user/message', data: message })
  assert.equal(persisted['voice:1'][frameKey(f.binding, frame)].length, 64)
  assert.equal(voiceContextProjection.apply(persisted, { type: 'compaction/end', data: {} }), persisted)
  f.binding.restoreContextFrames = () => new Map(Object.entries(persisted['voice:1']))
  delete f.binding.contextFrames; delete f.binding.pendingContext; delete f.binding.imageAttachments
  await prepareContextImages(f.binding, [frame]); enqueueAuthoritativeContext(f.binding, f.body(1, [frame]), 'snapshot')
  assert.equal(f.queue.length, 0); assert.equal(f.saved.length, 1)
  assert.throws(() => freshFrames(f.binding, [{ ...frame, revision: 8 }]), /摘要冲突/) // 身份不能靠重新编号逃过去重。
})

test('正常整理遇到图片预算错误保留完整材料，其他会话沿用宿主处理', async () => {
  const f = fixture(), frames = [f.frame(1, 'capture-a')]
  await prepareContextImages(f.binding, frames); enqueueAuthoritativeContext(f.binding, f.body(1, frames), 'snapshot')
  f.history.push(f.queue.shift())
  let handler, delegated = 0
  registerVoiceImageSummaryGuard({ on(event, callback, options) { assert.equal(event, 'compaction/summary-error'); assert.equal(options.prepend, true); handler = callback } }, new Map([['dsh', f.binding]]))
  const error = new LlmError('图像预算不足', 'IMAGE_OFFLOAD_REQUIRED', { offloadImages: 1 })
  assert.equal(handler({ session: f.binding.handle.agent.session, error }, () => { delegated++; return true }), false)
  assert.equal(delegated, 0)
  assert.equal(handler({ session: { deriveMessages: () => [] }, error }, () => { delegated++; return true }), true)
  assert.equal(delegated, 1)
})

test('最新上下文工具自动返回原生图片，持久工具元数据恢复身份', async () => {
  const f = fixture(), tools = new Map(), frame = f.frame(1, 'tool-capture')
  f.binding.activationId = 'activation'; f.binding.handle.agent.id = 'agent'
  const original = f.binding.callbacks.send
  f.binding.callbacks.send = async (path, body) => path.endsWith('shadow.context.read') ? { result: 'accepted', epoch: 1, frames: [frame] } : original(path, body)
  installRootTools({ tools: { register(definition) { tools.set(definition.name, definition) } } }, f.binding)
  const tool = tools.get('get_latest_context'), args = { since_revision: 0 }
  const value = await tool.execute(args, { agent: { id: 'agent' }, signal: new AbortController().signal })
  const content = tool.output.render(args, value)
  assert.equal(content.filter(p => p.type === 'image').length, 2)
  assert.ok(!JSON.stringify(content).includes('AQID'))
  const persisted = voiceContextProjection.apply({}, { type: 'tool/result', data: { message: { isError: false }, meta: tool.output.presentationMeta(args, value) } })
  assert.ok(persisted['voice:1'][frameKey(f.binding, frame)])
  f.binding.restoreContextFrames = () => new Map(Object.entries(persisted['voice:1']))
  delete f.binding.contextFrames; delete f.binding.imageAttachments
  assert.deepEqual(freshFrames(f.binding, [frame]), [])
})

import assert from 'node:assert/strict'
import test from 'node:test'
import { registerSessionEvents, stopRuntimeActivity } from '../../../../../.runtime/dsh/bundles/voice-terminal-test/lib/bridge/events.js'
import { clearActivationIfCurrent } from '../../../../../.runtime/dsh/bundles/voice-terminal-test/lib/bridge/messages.js'

function fixture(t) {
  const listeners = new Map()
  const received = []
  const session = { id: 'dsh-root', snapshotEvents: () => [], requestContext: () => ({}) }
  const agent = { id: session.id, session }
  const controller = new AbortController()
  const binding = {
    voiceSessionId: 'voice-test', dshSessionId: session.id, epoch: 1, revision: 2,
    state: 'active', activationId: 'activation-a', handle: { agent },
    records: [], traceRevision: 0, agentState: 'running', committedSteps: 0,
    pendingTools: new Map(), pendingToolActivations: new Map(),
    actionSeq: 0, actionQueue: Promise.resolve(), assistantStreamRevision: 0,
    pendingThinkingDelta: '', queuedMessages: new Map(),
    callbacks: { signal: controller.signal, async send(path, payload) {
      received.push({ path, payload }); return { result: 'accepted' }
    } },
  }
  registerSessionEvents({ on(name, listener) { listeners.set(name, listener) }, get() {} }, new Map([[agent.id, binding]]))
  t.after(async () => { stopRuntimeActivity(binding); controller.abort(); await binding.actionQueue })
  let revision = 0
  const frame = (attemptId, value, owner = agent) => listeners.get('agent/assistant-stream')({ agent: owner, frame: { attemptId, revision: ++revision, ...value } })
  const start = (id = 'attempt-a') => frame(id, { type: 'start', turn: 1, step: 0 })
  const delta = (text, index = 0, id = 'attempt-a') => frame(id, { type: 'chunk', index: revision, time: Date.now(), chunk: { type: 'reasoning-delta', index, text } })
  const end = (id = 'attempt-a') => frame(id, { type: 'end', index: revision, outcome: { kind: 'abandoned' } })
  const event = (type, data, owner = session) => listeners.get('session/event')(owner, { type, data })
  const status = value => listeners.get('agent/status')({ agent, status: value })
  const callbacks = () => received.filter(value => value.path.endsWith('/shadow.activity')).map(value => value.payload)
  return { binding, received, frame, start, delta, end, event, status, callbacks, agent, controller }
}

test('根代理思考保留空白并累加，新块与工具按同一动作序号发送', async t => {
  const f = fixture(t)
  f.start(); f.delta('正在'); f.delta(' 思考\n')
  f.frame('attempt-a', { type: 'chunk', chunk: { type: 'text-delta', index: 1, text: '助手正文不进入单槽' } })
  f.delta('新思考块', 2); f.end()
  f.event('assistant/message', { content: [{ type: 'text', text: '助手完整消息' }] })
  f.event('tool/call', { callId: 'tool-a', name: 'read_file', arguments: '私有调用参数' })
  f.event('tool/result', { message: { content: [{ toolCallId: 'tool-a', isError: true, content: ['私有结果正文'] }] } })
  f.status('idle')
  await f.binding.actionQueue
  const rows = f.callbacks()
  assert.deepEqual(rows.map(value => value.action_seq), [1, 2, 3, 4, 5, 6])
  assert.deepEqual(rows.filter(value => value.activity?.delta).map(value => value.activity.delta), ['正在 思考\n', '新思考块'])
  assert.deepEqual(rows[3].activity, { stream: 'tool', phase: 'started', item_id: 'tool-tool-a', tool_name: 'read_file', tool_status: 'running' })
  assert.deepEqual(rows[4].activity, { stream: 'tool', phase: 'failed', item_id: 'tool-tool-a', tool_name: 'read_file', tool_status: 'failed' })
  assert.equal(rows[5].state, 'idle')
  assert.equal(JSON.stringify(rows).includes('私有'), false)
  assert.equal(JSON.stringify(rows).includes('助手'), false)
})

test('旧修订帧和旧尝试结束不能刷新或替换新思考块', async t => {
  const f = fixture(t)
  f.start(); f.delta('旧思考')
  f.start('attempt-b'); f.delta('新', 0, 'attempt-b')
  f.end('attempt-a')
  assert.equal(f.binding.pendingThinkingDelta, '新')
  f.delta('迟到旧思考', 0, 'attempt-a')
  f.frame('attempt-b', { type: 'chunk', revision: 2, chunk: { type: 'reasoning-delta', index: 0, text: '乱序' } })
  f.delta('思考', 0, 'attempt-b'); f.end('attempt-b')
  await f.binding.actionQueue
  assert.deepEqual(f.callbacks().filter(value => value.activity?.delta).map(value => value.activity.delta), ['旧思考', '新思考'])
})

test('激活替换丢弃旧缓冲与排队回调，旧工具结果不能进入新激活', async t => {
  const f = fixture(t)
  let release
  f.binding.actionQueue = new Promise(resolve => { release = resolve })
  f.start(); f.delta('尚未发送的旧思考')
  f.event('tool/call', { callId: 'old-tool', name: 'old_name' })
  stopRuntimeActivity(f.binding)
  f.binding.activationId = 'activation-b'; f.binding.actionSeq = 0
  f.delta('旧尝试在新激活内迟到')
  f.event('tool/result', { message: { source: { callId: 'old-tool' } } })
  f.start('attempt-b'); f.delta('当前思考', 0, 'attempt-b'); f.end('attempt-b')
  release(); await f.binding.actionQueue
  assert.deepEqual(f.callbacks().map(value => value.activation_id), ['activation-b', 'activation-b'])
  assert.deepEqual(f.callbacks().map(value => value.action_seq), [1, 2])
  assert.equal(f.callbacks()[0].activity.delta, '当前思考')
})

test('相同标识的其他代理或会话对象没有实时投影权限', async t => {
  const f = fixture(t)
  f.frame('forged', { type: 'start' }, { ...f.agent })
  f.frame('forged', { type: 'chunk', chunk: { type: 'reasoning-delta', index: 0, text: '错误代理' } }, { ...f.agent })
  f.event('tool/call', { callId: 'forged', name: 'forged' }, { ...f.agent.session })
  await f.binding.actionQueue
  assert.equal(f.callbacks().length, 0)
})

test('终态工具结果仍绑定原激活，空闲回调排在结果之后', async t => {
  const f = fixture(t)
  f.event('tool/call', { callId: 'terminal', name: 'return_shadow_result' })
  assert.equal(clearActivationIfCurrent(f.binding, 'activation-a'), true)
  f.event('tool/result', { message: { source: { callId: 'terminal' } } })
  f.status('idle')
  await f.binding.actionQueue
  assert.deepEqual(f.callbacks().map(value => value.action_seq), [1, 2, 3])
  assert.equal(f.callbacks()[1].activity.tool_status, 'completed')
  assert.equal(f.callbacks()[2].state, 'idle')
  assert.ok(f.callbacks().every(value => value.activation_id === 'activation-a'))
})

test('长思考分片保留 Unicode（统一字符编码）且自动刷新缓冲', async t => {
  const f = fixture(t)
  const text = '思考🙂'.repeat(2000)
  f.start(); f.delta(text); f.end()
  await f.binding.actionQueue
  const pieces = f.callbacks().filter(value => value.activity?.delta).map(value => value.activity.delta)
  assert.equal(pieces.join(''), text)
  assert.ok(pieces.every(value => Array.from(value).length <= 1800))
  f.start('attempt-b'); f.delta('短思考', 0, 'attempt-b')
  await new Promise(resolve => setTimeout(resolve, 100))
  await f.binding.actionQueue
  assert.equal(f.callbacks().at(-1).activity.delta, '短思考')
  assert.equal(f.binding.thinkingTimer, undefined)
})

test('释放绑定后停止定时器，排队工作与后续事件不再发送', async t => {
  const f = fixture(t)
  let release
  f.binding.actionQueue = new Promise(resolve => { release = resolve })
  f.start(); f.delta('待清理')
  assert.ok(f.binding.thinkingTimer)
  f.event('tool/call', { callId: 'queued', name: 'queued_tool' })
  f.delta('已清理尝试')
  stopRuntimeActivity(f.binding); f.binding.state = 'retired'; f.controller.abort()
  release(); await f.binding.actionQueue
  f.start(); f.delta('卸载后'); f.event('tool/call', { callId: 'late', name: 'late' }); f.status('idle')
  await f.binding.actionQueue
  assert.equal(f.received.length, 0)
  assert.equal(f.binding.thinkingTimer, undefined)
  assert.equal(f.binding.pendingThinkingDelta, '')
})

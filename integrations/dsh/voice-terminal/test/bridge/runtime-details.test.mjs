import assert from 'node:assert/strict'
import test from 'node:test'
import { registerExtendedActivity } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/runtime-details.js'
import { stopRuntimeActivity } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/events.js'

function fixture(t) {
  const listeners = new Map(), received = [], reads = []
  const root = { id: 'root', session: { id: 'root', header: {} } }
  const child = { id: 'child', session: { id: 'child', header: { parentSession: 'root' } } }
  const unrelated = { id: 'other', session: { id: 'other', header: {} } }
  const binding = {
    voiceSessionId: 'voice', dshSessionId: 'root', epoch: 1, revision: 2,
    state: 'active', activationId: 'run-a', handle: { agent: root },
    actionSeq: 0, actionQueue: Promise.resolve(), pendingTools: new Map(), pendingToolActivations: new Map(),
    callbacks: { signal: new AbortController().signal, async send(path, payload) { received.push(payload); return { result: 'accepted' } } },
  }
  let receiveJob, dispose
  let chunks = []
  const ctx = {
    on(name, listener) { listeners.set(name, listener) },
    agents: { list: () => [root, child, unrelated], isOwnedBy: (id, parent) => id === 'child' && parent === root },
    jobs: {
      events: { subscribe(_options, callback) { receiveJob = callback; return () => {} } },
      readAt(id, offset, owner) {
        reads.push({ id, offset, owner })
        return { chunks: chunks.filter(chunk => chunk.at >= offset), next: chunks.reduce((sum, chunk) => sum + chunk.text.length, 0), lossy: false }
      },
    },
    effect(callback) { dispose = callback() },
  }
  registerExtendedActivity(ctx, new Map([['root', binding]]))
  t.after(async () => { dispose(); stopRuntimeActivity(binding); await binding.actionQueue })
  const stream = (agent, frame) => listeners.get('agent/assistant-stream')({ agent, frame })
  const approval = (agent, extra = {}) => listeners.get('approval/request')({ agent, toolName: 'bash', callId: 'call', reason: '需要执行命令', ...extra }, () => Promise.resolve('fallback'))
  return { binding, root, child, unrelated, stream, approval, receiveJob: event => receiveJob(event), received, reads,
    chunks: value => { chunks = value }, event: (agent, type, data) => listeners.get('session/event')(agent.session, { type, data }) }
}

test('所属子代理的正文与思考完成后结算，迟到尝试与无关会话不进入轨迹', async t => {
  const f = fixture(t)
  f.stream(f.child, { type: 'start', revision: 1, attemptId: 'a' })
  f.stream(f.child, { type: 'chunk', revision: 2, attemptId: 'a', chunk: { type: 'reasoning-delta', index: 0, text: '子代理分析' } })
  f.stream(f.child, { type: 'chunk', revision: 3, attemptId: 'a', chunk: { type: 'text-delta', index: 1, text: '子代理结果' } })
  f.stream(f.child, { type: 'chunk', revision: 2, attemptId: 'a', chunk: { type: 'text-delta', index: 1, text: '乱序' } })
  f.stream(f.child, { type: 'end', revision: 4, attemptId: 'a' })
  f.stream(f.child, { type: 'chunk', revision: 5, attemptId: 'a', chunk: { type: 'text-delta', index: 1, text: '结束后迟到' } })
  f.stream(f.unrelated, { type: 'start', revision: 1, attemptId: 'b' })
  f.stream(f.unrelated, { type: 'chunk', revision: 2, attemptId: 'b', chunk: { type: 'text-delta', index: 0, text: '无关' } })
  await f.binding.actionQueue
  assert.deepEqual(f.received.map(item => item.activity.phase), ['delta', 'delta', 'completed', 'completed'])
  assert.equal(JSON.stringify(f.received).includes('子代理结果'), true)
  assert.equal(JSON.stringify(f.received).includes('乱序'), false)
})

test('子代理工具的参数和结果可展开且脱敏，参数缓存随结果清理', async t => {
  const f = fixture(t)
  f.event(f.child, 'tool/call', { callId: 'call', name: 'bash', arguments: { command: 'echo demo', password: 'private-password' } })
  f.event(f.child, 'tool/result', { message: { source: { callId: 'call' }, content: [{ type: 'text', text: 'demo' }] } })
  await f.binding.actionQueue
  assert.equal(f.received.length, 2)
  assert.ok(f.received[1].activity.text.includes('echo demo'))
  assert.equal(JSON.stringify(f.received).includes('private-password'), false)
  assert.equal(f.binding.toolArguments.size, 0)
  assert.equal(f.binding.pendingTools.size, 0)
})

test('授权卡片答复原请求，无关权限交给宿主，取消与运行停止均释放等待', async t => {
  const f = fixture(t)
  assert.equal(await f.approval(f.unrelated), 'fallback')
  const allowed = f.approval(f.root)
  await f.binding.actionQueue
  const id = f.received.at(-1).activity.approval_id
  f.binding.pendingApprovals.get(id).settle('allowed-once')
  assert.equal(await allowed, 'allowed-once')
  const rejected = f.approval(f.child)
  const next = [...f.binding.pendingApprovals.values()][0]
  next.settle('rejected')
  assert.equal(await rejected, 'rejected')
  const abort = new AbortController()
  const cancelled = f.approval(f.root, { signal: abort.signal })
  abort.abort()
  assert.equal(await cancelled, 'cancelled')
  const stopped = f.approval(f.root)
  stopRuntimeActivity(f.binding)
  assert.equal(await stopped, 'cancelled')
  assert.equal(f.binding.pendingApprovals.size, 0)
})

test('命令作业映射实时标准和错误输出，使用独立游标并在结束时结算', async t => {
  const f = fixture(t)
  const job = { id: 'bash-1', owner: 'root', label: 'echo demo', status: 'running' }
  f.chunks([{ at: 0, text: 'demo\n', channel: 'stdout' }])
  f.receiveJob({ type: 'registered', job })
  f.chunks([{ at: 0, text: 'demo\n', channel: 'stdout' }, { at: 5, text: 'warning\n', channel: 'stderr' }])
  f.receiveJob({ type: 'output', id: job.id })
  delete f.binding.activationId
  f.binding.terminalActivationId = 'run-a'
  f.receiveJob({ type: 'settled', job: { ...job, status: 'completed', detail: 'exit code: 0' } })
  await f.binding.actionQueue
  assert.deepEqual(f.reads.map(item => item.offset), [0, 5, 13])
  assert.equal(f.received[0].activity.phase, 'started')
  assert.ok(f.received.some(item => item.activity.delta?.includes('[错误输出] warning')))
  assert.equal(f.received.at(-1).activity.phase, 'completed')
  assert.ok(f.received.every(item => item.activation_id === 'run-a'))
})

test('旧作业不能因后续进度重新归属新运行', async t => {
  const f = fixture(t)
  const job = { id: 'bash-1', owner: 'root', label: 'old command', status: 'running' }
  f.receiveJob({ type: 'registered', job })
  await f.binding.actionQueue
  const count = f.received.length
  f.binding.activationId = 'run-b'
  f.receiveJob({ type: 'progress', job: { ...job, progress: 'old progress' } })
  f.receiveJob({ type: 'settled', job: { ...job, status: 'completed' } })
  await f.binding.actionQueue
  assert.equal(f.received.length, count)
})

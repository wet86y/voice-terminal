import assert from 'node:assert/strict'
import test from 'node:test'
import { randomUUID } from 'node:crypto'

process.env.VOICE_DSH_BRIDGE_TOKEN = 'isolated-request-lifecycle'
const build = '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib'
const { createBridgeRoute } = await import(`${build}/bridge/http.js`)
const { TOKEN } = await import(`${build}/shared/protocol.js`)
const { taskDeliveryProjection } = await import(`${build}/bridge/task-delivery.js`)

function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function fixture() {
  const bindings = new Map(), retired = new Map(), followups = [], events = []
  let createGate, flushGate, creates = 0
  const handle = { agent: { session: { id: 'dsh', snapshotEvents: () => events }, status: 'idle',
    inject() {}, followup(message) {
      followups.push(message)
      events.push({ type: 'agent/inbox/spliced', data: { inserted: [message] } })
    }, cancel() {}, whenIdle: async () => {},
    inbox: { replace: () => false, remove: () => false } } }
  const workspace = { id: 'workspace', path: process.cwd(), status: async () => 'ok', attachSession: async () => {} }
  const ctx = {
    sessionProjections: { stateOf: () => events.reduce((state, event) => taskDeliveryProjection.apply(state, event), taskDeliveryProjection.init()) },
    agentPresets: { resolve: async () => ({ id: 'shadow-agent' }) },
    agentDefaultModel: { currentSelection: () => ({ provider: 'test', model: 'test' }) },
    agents: { create: async () => { creates++; if (createGate) await createGate; return handle } },
    workspaceRegistry: { resolveByPath: async () => workspace },
    sessionTitle: { rename() {} }, sessions: { flush: async () => { if (flushGate) await flushGate; return true } },
    get: () => undefined,
  }
  const route = createBridgeRoute(ctx, { routePrefix: '/bridge', maxBodyBytes: 1000000 }, bindings,
    Promise.resolve({ settings: { get: () => ({}), update: async () => {} } }), () => undefined, retired)
  const envelope = extra => ({ protocol_version: '2.0', request_id: randomUUID(),
    voice_session_id: 'voice', dsh_session_id: 'dsh', epoch: 1, ...extra })
  async function call(path, body = envelope()) {
    const request = { method: 'POST', url: `/bridge/${path}`, headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' },
      socket: { remoteAddress: '127.0.0.1' }, async *[Symbol.asyncIterator]() { yield Buffer.from(JSON.stringify(body)) } }
    const response = { statusCode: 200, setHeader() {}, end(value) { this.body = JSON.parse(value) } }
    await route.handler(request, response)
    return response
  }
  return { bindings, retired, followups, events, handle, envelope, call,
    createGate(value) { createGate = value }, flushGate(value) { flushGate = value }, creates: () => creates }
}

test('只读快照不累积响应，导出分页仍以同一导出编号冻结', async () => {
  const f = fixture()
  assert.equal((await f.call('session.open')).body.result, 'accepted')
  const binding = f.bindings.get('dsh'), baseline = binding.requests.size
  f.events.push({ seq: 1, type: 'user/message', data: { text: '原始归档' } })
  const exportBody = f.envelope({ export_id: 'archive', limit: 1 })
  const page = (await f.call('session.export', exportBody)).body
  f.events.push({ seq: 2, type: 'user/message', data: { text: '稍后新增' } })
  assert.deepEqual((await f.call('session.export', exportBody)).body, page)
  for (let index = 0; index < 200; index++) {
    assert.equal((await f.call('session.state')).body.result, 'accepted')
    assert.equal((await f.call('observer.snapshot', f.envelope({ trace_revision: 0 }))).body.result, 'accepted')
  }
  assert.equal(binding.requests.size, baseline)
})

test('同编号并发投递只执行一次，异正文重放仍冲突', async () => {
  const f = fixture()
  await f.call('session.open')
  const gate = deferred()
  f.flushGate(gate.promise)
  const body = f.envelope({ task_id: 'task', occurrence_id: 'occurrence', text: '任务正文' })
  const first = f.call('task.deliver', body), second = f.call('task.deliver', body)
  for (let tick = 0; tick < 12; tick++) await Promise.resolve()
  assert.equal(f.followups.length, 1)
  gate.resolve()
  assert.equal((await first).body.result, 'accepted')
  assert.equal((await second).body.result, 'duplicate')
  assert.equal((await f.call('task.deliver', { ...body, text: '不同正文' })).body.result, 'conflict')
  assert.equal(f.followups.length, 1)
})

test('同一会话并发打开不会创建两个官方执行实例', async () => {
  const f = fixture(), gate = deferred()
  f.createGate(gate.promise)
  const first = f.call('session.open')
  for (let tick = 0; tick < 16; tick++) await Promise.resolve()
  const other = await f.call('session.open')
  assert.equal(other.body.result, 'conflict')
  gate.resolve()
  assert.equal((await first).body.result, 'accepted')
  assert.equal(f.creates(), 1)
})

test('副作用已提交后持久化失败，并发与串行重试均不重复投递', async () => {
  const f = fixture()
  await f.call('session.open')
  const gate = deferred()
  f.flushGate(gate.promise)
  const body = f.envelope({ task_id: 'task', occurrence_id: 'occurrence', text: '只投递一次' })
  const first = f.call('task.deliver', body), second = f.call('task.deliver', body)
  for (let tick = 0; tick < 12; tick++) await Promise.resolve()
  assert.equal(f.followups.length, 1)
  gate.reject(new Error('持久化确认失败'))
  const responses = await Promise.all([first, second])
  assert.equal(responses[0].statusCode, 400)
  assert.deepEqual(responses[0].body, responses[1].body)
  assert.equal(f.followups.length, 1)
  assert.equal((await f.call('task.deliver', body)).statusCode, 400)
  assert.equal(f.followups.length, 1)
  f.flushGate(undefined)
  const retried = await f.call('task.deliver', body)
  assert.equal(retried.body.result, 'duplicate')
  assert.equal(retried.body.original.result, 'accepted')
  assert.equal(f.bindings.get('dsh').requests.get(body.request_id).flushPending, false)
  assert.equal(f.followups.length, 1)
})

test('退役开始即停止新工作准入，收尾失败后可重试完成', async () => {
  const f = fixture(), idle = deferred(), flush = deferred()
  await f.call('session.open')
  f.handle.agent.whenIdle = () => idle.promise
  f.flushGate(flush.promise)
  const body = f.envelope(), retiring = f.call('session.retire', body)
  for (let tick = 0; tick < 12; tick++) await Promise.resolve()
  assert.equal(f.bindings.get('dsh').state, 'retired')
  assert.equal((await f.call('wake', f.envelope({ context_revision: 0 }))).body.result, 'stale')
  assert.equal((await f.call('task.deliver', f.envelope({ task_id: 'task', occurrence_id: 'occurrence', text: '过期投递' }))).body.result, 'stale')
  assert.equal((await f.call('session.retire')).body.result, 'conflict')
  assert.equal(f.followups.length, 0)
  idle.resolve()
  flush.reject(new Error('退役落盘失败'))
  assert.equal((await retiring).statusCode, 400)
  assert.equal(f.bindings.size, 1)
  assert.equal(f.retired.size, 0)
  f.flushGate(undefined)
  assert.equal((await f.call('session.retire', body)).body.result, 'accepted')
  assert.equal(f.bindings.size, 0)
  assert.equal(f.retired.size, 1)
})

test('会话恢复后依据官方收件箱与历史身份去重，不重复投递或接受异正文', async () => {
  const f = fixture()
  await f.call('session.open')
  const body = f.envelope({ task_id: 'task', occurrence_id: 'occurrence', text: '已提交的任务' })
  assert.equal((await f.call('task.deliver', body)).body.result, 'accepted')
  const message = f.followups[0]
  for (const event of [
    { type: 'agent/inbox/spliced', data: { inserted: [message] } },
    { type: 'user/message', data: message },
  ]) {
    // 模拟插件重载丢失进程内回执，官方持久日志已恢复。
    f.bindings.get('dsh').requests.clear()
    f.events.splice(0, f.events.length, event)
    assert.equal((await f.call('task.deliver', body)).body.result, 'duplicate')
    assert.equal(f.followups.length, 1)
    f.bindings.get('dsh').requests.clear()
    assert.equal((await f.call('task.deliver', { ...body, text: '同编号的不同正文' })).body.result, 'conflict')
    assert.equal(f.followups.length, 1)
  }
})

test('任务身份投影只保存摘要，取消和历史整理不会抹掉已经提交的身份', () => {
  const empty = taskDeliveryProjection.init()
  const message = { id: 'occurrence', source: { kind: 'schedule' }, content: [{ type: 'text', text: '用户任务正文' }] }
  assert.equal(taskDeliveryProjection.apply(empty, { type: 'user/message', data: { ...message, source: { kind: 'user' } } }), empty)
  const stored = taskDeliveryProjection.apply(empty, { type: 'agent/inbox/spliced', data: { inserted: [message] } })
  assert.deepEqual(empty, {})
  assert.match(stored.occurrence, /^[a-f0-9]{64}$/)
  assert.ok(!JSON.stringify(stored).includes('用户任务正文'))
  for (const event of [
    { type: 'agent/inbox/spliced', data: { inserted: [], removedCount: 1, outcome: 'canceled' } },
    { type: 'user/message', data: message },
    { type: 'compaction/end', data: {} },
  ]) assert.equal(taskDeliveryProjection.apply(stored, event), stored)
  assert.ok(taskDeliveryProjection.stateSchema.safeParse(JSON.parse(JSON.stringify(stored))).success)
})

test('退役只留下身份回执，保留官方历史并拒绝旧会话活动', async () => {
  const f = fixture()
  await f.call('session.open')
  const binding = f.bindings.get('dsh')
  f.events.push({ seq: 1, type: 'user/message', data: { text: '需要保留的官方历史' } })
  binding.records.push({ trace_revision: 1, type: 'test', data: '桥接缓存' })
  await f.call('session.export', f.envelope({ export_id: 'archive' }))
  const body = f.envelope()
  assert.equal((await f.call('session.retire', body)).body.result, 'accepted')
  assert.equal(f.bindings.size, 0)
  assert.equal(f.retired.size, 1)
  assert.equal(binding.requests.size, 0)
  assert.equal(binding.records.length, 0)
  assert.equal(binding.exportSnapshot, undefined)
  assert.equal(f.handle.agent.session.snapshotEvents().length, 1)
  assert.equal((await f.call('session.retire', body)).body.result, 'duplicate')
  assert.equal((await f.call('session.retire')).body.result, 'duplicate')
  assert.equal((await f.call('session.retire', { ...body, context_revision: 99 })).body.result, 'conflict')
  assert.equal((await f.call('session.state')).body.result, 'stale')
  assert.equal((await f.call('session.export', f.envelope({ export_id: 'archive' }))).body.result, 'stale')
})

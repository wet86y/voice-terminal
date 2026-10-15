// session.open 级覆盖：默认关闭运行提醒，仅显式的正整数间隔才 opt-in。
// 桥接令牌与数据目录在环境模块首次加载时读取，因此必须先设置再导入构建产物。
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

const dataDirectory = await mkdtemp(join(tmpdir(), 'voice-reminder-'))
process.env.VOICE_DSH_BRIDGE_TOKEN = randomUUID()
process.env.VOICE_DATA_DIR = dataDirectory

const build = '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib'
const { createBridgeRoute } = await import(`${build}/bridge/http.js`)
const { armRuntimeReminder, deliverRuntimeReminder, stopRuntimeReminder } = await import(`${build}/bridge/context.js`)
const { TOKEN } = await import(`${build}/shared/protocol.js`)

after(async () => { await rm(dataDirectory, { recursive: true, force: true }) })

const routePrefix = '/api/voice-terminal-shadow/v1'
const ADVISORY = 'VOICE SHADOW RUNTIME ADVISORY'
const advisories = injected => injected.filter(message => String(message.content?.[0]?.text ?? '').includes(ADVISORY))

/** 最小宿主替身：只提供 session.open 路径实际触及的服务。 */
function harness() {
  const bindings = new Map()
  const injected = []
  const handle = {
    agent: {
      session: { id: 'dsh-session', snapshotEvents: () => [] },
      status: 'idle',
      inject(message) { injected.push(message) },
      followup() {},
      inbox: { replace: () => false, remove: () => false },
    },
  }
  const workspace = { id: 'workspace-1', path: dataDirectory, status: async () => 'ok', attachSession: async () => {} }
  const ctx = {
    agentPresets: { resolve: async () => ({ id: 'shadow-agent' }) },
    agentDefaultModel: { currentSelection: () => ({ provider: 'mock', model: 'local-test' }) },
    agents: { create: async () => handle },
    workspaceRegistry: { resolveByPath: async () => workspace },
    sessionTitle: { rename: () => {} },
    get: () => undefined,
  }
  const settings = { get: () => ({}), update: async () => {} }
  const route = createBridgeRoute(
    ctx,
    { routePrefix, maxBodyBytes: 1_000_000 },
    bindings,
    Promise.resolve({ settings }),
    () => undefined,
  )
  return { bindings, injected, route }
}

async function open(route, dshSessionId, extra = {}) {
  const body = {
    protocol_version: '2.0', request_id: randomUUID(),
    voice_session_id: 'voice-session', dsh_session_id: dshSessionId, epoch: 1,
    ...extra,
  }
  const request = {
    url: `${routePrefix}/session.open`,
    method: 'POST',
    headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' },
    socket: { remoteAddress: '127.0.0.1' },
    async *[Symbol.asyncIterator]() { yield Buffer.from(JSON.stringify(body)) },
  }
  const response = { statusCode: 0, setHeader() {}, end(value) { this.body = JSON.parse(String(value)) } }
  await route.handler(request, response)
  return response
}

test('session.open 仅在显式正整数间隔时启用运行提醒，缺失或非正值解析为 0', async () => {
  const h = harness()
  const cases = [
    ['omitted', {}, 0],
    ['zero', { runtime_reminder_interval_ms: 0 }, 0],
    ['negative', { runtime_reminder_interval_ms: -5000 }, 0],
    ['fractional', { runtime_reminder_interval_ms: 1.5 }, 0],
    ['malformed', { runtime_reminder_interval_ms: 'soon' }, 0],
    ['opted in', { runtime_reminder_interval_ms: 250 }, 250],
  ]
  let seq = 0
  for (const [label, extra, expected] of cases) {
    const dshSessionId = `dsh-reminder-${++seq}`
    const response = await open(h.route, dshSessionId, extra)
    assert.equal(response.statusCode, 200, label)
    assert.equal(response.body.result, 'accepted', label)
    const binding = h.bindings.get(dshSessionId)
    assert.equal(binding.reminderIntervalMs, expected, label)
    assert.equal(binding.nextReminderMs, expected, label)
  }
})

test('未携带间隔的会话即使激活也不会注入运行提醒或空转定时器', async () => {
  const h = harness()
  const response = await open(h.route, 'dsh-disabled')
  assert.equal(response.body.result, 'accepted')
  const binding = h.bindings.get('dsh-disabled')
  assert.equal(binding.reminderIntervalMs, 0)

  // 模拟一次 wake 激活：禁用间隔不得启动定时器（0 毫秒间隔会自我重排空转），也不得注入。
  binding.activationId = 'activation-disabled'
  armRuntimeReminder(binding)
  await new Promise(resolve => setTimeout(resolve, 40))

  assert.equal(binding.reminderTimer, undefined)
  assert.equal(binding.activationStartedAt, undefined)
  assert.deepEqual(binding.records, [])
  assert.equal(advisories(h.injected).length, 0)
  assert.equal(deliverRuntimeReminder(binding, Date.now() + 600_000), false)
  assert.equal(advisories(h.injected).length, 0)
})

test('显式 opt-in 的正间隔在 session.open 后仍按间隔注入运行提醒', async () => {
  const h = harness()
  const response = await open(h.route, 'dsh-enabled', { runtime_reminder_interval_ms: 25 })
  assert.equal(response.body.result, 'accepted')
  const binding = h.bindings.get('dsh-enabled')
  assert.equal(binding.reminderIntervalMs, 25)

  binding.activationId = 'activation-enabled'
  armRuntimeReminder(binding)
  const deadline = Date.now() + 2000
  while (Date.now() < deadline && advisories(h.injected).length === 0) {
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  stopRuntimeReminder(binding, true)

  const [advisory] = advisories(h.injected)
  assert.ok(advisory, '显式 opt-in 的间隔仍注入运行提醒')
  assert.match(String(advisory.content[0].text), /运行时限提醒/)
  assert.ok(binding.records.some(item => item.type === 'voice/runtime-reminder' && item.data.action === 'inserted'))
})

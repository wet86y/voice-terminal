import assert from 'node:assert/strict'
import test from 'node:test'
import { randomUUID } from 'node:crypto'

process.env.VOICE_DSH_BRIDGE_TOKEN = randomUUID()
const build = '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib'
const { syncMiniMax } = await import(`${build}/bridge/minimax.js`)
const { createBridgeRoute } = await import(`${build}/bridge/http.js`)
const { TOKEN } = await import(`${build}/shared/protocol.js`)

/** harness（配置测试台）：模拟公开服务的并发修订与目录继承。 */
function harness(previous) {
  let revision = 0
  let providers = { untouched: { models: [{ id: 'keep-global' }] }, ...(previous ? { 'minimax-cn': structuredClone(previous) } : {}) }
  const writes = []; const credentials = []
  const globalSelection = { provider: 'untouched', model: 'keep-global' }
  const ctx = {
    credentials: { async set(ref, key) { credentials.push([ref, key]) } },
    settings: {
      describe() { return [{ ns: 'llm-pi-ai', revision, value: { providers: structuredClone(providers) } }] },
      async mutate(ns, operations, expected) {
        assert.equal(ns, 'llm-pi-ai'); assert.equal(expected, revision)
        if (operations[0].path.length === 3) {
          assert.deepEqual(operations[0].path, ['providers', 'minimax-cn', 'models'])
          providers['minimax-cn'].models = structuredClone(operations[0].value)
        } else {
          assert.deepEqual(operations[0].path, ['providers', 'minimax-cn'])
          providers['minimax-cn'] = structuredClone(operations[0].value)
        }
        revision++
        writes.push(structuredClone(providers['minimax-cn']))
      },
    },
    llm: { async listModels() { return providers['minimax-cn']?.models ?? [{ id: 'MiniMax-M2.7' }, { id: 'MiniMax-M3' }] } },
    agentDefaultModel: { currentSelection: () => globalSelection },
  }
  return { ctx, writes, credentials, globalSelection, read: () => structuredClone(providers) }
}

test('首次覆盖连接和 M3.1，显式原模型与其他供应方保持原值', async () => {
  const oldModel = { id: 'old-private', contextWindow: 9999, maxTokens: 888, compat: { supportsTemperature: false } }
  const h = harness({ api: 'openai-completions', baseURL: 'https://old.invalid', apiKeyEnv: 'OLD', models: [oldModel], timeoutMs: 7654 })
  await syncMiniMax(h.ctx, 'synthetic-test-secret', true)
  const route = h.read()['minimax-cn']
  assert.deepEqual(route.models[0], oldModel)
  assert.equal(route.api, 'anthropic-messages'); assert.equal(route.apiKeyEnv, 'MINIMAX_API_KEY')
  assert.equal(route.baseURL, 'https://api.minimaxi.com/anthropic'); assert.equal(route.timeoutMs, 7654)
  const model = route.models.find(row => row.id === 'MiniMax-M3.1-Flash')
  assert.equal(model.name, 'MiniMax M3.1 Flash')
  assert.equal(model.contextWindow, 1_000_000); assert.equal(model.maxTokens, 393_216)
  assert.deepEqual(model.input, ['text', 'image'])
  assert.deepEqual(Object.keys(model.reasoningEfforts), ['low', 'medium', 'high', 'xhigh', 'max'])
  assert.deepEqual(model.compat, { forceAdaptiveThinking: true })
  assert.equal(route.compat?.forceAdaptiveThinking, undefined)
  assert.deepEqual(h.read().untouched, { models: [{ id: 'keep-global' }] })
  assert.deepEqual(h.globalSelection, { provider: 'untouched', model: 'keep-global' })
})

test('目录模式保留所有原模型编号并展开 modelOverrides（模型覆盖）', async () => {
  const h = harness({ modelOverrides: { 'MiniMax-M3': { maxTokens: 12345, compat: { supportsTemperature: false } } } })
  await syncMiniMax(h.ctx, 'synthetic-test-secret', true)
  const route = h.read()['minimax-cn']
  assert.equal(route.modelOverrides, undefined)
  assert.deepEqual(route.models[0], { id: 'MiniMax-M2.7' })
  assert.deepEqual(route.models[1], { id: 'MiniMax-M3', maxTokens: 12345, compat: { supportsTemperature: false } })
  assert.equal(route.models.length, 3)
})

test('路由不存在先挂载连接，第二阶段中断后重入仍保留目录', async () => {
  const h = harness()
  const mutate = h.ctx.settings.mutate
  let interrupted = false
  h.ctx.settings.mutate = async (...args) => {
    if (h.writes.length === 1 && !interrupted) { interrupted = true; throw new Error('模拟写入中断') }
    return mutate(...args)
  }
  await assert.rejects(syncMiniMax(h.ctx, 'synthetic-test-secret', true), /中断/)
  assert.equal(h.writes[0].models, undefined)
  await syncMiniMax(h.ctx, 'synthetic-test-secret', true)
  assert.equal(h.read()['minimax-cn'].models.length, 3)
  assert.equal(h.read()['minimax-cn'].models.filter(row => row.id === 'MiniMax-M3.1-Flash').length, 1)
})

test('初始化完成后仅更新凭据，不改手工参数、当前选择或在途任务', async () => {
  const h = harness({ models: [{ id: 'MiniMax-M3.1-Flash', name: '用户自定名称（保留）', maxTokens: 4321 }] })
  const before = h.read()
  h.ctx.agents = { cancel() { assert.fail("密钥同步不得取消在途任务") } }
  await syncMiniMax(h.ctx, 'new-synthetic-secret', false)
  assert.deepEqual(h.read(), before); assert.equal(h.writes.length, 0)
  assert.equal(h.credentials.at(-1)[1], 'new-synthetic-secret')
})

test('已初始化路由仅清理上版预设名称的翻译，不重置模型参数', async () => {
  const h = harness({ timeoutMs: 4567, models: [{ id: 'MiniMax-M3.1-Flash', name: 'MiniMax M3.1 Flash（闪速版）', maxTokens: 4321, compat: { forceAdaptiveThinking: true } }, { id: 'old', name: '旧模型原名称' }] })
  const expected = h.read()
  expected['minimax-cn'].models[0].name = 'MiniMax M3.1 Flash'
  await syncMiniMax(h.ctx, 'synthetic-test-secret', false)
  assert.deepEqual(h.read(), expected)
  assert.equal(h.writes.length, 1)
  await syncMiniMax(h.ctx, 'synthetic-test-secret', false)
  assert.equal(h.writes.length, 1)
})

test('目录不可读或读回缺少模型时不宣布初始化成功', async () => {
  const h = harness({ timeoutMs: 1000 })
  h.ctx.llm.listModels = async () => []
  await assert.rejects(syncMiniMax(h.ctx, 'synthetic-test-secret', true), /目录/)
  assert.equal(h.writes.length, 0)
})

test('修订冲突后重新读配置再合并，保留同时新增的模型', async () => {
  const h = harness({ models: [{ id: 'old' }] })
  const mutate = h.ctx.settings.mutate
  let conflict = true
  h.ctx.settings.mutate = async (...args) => {
    if (conflict) {
      conflict = false
      args[1][0].value.models = [{ id: 'old' }, { id: 'concurrent-user-model' }]
      await mutate(...args)
      throw Object.assign(new Error('冲突'), { code: 'SETTINGS_CONFLICT' })
    }
    await mutate(...args)
  }
  await syncMiniMax(h.ctx, 'synthetic-test-secret', true)
  assert.ok(h.read()['minimax-cn'].models.some(row => row.id === 'concurrent-user-model'))
})

test('宿主同步入口沿用令牌认证，不返回密钥和配置正文', async () => {
  const h = harness({ models: [{ id: 'old' }] })
  const route = createBridgeRoute(h.ctx, { routePrefix: '/bridge', maxBodyBytes: 8192 }, new Map(), Promise.resolve({}), () => undefined)
  async function call(token) {
    const body = { api_key: 'synthetic-test-secret', initialize: true }
    const req = { url: '/bridge/host.minimax.sync', method: 'POST', headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, socket: { remoteAddress: '127.0.0.1' }, async *[Symbol.asyncIterator]() { yield Buffer.from(JSON.stringify(body)) } }
    const res = { statusCode: 0, setHeader() {}, end(value) { this.body = JSON.parse(String(value)) } }
    await route.handler(req, res)
    return res
  }
  assert.equal((await call('invalid')).statusCode, 401)
  assert.equal(h.credentials.length, 0)
  const response = await call(TOKEN)
  assert.equal(response.body.result, 'accepted')
  assert.equal(response.body.bootstrap_version, 1)
  assert.ok(!JSON.stringify(response.body).includes('synthetic-test-secret'))
  assert.equal(response.body.api_key, undefined)
})

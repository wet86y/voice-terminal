import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync, unlinkSync, rmdirSync, renameSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const repo = resolve(fileURLToPath(new URL('../../../../../', import.meta.url)))
const temporary = mkdtempSync(join(tmpdir(), 'voice-dsh-task-test-'))
const store = join(temporary, 'tasks.json')
const native = join(temporary, 'schedule.json')
const legacy = join(temporary, 'legacy.json')
process.env.VOICE_DSH_TASKS_PATH = store
process.env.VOICE_DSH_NATIVE_SCHEDULE_PATH = native
process.env.VOICE_DSH_LEGACY_WAKE_PATH = legacy
process.env.VOICE_DSH_CALLBACK_ORIGIN = 'http://127.0.0.1:8765'
process.env.VOICE_DSH_BRIDGE_TOKEN = 'test-token'

const historical = {
  id: 'schedule-old', kind: 'after', title: '已有链路任务', prompt: '旧任务内容',
  afterSeconds: 60, scheduledAt: '2026-09-26T15:13:25.574Z',
}
writeFileSync(store, JSON.stringify({ schema_version: 1, tasks: [{
  task_id: historical.id, target: 'shadow_agent', text: historical.prompt, reference: '已有上下文',
  record: historical, status: 'archived', created_at: historical.scheduledAt, updated_at: historical.scheduledAt,
  last_triggered_at: null, occurrence: 2, history: [{ messageId: 'old-message' }],
}, {
  task_id: 'legacy-every', target: 'shadow_agent', text: '旧间隔任务', reference: '',
  record: { id: 'legacy-every', kind: 'every', title: '旧间隔任务', prompt: '旧间隔任务', everySeconds: 60, scheduledAt: '2030-01-01T00:00:00.000Z' },
  status: 'pending', created_at: '2026-09-26T15:13:25.574Z', updated_at: '2026-09-26T15:13:25.574Z',
  last_triggered_at: null, occurrence: 1, history: [],
}] }), 'utf8')
const nativeContent = JSON.stringify({ tables: { tasks: {
  'native-task': { sessionId: 'ordinary-dsh-session', record: { ...historical, id: 'native-task', scheduledAt: '2030-01-01T00:00:00Z' },
    status: 'active', deliveryHistory: { records: [] } },
} } })
writeFileSync(native, nativeContent, 'utf8')

const { TaskService } = await import(
  new URL('../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/wake-tasks/task-service.js', import.meta.url).href,
)

// blockStore（存储故障注入）：阻止原子换入，原文件与恢复操作均留在测试目录。
function blockStore() {
  renameSync(store, `${store}.backup`)
  mkdirSync(store)
  return () => { rmdirSync(store); renameSync(`${store}.backup`, store) }
}
async function withBlockedStore(work) {
  const restore = blockStore()
  try { await work() }
  finally { restore() }
}

test('preserves project history, keeps native tasks separate, and delivers to the current voice mainline', async () => {
  const calls = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) })
    return { ok: true, json: async () => ({ result: 'accepted', dsh_session_id: 'voice-shadow-current' }) }
  }
  let service
  try {
    service = new TaskService()
    assert.equal(service.list().find(row => row.task_id === 'schedule-old').status, 'archived')
    assert.equal(service.list().some(row => row.task_id === 'native-task'), false)
    assert.equal(service.reference('schedule-old').reference, '已有上下文')
    assert.equal(service.list().find(row => row.task_id === 'schedule-old').creation_source, 'unknown')
    assert.equal(service.list().find(row => row.task_id === 'legacy-every').loop, true,
      '没有新字段的旧间隔任务应继续循环')
    assert.equal(service.list().find(row => row.task_id === 'legacy-every').trigger_count, null)
    const created = await service.create({ when: '1s', text: '请检查现在的状态' }, '创建时背景')
    assert.equal(created.has_reference, true)
    assert.equal(created.creation_source, 'agent')
    assert.equal(service.reference(created.task_id).reference, '创建时背景')
    const daily = await service.create({ title: '每日检查', prompt: '检查日报', creation_source: 'manual',
      daily: { time: '23:59:59', time_zone: 'Asia/Hong_Kong' } })
    const weekly = await service.create({ title: '每周检查', prompt: '检查周报',
      weekly: { time: '23:59:59', time_zone: 'Asia/Hong_Kong', weekdays: [1, 3] } })
    const cron = await service.create({ title: '工作日检查', prompt: '检查任务',
      cron: { expression: '0 9 * * 1-5', time_zone: 'Asia/Hong_Kong' } })
    assert.deepEqual([daily.rule_kind, weekly.rule_kind, cron.rule_kind], ['daily', 'weekly', 'cron'])
    assert.deepEqual(cron.cron_input, { expression: '0 9 * * 1-5', time_zone: 'Asia/Hong_Kong' },
      'Cron（定时规则）列表输出提供可直接复用的官方输入格式')
    await assert.rejects(service.create({ title: '错误时间格式', prompt: '不能把时钟时间当 Cron 表达式',
      cron: { expression: '02:55', time_zone: 'Asia/Hong_Kong' } }), /exactly five whitespace-separated fields/u,
    'Cron（定时表达式）应拒绝 HH:mm（时:分）格式')
    await assert.rejects(service.create({ title: '错误时区字段', prompt: '必须使用官方输入字段',
      cron: { expression: '0 9 * * 1-5', timeZone: 'Asia/Hong_Kong' } }), /exactly expression and time_zone/u,
    'Cron（定时表达式）应拒绝与官方接口不兼容的字段名')
    assert.equal(daily.creation_source, 'manual')
    assert.equal(daily.has_reference, false)
    await new Promise(resolve => setTimeout(resolve, 1300))
    assert.equal(calls.length, 1)
    assert.equal(calls[0].body.task_id, created.task_id)
    assert.equal(service.list().find(row => row.task_id === created.task_id).status, 'archived')
    await service.close()
    service = new TaskService()
    assert.equal(service.list().length, 6)
    assert.equal(service.list().find(row => row.task_id === daily.task_id).creation_source, 'manual')
    assert.equal(service.list().some(row => row.task_id === 'native-task'), false)
    assert.equal(service.list().find(row => row.task_id === 'schedule-old').delivery_history[0].messageId, 'old-message')
    assert.equal(readFileSync(native, 'utf8'), nativeContent, '官方会话任务文件保持独立且未改动')
    assert.equal(service.list().find(row => row.task_id === created.task_id).delivery_history[0].dshSessionId,
      'voice-shadow-current')
    await assert.rejects(service.removeArchive(daily.task_id), /先取消/u)
    assert.equal(service.list().find(row => row.task_id === daily.task_id).status, 'pending')
    const beforeFailure = readFileSync(store, 'utf8')
    await withBlockedStore(async () => {
      await assert.rejects(service.removeArchive('schedule-old'))
      assert.equal(service.list().find(row => row.task_id === 'schedule-old').status, 'archived')
      assert.equal(readFileSync(`${store}.backup`, 'utf8'), beforeFailure)
    })
    writeFileSync(legacy, JSON.stringify({ tasks: { 'legacy-cancelled': {
      text: '旧存储取消任务', scheduled_at: 1700000000, status: 'cancelled',
    } } }), 'utf8')
    await service.close()
    service = new TaskService()
    assert.equal(service.list().some(row => row.task_id === 'legacy-cancelled'), true)
    await service.removeArchive('legacy-cancelled')
    await service.removeArchive('schedule-old')
    assert.deepEqual(await service.removeArchive('schedule-old'), { task_id: 'schedule-old', deleted: true })
    await service.cancel(daily.task_id)
    assert.equal(service.list().find(row => row.task_id === daily.task_id).status, 'cancelled')
    await service.removeArchive(daily.task_id)
    await service.close()
    service = new TaskService()
    for (const id of ['legacy-cancelled', 'schedule-old', daily.task_id]) {
      assert.equal(service.list().some(row => row.task_id === id), false, '重启或旧存储导入不能恢复已删除的归档')
      assert.equal(JSON.parse(readFileSync(store, 'utf8')).deleted_task_ids.includes(id), true)
    }
    assert.equal(service.list().some(row => row.task_id === created.task_id), true, '保留未删除的交付历史')
    assert.equal(readFileSync(native, 'utf8'), nativeContent, '归档删除不修改官方会话任务')
  } finally {
    if (service) await service.close()
    globalThis.fetch = originalFetch
    for (const path of [store, native, legacy]) {
      try { unlinkSync(path) } catch (error) { if (error?.code !== 'ENOENT') throw error }
    }
    rmdirSync(temporary)
  }
})

for (const operation of ['create', 'update', 'cancel']) {
  test(`任务${operation}（创建／修改／取消）写盘失败时保留原任务及计时，后续保存不混入失败操作`, async () => {
    mkdirSync(temporary, { recursive: true })
    const originalFetch = globalThis.fetch
    const calls = []
    globalThis.fetch = async (_url, options) => {
      calls.push(JSON.parse(options.body))
      return { ok: true, json: async () => ({ result: 'accepted' }) }
    }
    let service
    try {
      service = new TaskService()
      const original = await service.create({ when: '1s', text: '原任务正文' }, '冻结参考')
      const before = structuredClone(service.list())
      const stored = readFileSync(store, 'utf8')
      await withBlockedStore(async () => {
        await assert.rejects(operation === 'create'
          ? service.create({ when: '1s', text: '失败创建的任务' })
          : operation === 'update'
            ? service.update(original.task_id, { text: '失败修改的正文', when: '30m' })
            : service.cancel(original.task_id))
        assert.deepEqual(service.list(), before)
        assert.equal(readFileSync(`${store}.backup`, 'utf8'), stored)
        assert.equal(service.reference(original.task_id).reference, '冻结参考')
      })
      const later = await service.create({ when: '30m', text: '后续正常保存' })
      assert.deepEqual(JSON.parse(readFileSync(store, 'utf8')).tasks.filter(row => row.task_id !== later.task_id),
        JSON.parse(stored).tasks)
      await new Promise(resolve => setTimeout(resolve, 1300))
      assert.deepEqual(calls.map(row => row.text), ['原任务正文'])
      assert.equal(service.list().find(row => row.task_id === original.task_id).status, 'archived')
      await service.close()
      service = new TaskService()
      assert.equal(service.list().length, 2)
      assert.equal(service.list().find(row => row.task_id === original.task_id).status, 'archived')
    } finally {
      if (service) await service.close()
      globalThis.fetch = originalFetch
      unlinkSync(store)
      rmdirSync(temporary)
    }
  })
}

test('投递成功但结算写盘失败时保留同次触发，重启重试沿用幂等标识', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-05T00:00:00Z') })
  mkdirSync(temporary, { recursive: true })
  const originalFetch = globalThis.fetch
  const calls = []
  let restore
  globalThis.fetch = async (_url, options) => {
    const body = JSON.parse(options.body)
    calls.push(body)
    if (calls.length === 1) restore = blockStore()
    return { ok: true, json: async () => ({ result: calls.length === 1 ? 'accepted' : 'duplicate', message_id: body.occurrence_id }) }
  }
  let service
  try {
    service = new TaskService()
    const original = await service.create({ when: '1s', text: '投递结算测试' })
    const before = structuredClone(service.list())
    t.mock.timers.tick(1000)
    await service.chain
    assert.equal(calls.length, 1)
    assert.deepEqual(service.list(), before)
    await assert.rejects(service.update(original.task_id, { text: '未结算前不能改写投递内容' }))
    await assert.rejects(service.cancel(original.task_id))
    assert.deepEqual(service.list(), before)
    const stored = JSON.parse(readFileSync(`${store}.backup`, 'utf8')).tasks[0]
    assert.equal(stored.delivery_pending, true)
    assert.equal(stored.occurrence, 1)
    assert.equal(stored.text, '投递结算测试')
    await service.close()
    restore(); restore = undefined
    service = new TaskService()
    t.mock.timers.tick(0)
    await service.chain
    assert.equal(calls.length, 2)
    assert.equal(calls[0].occurrence_id, calls[1].occurrence_id)
    const completed = service.list().find(row => row.task_id === original.task_id)
    assert.equal(completed.status, 'archived')
    assert.equal(completed.occurrence, 2)
    assert.equal(completed.delivery_history.length, 1)
    assert.equal(JSON.parse(readFileSync(store, 'utf8')).tasks[0].status, 'archived')
    assert.equal('delivery_pending' in JSON.parse(readFileSync(store, 'utf8')).tasks[0], false)
  } finally {
    if (service) await service.close()
    if (restore) restore()
    globalThis.fetch = originalFetch
    unlinkSync(store)
    rmdirSync(temporary)
  }
})

for (const repeated of [false, true]) {
  test(`${repeated ? '重复' : '单次'}任务结算恢复后再接受用户变更，已确认投递不在进程内重发`, async t => {
    t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-05T00:00:00Z') })
    mkdirSync(temporary, { recursive: true })
    const originalFetch = globalThis.fetch
    const calls = []
    let restore
    globalThis.fetch = async (_url, options) => {
      const body = JSON.parse(options.body)
      calls.push(body)
      if (calls.length === 1) restore = blockStore()
      return { ok: true, json: async () => ({ result: 'accepted', message_id: body.occurrence_id }) }
    }
    let service
    try {
      service = new TaskService()
      const original = await service.create({ when: '1s', text: '原投递内容',
        ...(repeated ? { repeat: '1m', loop: false, trigger_count: 2 } : {}) })
      const before = structuredClone(service.list())
      t.mock.timers.tick(1000)
      await service.chain
      assert.deepEqual(service.list(), before)
      restore(); restore = undefined
      await service.create({ when: '30m', text: '恢复写盘后的独立任务' })
      let row = service.list().find(item => item.task_id === original.task_id)
      assert.equal(calls.length, 1)
      assert.equal(row.occurrence, 2)
      assert.equal(row.status, repeated ? 'pending' : 'archived')
      assert.equal(row.delivery_history.length, 1)
      assert.equal(row.delivery_history[0].prompt, '原投递内容')
      assert.equal(JSON.parse(readFileSync(store, 'utf8')).tasks[0].delivery_pending, undefined)
      if (repeated) {
        await service.update(original.task_id, { text: '下一次投递内容' })
        t.mock.timers.tick(60_000)
        await service.chain
        row = service.list().find(item => item.task_id === original.task_id)
        assert.equal(row.status, 'archived')
        assert.deepEqual(row.delivery_history.map(item => item.prompt), ['原投递内容', '下一次投递内容'])
        assert.deepEqual(calls.map(item => item.occurrence_id), [`${original.task_id}-1`, `${original.task_id}-2`])
      }
    } finally {
      if (service) await service.close()
      if (restore) restore()
      globalThis.fetch = originalFetch
      unlinkSync(store)
      rmdirSync(temporary)
    }
  })
}

for (const cancel of [false, true]) {
  test(`回执丢失后${cancel ? '取消会停止后续重试' : '重启仍固定原投递内容和标识'}，不把未投递的新正文写入历史`, async t => {
    t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-05T00:00:00Z') })
    mkdirSync(temporary, { recursive: true })
    const originalFetch = globalThis.fetch
    const calls = []
    const receiver = new Map() // receiver（持久接收方替身）：接受后即使回执丢失，也按原触发标识去重。
    globalThis.fetch = async (_url, options) => {
      const body = JSON.parse(options.body)
      calls.push(body)
      if (!receiver.has(body.occurrence_id)) {
        receiver.set(body.occurrence_id, body)
        throw new TypeError('接收成功后连接中断，客户端未收到回执')
      }
      return { ok: true, json: async () => ({ result: 'duplicate', message_id: body.occurrence_id }) }
    }
    let service
    try {
      service = new TaskService()
      const original = await service.create({ when: '1s', text: '接收方实际收到的正文' })
      t.mock.timers.tick(1000)
      await service.chain
      assert.equal(JSON.parse(readFileSync(store, 'utf8')).tasks[0].delivery_pending, true)
      await assert.rejects(service.update(original.task_id, { text: '不能冒用旧标识的新正文' }), /投递结果尚未确认/u)
      if (cancel) await service.cancel(original.task_id)
      await service.close()
      service = new TaskService()
      if (!cancel) await assert.rejects(service.update(original.task_id, { text: '重启后也不能改写' }), /投递结果尚未确认/u)
      t.mock.timers.tick(30_000)
      await service.chain
      const row = service.list().find(item => item.task_id === original.task_id)
      assert.equal(row.status, cancel ? 'cancelled' : 'archived')
      assert.equal(calls.length, cancel ? 1 : 2)
      assert.equal(receiver.size, 1)
      if (!cancel) {
        assert.deepEqual(calls[0], calls[1])
        assert.equal(row.delivery_history[0].prompt, calls[0].text)
        assert.equal(row.delivery_history.length, 1)
      }
    } finally {
      if (service) await service.close()
      globalThis.fetch = originalFetch
      unlinkSync(store)
      rmdirSync(temporary)
    }
  })
}

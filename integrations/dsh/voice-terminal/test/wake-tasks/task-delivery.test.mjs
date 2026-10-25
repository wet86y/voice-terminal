import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, unlinkSync, rmdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const repo = resolve(fileURLToPath(new URL('../../../../../', import.meta.url)))
const temporary = mkdtempSync(join(tmpdir(), 'voice-dsh-delivery-test-'))
const store = join(temporary, 'tasks.json')
const native = join(temporary, 'schedule.json')
process.env.VOICE_DSH_TASKS_PATH = store
process.env.VOICE_DSH_NATIVE_SCHEDULE_PATH = native
process.env.VOICE_DSH_CALLBACK_ORIGIN = 'http://127.0.0.1:8765'
process.env.VOICE_DSH_BRIDGE_TOKEN = 'test-token'

const { TaskService } = await import(
  new URL('../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/wake-tasks/task-service.js', import.meta.url).href,
)

test('实际 DSH 规则投递一次性与重复任务，并在失败重试及重启后复用触发标识', async t => {
  const start = Date.parse('2026-10-05T00:00:00.000Z')
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: start })
  const originalFetch = globalThis.fetch
  const originalWarn = console.warn
  const calls = []
  const replies = [
    { ok: true, body: { result: 'accepted', message_id: 'once-message', dsh_session_id: 'dsh-session' } },
    { ok: false, status: 503, body: { detail: '暂时不可用' } },
    { ok: true, body: { result: 'duplicate', message_id: 'repeat-1', dsh_session_id: 'dsh-session' } },
    { ok: false, status: 503, body: { detail: '暂时不可用' } },
    { ok: true, body: { result: 'duplicate', message_id: 'repeat-2', dsh_session_id: 'dsh-session' } },
  ]
  const services = []
  globalThis.fetch = async (url, options) => {
    const call = { url, body: JSON.parse(options.body) }
    calls.push(call)
    assert.equal(options.method, 'POST')
    assert.equal(options.headers.authorization, 'Bearer test-token')
    const reply = replies.shift()
    assert.ok(reply, 'unexpected callback delivery')
    return { ok: reply.ok, status: reply.status ?? 200, json: async () => reply.body }
  }
  console.warn = () => {}
  const createService = () => {
    const service = new TaskService()
    services.push(service)
    return service
  }

  try {
    const service = createService()
    const once = await service.create({ title: '一次任务', text: '检查一次', when: '1s' })
    const repeated = await service.create({
      title: '重复任务', text: '定期检查', when: '1s', repeat: '60s',
    })
    assert.equal(service.list().find(row => row.task_id === once.task_id).status, 'pending')
    assert.equal(service.list().find(row => row.task_id === repeated.task_id).rule_kind, 'every')

    t.mock.timers.tick(1_000)
    await service.chain
    assert.deepEqual(calls.slice(0, 2).map(call => call.body.task_id), [once.task_id, repeated.task_id])
    assert.equal(calls[0].body.occurrence_id, `${once.task_id}-1`)
    assert.equal(service.list().find(row => row.task_id === once.task_id).status, 'archived')
    assert.equal(service.list().find(row => row.task_id === repeated.task_id).occurrence, 1)

    // occurrence_id（触发标识）由 Voice 回调透传为请求幂等键；失败不推进 occurrence（触发序号）。
    assert.equal(calls[1].body.occurrence_id, `${repeated.task_id}-1`)
    t.mock.timers.tick(30_000)
    await service.chain
    assert.equal(calls[2].body.occurrence_id, calls[1].body.occurrence_id)
    assert.equal(service.list().find(row => row.task_id === repeated.task_id).occurrence, 2)
    await service.close()

    let recovered = createService()
    const nextAt = Date.parse(recovered.list().find(row => row.task_id === repeated.task_id).when)
    assert.ok(nextAt >= Date.now())
    t.mock.timers.tick(nextAt - Date.now())
    await recovered.chain
    assert.equal(calls[3].body.occurrence_id, `${repeated.task_id}-2`)
    const saved = JSON.parse(readFileSync(store, 'utf8'))
    const savedRepeated = saved.tasks.find(row => row.task_id === repeated.task_id)
    assert.equal(savedRepeated.status, 'pending')
    assert.equal(savedRepeated.occurrence, 2)
    assert.equal(Object.hasOwn(savedRepeated, 'retry_at'), false)
    await recovered.close()

    recovered = createService()
    t.mock.timers.tick(0)
    await recovered.chain
    assert.equal(calls[4].body.occurrence_id, calls[3].body.occurrence_id)
    const restored = recovered.list().find(row => row.task_id === repeated.task_id)
    assert.equal(restored.occurrence, 3)
    assert.equal(restored.delivery_history.length, 2)
    assert.equal(replies.length, 0)
  } finally {
    for (const service of services) await service.close()
    globalThis.fetch = originalFetch
    console.warn = originalWarn
    for (const path of [store, native]) {
      try { unlinkSync(path) } catch (error) { if (error?.code !== 'ENOENT') throw error }
    }
    rmdirSync(temporary)
  }
})

test('有限间隔只在成功投递达到总次数后归档，并在重启后保留次数设置', async t => {
  const start = Date.parse('2026-10-05T00:00:00.000Z')
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: start })
  const originalFetch = globalThis.fetch
  const originalWarn = console.warn
  const calls = []
  let service
  globalThis.fetch = async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) })
    if (calls.length === 1) return { ok: false, status: 503, json: async () => ({ detail: '暂时不可用' }) }
    return { ok: true, status: 200, json: async () => ({ result: 'accepted', message_id: `finite-${calls.length}` }) }
  }
  console.warn = () => {}
  try {
    service = new TaskService()
    const finite = await service.create({
      title: '有限间隔', text: '检查两次', every_seconds: 60, loop: false, trigger_count: 2,
    })
    assert.equal(finite.when, new Date(start + 60_000).toISOString())
    assert.equal(finite.loop, false)
    assert.equal(finite.trigger_count, 2)
    await assert.rejects(service.create({ title: '超长间隔', text: '不应保存', every_seconds: 86_460 }))
    await assert.rejects(service.create({ title: '缺少次数', text: '不应保存', every_seconds: 60, loop: false }))

    t.mock.timers.tick(60_000)
    await service.chain
    let row = service.list().find(item => item.task_id === finite.task_id)
    assert.equal(calls[0]?.body.occurrence_id, `${finite.task_id}-1`)
    assert.equal(row?.status, 'pending')
    assert.equal(row?.occurrence, 1, '失败投递不能推进有限任务的触发次数')

    t.mock.timers.tick(30_000)
    await service.chain
    row = service.list().find(item => item.task_id === finite.task_id)
    assert.equal(calls[1]?.body.occurrence_id, calls[0]?.body.occurrence_id, '失败重试应沿用同一触发标识')
    assert.equal(row?.status, 'pending')
    assert.equal(row?.occurrence, 2)

    await service.close()
    service = new TaskService()
    row = service.list().find(item => item.task_id === finite.task_id)
    assert.equal(row?.status, 'pending')
    assert.equal(row?.occurrence, 2)
    assert.equal(row?.trigger_count, 2, '重启后应恢复有限触发次数与当前序号')

    t.mock.timers.tick(30_000)
    await service.chain
    row = service.list().find(item => item.task_id === finite.task_id)
    assert.equal(calls[2]?.body.occurrence_id, `${finite.task_id}-2`)
    assert.equal(row?.status, 'archived')
    assert.equal(row?.occurrence, 3)
    const persisted = JSON.parse(readFileSync(store, 'utf8'))
    assert.equal(persisted.schema_version, 1)
    assert.equal(persisted.tasks[0].loop, false)
    assert.equal(persisted.tasks[0].trigger_count, 2)

    await service.close()
    service = new TaskService()
    row = service.list().find(item => item.task_id === finite.task_id)
    assert.equal(row?.status, 'archived')
    assert.equal(row?.trigger_count, 2)

    const updatedFinite = await service.create({
      title: '调整次数', text: '把次数调低', every_seconds: 60, loop: false, trigger_count: 5,
    })
    t.mock.timers.tick(60_000)
    await service.chain
    t.mock.timers.tick(60_000)
    await service.chain
    const updated = await service.update(updatedFinite.task_id, { trigger_count: 1 })
    assert.equal(updated.occurrence, 3)
    assert.equal(updated.status, 'archived', '新总次数低于已成功触发次数时应立即归档')
  } finally {
    if (service) await service.close()
    globalThis.fetch = originalFetch
    console.warn = originalWarn
    for (const path of [store, native]) {
      try { unlinkSync(path) } catch (error) { if (error?.code !== 'ENOENT') throw error }
    }
    rmdirSync(temporary)
  }
})

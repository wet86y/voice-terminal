import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, unlinkSync, rmdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const repo = resolve(fileURLToPath(new URL('../../../../', import.meta.url)))
const temporary = mkdtempSync(join(tmpdir(), 'voice-dsh-task-test-'))
const store = join(temporary, 'tasks.json')
const native = join(temporary, 'schedule.json')
process.env.VOICE_DSH_SCHEDULE_MODULE = join(repo, '.runtime/dsh/source/packages/schedule/schedule/lib/index.js')
process.env.VOICE_DSH_TASKS_PATH = store
process.env.VOICE_DSH_NATIVE_SCHEDULE_PATH = native
process.env.VOICE_DSH_CALLBACK_ORIGIN = 'http://127.0.0.1:8765'
process.env.VOICE_DSH_BRIDGE_TOKEN = 'test-token'

const historical = {
  id: 'schedule-old', kind: 'after', title: '旧任务', prompt: '旧任务内容',
  afterSeconds: 60, scheduledAt: '2026-09-26T15:13:25.574Z',
}
writeFileSync(native, JSON.stringify({ tables: { tasks: {
  'schedule-old': { sessionId: 'voice-shadow-retired', record: historical,
    status: 'inactive', deliveryHistory: { records: [{ messageId: 'old-message' }] } },
} } }), 'utf8')

const { TaskService } = await import(
  new URL('../../../../.runtime/dsh/bundles/voice-terminal-wake-tasks/lib/task-service.js', import.meta.url).href,
)

test('imports history once, persists a new task, and delivers it through the current-mainline endpoint', async () => {
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
    const created = await service.create({ when: '1s', text: '请检查现在的状态' }, '创建时背景')
    assert.equal(created.has_reference, true)
    assert.equal(service.reference(created.task_id).reference, '创建时背景')
    const daily = await service.create({ title: '每日检查', prompt: '检查日报',
      daily: { time: '23:59:59', time_zone: 'Asia/Hong_Kong' } })
    const weekly = await service.create({ title: '每周检查', prompt: '检查周报',
      weekly: { time: '23:59:59', time_zone: 'Asia/Hong_Kong', weekdays: [1, 3] } })
    const cron = await service.create({ title: '工作日检查', prompt: '检查任务',
      cron: { expression: '0 9 * * 1-5', time_zone: 'Asia/Hong_Kong' } })
    assert.deepEqual([daily.rule_kind, weekly.rule_kind, cron.rule_kind], ['daily', 'weekly', 'cron'])
    await new Promise(resolve => setTimeout(resolve, 1300))
    assert.equal(calls.length, 1)
    assert.equal(calls[0].body.task_id, created.task_id)
    assert.equal(service.list().find(row => row.task_id === created.task_id).status, 'archived')
    await service.close()
    service = new TaskService()
    assert.equal(service.list().length, 5)
    assert.equal(service.list().find(row => row.task_id === created.task_id).delivery_history[0].dshSessionId,
      'voice-shadow-current')
  } finally {
    if (service) await service.close()
    globalThis.fetch = originalFetch
    for (const path of [store, native]) {
      try { unlinkSync(path) } catch (error) { if (error?.code !== 'ENOENT') throw error }
    }
    rmdirSync(temporary)
  }
})

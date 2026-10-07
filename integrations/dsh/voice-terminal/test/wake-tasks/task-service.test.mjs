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
}] }), 'utf8')
const nativeContent = JSON.stringify({ tables: { tasks: {
  'native-task': { sessionId: 'ordinary-dsh-session', record: { ...historical, id: 'native-task', scheduledAt: '2030-01-01T00:00:00Z' },
    status: 'active', deliveryHistory: { records: [] } },
} } })
writeFileSync(native, nativeContent, 'utf8')

const { TaskService } = await import(
  new URL('../../../../../.runtime/dsh/bundles/voice-terminal-test/lib/wake-tasks/task-service.js', import.meta.url).href,
)

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
    assert.equal(daily.creation_source, 'manual')
    assert.equal(daily.has_reference, false)
    await new Promise(resolve => setTimeout(resolve, 1300))
    assert.equal(calls.length, 1)
    assert.equal(calls[0].body.task_id, created.task_id)
    assert.equal(service.list().find(row => row.task_id === created.task_id).status, 'archived')
    await service.close()
    service = new TaskService()
    assert.equal(service.list().length, 5)
    assert.equal(service.list().find(row => row.task_id === daily.task_id).creation_source, 'manual')
    assert.equal(service.list().some(row => row.task_id === 'native-task'), false)
    assert.equal(service.list().find(row => row.task_id === 'schedule-old').delivery_history[0].messageId, 'old-message')
    assert.equal(readFileSync(native, 'utf8'), nativeContent, '官方会话任务文件保持独立且未改动')
    assert.equal(service.list().find(row => row.task_id === created.task_id).delivery_history[0].dshSessionId,
      'voice-shadow-current')
    await assert.rejects(service.removeArchive(daily.task_id), /先取消/u)
    assert.equal(service.list().find(row => row.task_id === daily.task_id).status, 'pending')
    const beforeFailure = readFileSync(store, 'utf8')
    renameSync(store, `${store}.backup`)
    mkdirSync(store) // 用目录模拟持久化失败；不能先报成功或丢失内存中的任务。
    try {
      await assert.rejects(service.removeArchive('schedule-old'))
      assert.equal(service.list().find(row => row.task_id === 'schedule-old').status, 'archived')
      assert.equal(readFileSync(`${store}.backup`, 'utf8'), beforeFailure)
    } finally { rmdirSync(store); renameSync(`${store}.backup`, store) }
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

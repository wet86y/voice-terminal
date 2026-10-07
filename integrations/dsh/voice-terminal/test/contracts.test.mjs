import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
process.env.VOICE_DSH_CALLBACK_ORIGIN = 'http://127.0.0.1:18765'
process.env.VOICE_DSH_BRIDGE_TOKEN = 'isolated-test-token'
const root = new URL('../../../../.local/build/dsh/bundles/voice-terminal-test/lib/', import.meta.url)
const { CallbackScope } = await import(new URL('shared/callback.js', root))
const { installRootTools } = await import(new URL('bridge/tools.js', root))

test('工具输入范围、调用者身份与取消信号保留同一契约', async () => {
  const tools = new Map()
  const agent = { id: 'root' }
  const binding = { handle: { agent }, state: 'active', activationId: 'activation', dshSessionId: 'dsh', voiceSessionId: 'voice', epoch: 1, revision: 0 }
  installRootTools({ tools: { register(tool) { tools.set(tool.name, tool) } } }, binding)
  assert.deepEqual([...tools.keys()], ['get_latest_context', 'read_ambient_context', 'publish_progress', 'publish_voice_information', 'request_early_silence', 'request_user_input', 'return_shadow_result', 'finish_shadow_no_action'])
  const originalFetch = globalThis.fetch
  let calls = 0
  const controller = new AbortController()
  const exec = { agent, callId: 'call', signal: controller.signal, concludeTurn() {} }
  globalThis.fetch = async (_url, options) => {
    calls++
    assert.equal(options.signal, controller.signal)
    return { ok: true, status: 200, json: async () => ({ result: 'accepted' }) }
  }
  try {
    for (const [name, args] of [
      ['get_latest_context', { since_revision: -1 }], ['read_ambient_context', { limit: 51 }],
      ['publish_progress', { text: ' ' }], ['publish_progress', { text: 'x'.repeat(513) }],
      ['publish_voice_information', { text: 'x'.repeat(2001) }],
      ['request_user_input', { question: '问题', options: Array(9).fill('选项') }],
      ['return_shadow_result', { text: 'x'.repeat(8001) }], ['finish_shadow_no_action', { reason: 'x'.repeat(501) }],
      ['request_early_silence', { unexpected: true }],
    ]) await assert.rejects(tools.get(name).execute(args, exec), undefined, `${name} 保留原有业务限制`)
    assert.equal(calls, 0, '非法参数不能发出回调')
    await assert.rejects(tools.get('publish_progress').execute({ text: '合法文本' }, { ...exec, agent: { id: 'child' } }), /active bound root Agent/u)
    await tools.get('request_early_silence').execute({}, exec)
    assert.equal(calls, 1)
  } finally { globalThis.fetch = originalFetch }
})

test('CallbackScope（回调生命周期）释放会取消并等待未完成网络请求', async () => {
  const originalFetch = globalThis.fetch
  let cancelled = false
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => { cancelled = true; reject(options.signal.reason) }, { once: true })
  })
  try {
    const scope = new CallbackScope()
    const pending = scope.send('/callback', { event: 'test' })
    const rejected = assert.rejects(pending)
    await scope.dispose()
    await rejected
    assert.equal(cancelled, true)
    assert.equal(scope.signal.aborted, true)
  } finally { globalThis.fetch = originalFetch }
})

test('TaskService（任务服务）关闭时取消后台投递，并保留任务以供重启重试', async () => {
  const temporary = await mkdtemp(join(tmpdir(), 'voice-dsh-cancel-'))
  process.env.VOICE_DSH_TASKS_PATH = join(temporary, 'tasks.json')
  const { TaskService } = await import(new URL('wake-tasks/task-service.js', root))
  const originalFetch = globalThis.fetch
  const originalWarn = console.warn
  let service
  let cancelled = false
  let started
  const began = new Promise(resolve => { started = resolve })
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    started()
    options.signal.addEventListener('abort', () => { cancelled = true; reject(options.signal.reason) }, { once: true })
  })
  console.warn = () => {}
  try {
    service = new TaskService()
    const task = await service.create({ title: '取消测试', text: '保留', after_seconds: 1 })
    await began
    await service.close()
    assert.equal(cancelled, true)
    const row = service.list().find(value => value.task_id === task.task_id)
    assert.equal(row.status, 'pending')
    assert.equal(row.occurrence, 1)
  } finally {
    await service?.close()
    globalThis.fetch = originalFetch
    console.warn = originalWarn
    await rm(temporary, { recursive: true, force: true })
  }
})

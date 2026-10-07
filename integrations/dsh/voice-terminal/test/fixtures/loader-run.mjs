/** Real profile composition（真实配置组合）：只替代模型输出，其余使用锁定宿主与完整预设。 */
import assert from 'node:assert/strict'
import { cp, mkdir, mkdtemp, writeFile, rm, symlink, readFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { randomUUID } from 'node:crypto'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..')
const source = join(repo, '.runtime/dsh/source')
const temporary = await mkdtemp(join(tmpdir(), 'voice-dsh-loader-'))
const profile = join(temporary, 'home/profiles/test')
const token = randomUUID()
const received = []
const callback = createServer(async (req, res) => {
  assert.equal(req.headers.authorization, `Bearer ${token}`)
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  received.push({ path: req.url, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) })
  res.setHeader('content-type', 'application/json')
  res.end(JSON.stringify({ result: 'accepted', message_id: 'test-receipt' }))
})
let booted
try {
  await new Promise(resolveListen => callback.listen(0, '127.0.0.1', resolveListen))
  process.env.DSH_HOME = join(temporary, 'home')
  process.env.VOICE_DATA_DIR = temporary
  process.env.VOICE_DSH_BRIDGE_TOKEN = token
  process.env.VOICE_DSH_CALLBACK_ORIGIN = `http://127.0.0.1:${callback.address().port}`
  process.env.VOICE_DSH_TASKS_ACTIVE = '1'
  process.env.VOICE_DSH_TASKS_PATH = join(temporary, 'tasks.json')
  process.env.DSH_TELEMETRY_DISABLED = '1'
  process.chdir(temporary)
  await mkdir(join(profile, 'node_modules/@voice-terminal'), { recursive: true })
  await symlink(join(source, 'python/sdk-runtime/node_modules/@deepseek-ai'), join(profile, 'node_modules/@deepseek-ai'), process.platform === 'win32' ? 'junction' : 'dir')
  const built = join(repo, '.runtime/dsh/bundles/voice-terminal-test')
  const installed = join(profile, 'node_modules/@voice-terminal/dsh-plugin')
  await cp(built, installed, { recursive: true, filter: path => path !== join(built, 'node_modules') })
  await writeFile(join(profile, 'package.json'), JSON.stringify({
    name: 'test-profile', private: true, dependencies: { '@voice-terminal/dsh-plugin': '0.2.0' },
    dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', '@voice-terminal/dsh-plugin'], patchReload: 'live' } },
  }))
  await writeFile(join(profile, 'cordis.patch.yml'), [
    '- id: agent-default-model', '  config:', '    provider: mock', '    model: local-test',
    '- id: agent-preset-registry', '  config:', '    default: shadow-agent',
    '- id: session-log-deepseek', '  disabled: true',
    '- id: session-title-llm', '  disabled: true',
    '- id: web-runtime', '  config:', '    openBrowser: false', '    printUrl: false', '    surfaceContext: true', '',
  ].join('\n'))
  const originalFetch = globalThis.fetch
  globalThis.fetch = (input, init) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url)
    assert.equal(url.hostname, '127.0.0.1', '测试禁止外部网络访问')
    return originalFetch(input, init)
  }
  const { runProfile } = await import(pathToFileURL(join(source, 'apps/cli/lib/profile-boot.js')).href)
  const require = createRequire(join(source, 'apps/cli/package.json'))
  const { loadLayeredEnv } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-app-boot')).href)
  booted = await runProfile({ profile: 'test', environment: loadLayeredEnv('dsh'), args: ['--no-open', '--port', '0'], patchFiles: [] })
  const ctx = booted.ctx
  assert.equal(ctx.clientModules.graph().entries.filter(entry => entry.id === '@voice-terminal/dsh-plugin').length, 1, '统一包只注册一个浏览器模块')
  assert.equal(ctx.clientModules.graph().entries.some(entry => entry.id.endsWith('/wake-tasks')), false, '任务子入口不重复注册浏览器模块')
  const llm = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm')).href)
  let requests = 0
  const adapter = new class extends llm.LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model } }
    async *stream() {
      requests++
      const id = llm.ToolCallId(`terminal-${requests}`)
      yield { type: 'block-start', index: 0, blockType: 'tool-call' }
      yield { type: 'tool-call-delta', index: 0, id, name: 'return_shadow_result', argumentsDelta: '{"text":"组合测试完成"}' }
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name: 'return_shadow_result', arguments: '{"text":"组合测试完成"}' } }
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
    }
  }()
  ctx.llm.registerAdapter(['mock'], adapter)
  const origin = `http://127.0.0.1:${ctx.webServer.port}`
  const api = async (path, body, method = body ? 'POST' : 'GET') => {
    const response = await fetch(`${origin}${path}`, { method, headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) })
    const value = await response.json()
    assert.ok(response.ok, `${path} ${response.status} ${JSON.stringify(value)}`)
    return value
  }
  const prefix = '/api/voice-terminal-shadow/v1/'
  assert.equal((await api(prefix + 'health')).bundle_version, '0.2.0')
  const envelope = () => ({ protocol_version: '2.0', request_id: randomUUID(), voice_session_id: 'voice-test', dsh_session_id: 'dsh-loader-test', epoch: 1 })
  assert.equal((await api(prefix + 'session.open', envelope())).result, 'accepted')
  const agent = ctx.agents.list().find(value => value.id === 'dsh-loader-test')
  assert.ok(agent, '完整官方预设创建代理成功')
  assert.equal((await api(prefix + 'wake', { ...envelope(), activation_id: 'activation-test', context_revision: 0 })).result, 'accepted')
  await agent.whenIdle()
  assert.equal(requests, 1)
  assert.ok(received.some(value => value.body.event?.text === '组合测试完成'))
  const end = agent.session.snapshotEvents().findLast(event => event.type === 'turn/end')
  assert.equal(end.data.reason.kind, 'completed')
  const task = await api('/api/voice-terminal-wake/v1/tasks', { when: '1h', text: '保留的任务', creation_source: 'manual' })
  assert.equal(task.status, 'pending')
  assert.equal((await api('/api/voice-terminal-wake/v1/tasks')).tasks.length, 1)
  assert.equal((await api(prefix + 'session.retire', envelope())).result, 'accepted', '根代理取消、持久化与回调释放后确认解绑')
  assert.equal((await api(prefix + 'session.retire', envelope())).result, 'duplicate', '重复解绑保持幂等')
  const entries = []
  function walk(tree) { for (const entry of Object.values(tree.store)) { entries.push(entry); if (entry.subtree) walk(entry.subtree) } }
  walk(ctx.loader)
  for (const id of ['voice-terminal-shadow-bridge', 'voice-terminal-wake-tasks', 'preset-shadow-agent']) {
    assert.equal(entries.filter(entry => entry.options.id === id || entry.id === id).length, 1, `${id} 只加载一次`)
  }
  // 通过 Loader（加载器）拥有的 Fiber（生命周期）释放组件，验证无遗留代理或路由。
  const wake = entries.find(entry => entry.options.id === 'voice-terminal-wake-tasks' || entry.id === 'voice-terminal-wake-tasks')
  await wake.fiber.dispose()
  assert.ok([401, 404].includes((await fetch(origin + '/api/voice-terminal-wake/v1/tasks', { headers: { authorization: `Bearer ${token}` } })).status))
  const stored = JSON.parse(await readFile(process.env.VOICE_DSH_TASKS_PATH, 'utf8'))
  assert.equal(stored.tasks[0].task_id, task.task_id)
  const bridge = entries.find(entry => entry.options.id === 'voice-terminal-shadow-bridge' || entry.id === 'voice-terminal-shadow-bridge')
  await bridge.fiber.dispose()
  assert.equal(ctx.agents.list().length, 0, '组件卸载后已创建代理由官方所有者释放')
  assert.ok([401, 404].includes((await fetch(origin + prefix + 'health', { headers: { authorization: `Bearer ${token}` } })).status))
  console.log('LOADER_COMPOSITION_PASSED')
} finally {
  await booted?.shutdown.shutdown(0)
  await new Promise(resolveClose => callback.close(resolveClose))
  process.chdir(repo)
  // temporary（临时根目录）来自固定前缀 mkdtemp（独占临时目录创建），从未复用用户路径。
  await rm(temporary, { recursive: true, force: true })
}

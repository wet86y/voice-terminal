#!/usr/bin/env node
// Isolated target-build smoke test（隔离目标构建冒烟检查）: no provider, microphone, or production task store.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { cp, mkdir, mkdtemp, readFile, rm, symlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { randomUUID } from 'node:crypto'
import vm from 'node:vm'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..') // root（工作区根目录）
const dshRoot = join(root, '.runtime', 'dsh') // dshRoot（便携 DSH 运行目录）
const sourceRoot = join(dshRoot, 'source') // sourceRoot（DSH 源码目录）
const buildStatePath = join(dshRoot, 'build-state.json') // buildStatePath（构建状态文件）
const expectedTag = 'dsh-v0.2.0-rc.2' // expectedTag（预期上游版本标签）
const expectedCommit = '639ed015397290b3745d163aafe02ffee4aa3f84' // expectedCommit（预期上游提交号）
const expectedVersion = '0.2.0-rc.2' // expectedVersion（预期上游版本号）

function listen(server) { // listen（绑定随机回环端口）
  return new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', () => {
      server.off('error', rejectListen)
      resolveListen(server.address().port)
    })
  })
}

function close(server) { // close（关闭本脚本创建的 HTTP 服务）
  return new Promise((resolveClose, rejectClose) => {
    if (!server.listening) return resolveClose()
    server.close(error => error ? rejectClose(error) : resolveClose())
  })
}

function json(res, status, value) { // json（写出测试 HTTP JSON 响应）
  const body = JSON.stringify(value)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) })
  res.end(body)
}

async function readJson(req) { // readJson（读取模拟 Voice 回调请求体）
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

async function waitFor(predicate, label) { // waitFor（限时等待异步状态）
  const deadline = Date.now() + 5000
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise(resolveDelay => setTimeout(resolveDelay, 10))
  }
  throw new Error(`超时等待：${label}`)
}

async function importResolved(requireFrom, specifier) { // importResolved（由指定 package.json 解析并导入包）
  return import(pathToFileURL(requireFrom.resolve(specifier)).href)
}

function toolCallResponse(ToolCallId, rawId, name, args) { // toolCallResponse（构造真实 DSH LLM 工具调用流）
  const id = ToolCallId(rawId) // id（工具调用标识）
  const argumentsJson = JSON.stringify(args) // argumentsJson（JSON 编码的工具参数）
  return [
    { type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 0, id, name, argumentsDelta: argumentsJson.slice(0, 7) },
    { type: 'tool-call-delta', index: 0, id, argumentsDelta: argumentsJson.slice(7) },
    { type: 'block-end', index: 0, block: { type: 'tool-call', id, name, arguments: argumentsJson } },
    { type: 'usage', usage: { inputTokens: 1, outputTokens: 1 } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ]
}

function createScriptedMockAdapter(LlmAdapter, script) { // createScriptedMockAdapter（构造 DSH 官方基类上的本地模拟适配器）
  return new (class extends LlmAdapter {
    requests = [] // requests（实际收到的模型请求，用于断言重试和继续执行）
    constructor() {
      super()
      this.script = [...script] // script（预先固定的工具调用脚本）
    }
    resolveModel(provider, model) { // resolveModel（返回模拟模型路由，不访问外部供应方）
      return Promise.resolve({ provider, id: model, name: model })
    }
    async *stream(options) { // stream（回放一段真实格式的 DSH 流事件）
      this.requests.push(options)
      const chunks = this.script.shift()
      if (!chunks) throw new Error('mock 流脚本已耗尽')
      for (const chunk of chunks) {
        if (options.signal?.aborted) throw new Error('mock 流已取消')
        yield chunk
      }
    }
  })()
}

async function testConfigFormsWhileServed(Context, uiSettingsBundle, bridgeClientBundle) { // testConfigFormsWhileServed（验证真实编译客户端的设置服务生命周期）
  const clientCtx = new Context() // clientCtx（隔离的客户端 Cordis 上下文）
  let namespaces = [] // namespaces（模拟 Host 当前提供的设置命名空间）
  const invalidationListeners = new Set() // invalidationListeners（设置文档失效通知监听器）
  const remote = {
    $host: { isLoopback: true }, // isLoopback（标识此为本机 Host 持久化）
    $on(event, listener) { // $on（订阅编译客户端的设置文档更新事件）
      assert.equal(event, 'settings/document-updated')
      invalidationListeners.add(listener)
      return () => invalidationListeners.delete(listener)
    },
    settings: {
      async describe() { // describe（返回模拟 Host 所服务的命名空间清单）
        return { ok: true, value: {
          writable: true, hasDocument: true,
          namespaces: namespaces.map(ns => ({ ns, schema: {}, value: {}, applies: 'live', secrets: [], revision: 1 })),
        } }
      },
    },
    session: { async modelCatalog() { return { ok: true, value: { groups: [] } } } },
  }
  const slotEntries = new Map() // slotEntries（编译客户端注册的 UI 插槽贡献）
  const slotDisposals = [] // slotDisposals（UI 插槽撤销次数）
  const slots = {
    inject(_name, register) { return register() },
    register(entry, component) {
      const key = entry.id ?? entry.key ?? `${entry.name}:${slotEntries.size}`
      slotEntries.set(key, { entry, component })
      return () => { slotEntries.delete(key); slotDisposals.push(key) }
    },
  }
  const locale = {
    register() { return () => {} },
    bind() { return key => key },
  }
  clientCtx.provide('remote', remote)
  clientCtx.provide('slots', slots)
  clientCtx.provide('locale', locale)
  clientCtx.provide('workspaces', { list: { getSnapshot: () => ({ rows: [] }), subscribe: () => () => {} } })
  clientCtx.provide('uiConversation', { events: { register: () => () => {} } })

  uiSettingsBundle.apply(clientCtx) // apply（执行 DSH 实际编译的 configForms 设置服务插件）

  bridgeClientBundle.apply(clientCtx) // apply（执行实际编译的 Bridge 客户端插件代码）
  const configForms = clientCtx.get('configForms') // configForms（由真实编译设置插件提供的设置表单服务）
  assert.ok(configForms, 'Bridge 客户端应从 DSH ConfigForms 服务获取设置接口')
  await configForms.describe().ensure() // describe（读取 Host 命名空间镜像）
  assert.equal([...slotEntries.values()].filter(row => row.entry.id === 'voice-terminal').length, 0,
    '设置命名空间未被 Host 提供时不得注册 Voice Terminal 页面')

  const invalidate = async next => {
    namespaces = next
    for (const listener of invalidationListeners) listener()
    const wanted = new Set(next)
    await waitFor(() => {
      const served = new Set(configForms.describe().getSnapshot().view?.namespaces.map(row => row.ns) ?? [])
      return [...wanted].every(ns => served.has(ns)) && [...served].every(ns => wanted.has(ns))
    }, `configForms 服务命名空间 ${[...wanted].join(',')}`)
  }

  await invalidate(['voice-terminal-shadow-bridge'])
  assert.equal([...slotEntries.values()].filter(row => row.entry.id === 'voice-terminal').length, 1,
    `Host 提供命名空间后应注册一个设置页；entries=${JSON.stringify([...slotEntries.keys()])}; mirror=${JSON.stringify(configForms.describe().getSnapshot())}`)
  await invalidate(['another-plugin'])
  assert.equal([...slotEntries.values()].filter(row => row.entry.id === 'voice-terminal').length, 0,
    'Host 撤下命名空间后应释放设置页')
  assert.equal(slotDisposals.filter(key => key === 'voice-terminal').length, 1,
    'whileServed（服务期间注册）撤下时应调用一次 disposer（释放函数）')

  await invalidate(['voice-terminal-shadow-bridge'])
  assert.equal([...slotEntries.values()].filter(row => row.entry.id === 'voice-terminal').length, 1)
  await clientCtx.fiber.dispose()
  assert.equal([...slotEntries.values()].filter(row => row.entry.id === 'voice-terminal').length, 0,
    'Bridge 客户端卸载时应释放活动设置页')
  assert.equal(slotDisposals.filter(key => key === 'voice-terminal').length, 2)
  return { registrations: 2, disposals: 2, servedGuard: true } // servedGuard（仅 Host 服务该设置项时才显示）
}

async function loadBrowserBundle(filePath, moduleId, requireMap) { // loadBrowserBundle（在隔离 VM 中加载 DSH 编译客户端包）
  const source = await readFile(filePath, 'utf8') // source（实际构建产物内容）
  const loaded = new Map() // loaded（ModuleLoader 加载结果）
  const fakeRequire = specifier => {
    if (!requireMap.has(specifier)) throw new Error(`浏览器 smoke 缺少 require 映射：${specifier}`)
    return requireMap.get(specifier)
  }
  const window = { __ModuleLoader__: { load(definition) { loaded.set(definition.id, definition.factory(fakeRequire)) } } }
  vm.runInNewContext(source, { window, document: undefined, console, Buffer, setTimeout, clearTimeout }, { filename: filePath })
  const result = loaded.get(moduleId)
  assert.ok(result, `编译客户端未注册模块 ${moduleId}`)
  return result
}

async function main() {
  const buildState = JSON.parse(await readFile(buildStatePath, 'utf8')) // buildState（目标宿主构建身份）
  assert.equal(buildState.tag, expectedTag, '运行前确认当前源码为已批准目标 tag（版本标签）')
  assert.equal(buildState.commit, expectedCommit, '运行前确认当前源码为已批准 commit（提交号）')
  assert.equal(buildState.version, expectedVersion, '运行前确认当前源码版本号')

  const oldCwd = process.cwd() // oldCwd（运行前工作目录）
  const tempData = await mkdtemp(join(tmpdir(), 'dsh-upgrade-smoke-')) // tempData（本次测试独占临时数据目录）
  const oldBridgeToken = process.env.VOICE_DSH_BRIDGE_TOKEN // oldBridgeToken（需恢复的进程环境值，不打印）
  const oldCallbackOrigin = process.env.VOICE_DSH_CALLBACK_ORIGIN // oldCallbackOrigin（需恢复的回调地址，不打印）
  const oldFetch = globalThis.fetch // oldFetch（需恢复的 fetch 函数）
  let context // context（真实 DSH AgentLoop 测试上下文）
  let terminalCalls = [] // terminalCalls（模拟 Voice 收到的终态事件摘要，不含令牌）
  let callbackServer // callbackServer（本脚本创建的假 Voice 回调服务）
  let hostServer // hostServer（随机回环端口上的 DSH Bridge 测试服务）
  let hostRoute // hostRoute（Bridge 插件注册的实际 HTTP 路由）
  const smokeToken = `smoke-${randomUUID()}` // smokeToken（只在进程内使用的临时认证令牌）

  try {
    process.chdir(tempData)
    callbackServer = createServer(async (req, res) => {
      try {
        assert.equal(req.headers.authorization, `Bearer ${smokeToken}`, '假 Voice 只接受本次随机测试令牌')
        const body = await readJson(req) // body（Bridge 回调 JSON 字段）
        if (req.url === '/api/internal/dsh-bridge/shadow.event') {
          const event = body.event ?? {}
          if (event.type === 'result') {
            terminalCalls.push({ type: event.type, text: event.text })
            if (event.text === 'smoke callback failure') {
              json(res, 503, { status: 'unavailable' })
              return
            }
            json(res, 200, { result: event.text === 'smoke duplicate' ? 'duplicate' : 'accepted' })
            return
          }
        }
        json(res, 200, { result: 'accepted' })
      } catch {
        json(res, 500, { status: 'smoke-callback-error' })
      }
    })
    const callbackPort = await listen(callbackServer) // callbackPort（假 Voice 服务的随机回环端口）
    const callbackOrigin = `http://127.0.0.1:${callbackPort}` // callbackOrigin（仅 loopback 的模拟回调地址）
    process.env.VOICE_DSH_BRIDGE_TOKEN = smokeToken
    process.env.VOICE_DSH_CALLBACK_ORIGIN = callbackOrigin

    // Fail closed on any accidental non-loopback fetch（拦截任何意外外部网络请求）.
    globalThis.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url)
      assert.equal(url.hostname, '127.0.0.1', '冒烟脚本不得连接外部模型或服务')
      assert.ok(url.port === String(callbackPort) || (hostServer && url.port === String(hostServer.address().port)),
        '冒烟脚本 fetch 只能访问本地 Bridge 或假 Voice 端口')
      return oldFetch(input, init)
    }

    const cliRequire = createRequire(join(sourceRoot, 'apps', 'cli', 'package.json')) // cliRequire（解析已安装 DSH 运行包）
    const registryRequire = createRequire(join(sourceRoot, 'packages', 'preset', 'agent-preset-registry', 'package.json')) // registryRequire（解析 preset 测试包）
    const browserRequire = createRequire(join(sourceRoot, 'apps', 'web', 'package.json')) // browserRequire（解析已构建浏览器依赖）
    const cordis = await importResolved(cliRequire, '@deepseek-ai/cordis')
    const llm = await importResolved(cliRequire, '@deepseek-ai/dsh-llm')
    const session = await importResolved(cliRequire, '@deepseek-ai/dsh-session')
    const SessionProjectionRegistry = (await importResolved(cliRequire, '@deepseek-ai/dsh-session-projection')).default
    const SystemPrompt = (await importResolved(cliRequire, '@deepseek-ai/dsh-system-prompt')).default
    const ToolRuntime = (await importResolved(cliRequire, '@deepseek-ai/dsh-tools')).default
    const AgentRegistry = (await importResolved(cliRequire, '@deepseek-ai/dsh-agent')).default
    const AgentLoop = (await importResolved(cliRequire, '@deepseek-ai/dsh-agent-loop')).default
    const Loader = (await importResolved(cliRequire, '@deepseek-ai/cordis-plugin-loader')).default
    const SessionStore = session.default
    const { Context } = cordis

    const tempModules = join(tempData, 'node_modules') // tempModules（本次测试专属的 Node 模块查找目录）
    const tempBridgeRoot = join(tempModules, '@voice-terminal', 'dsh-plugin') // tempBridgeRoot（临时只读副本的 Bridge 包目录）
    await mkdir(join(tempModules, '@voice-terminal'), { recursive: true })
    await symlink(join(sourceRoot, 'python', 'sdk-runtime', 'node_modules', '@deepseek-ai'), join(tempModules, '@deepseek-ai'), 'junction')
    // 手工 Context（上下文）不启用官方配置启动的依赖解析回退；测试副本显式链接公开工作区包。
    await mkdir(join(tempBridgeRoot, 'node_modules', '@deepseek-ai'), { recursive: true })
    await symlink(join(sourceRoot, 'packages', 'workspace', 'workspace'), join(tempBridgeRoot, 'node_modules', '@deepseek-ai', 'dsh-workspace'), 'junction')
    await cp(join(dshRoot, 'bundles', 'voice-terminal'), tempBridgeRoot, { recursive: true })
    const bridgePluginPath = join(tempBridgeRoot, 'lib', 'bridge', 'plugin.js') // bridgePluginPath（Bridge 编译后宿主入口）
    const bridgeModule = await import(pathToFileURL(bridgePluginPath).href)

    const adapter = createScriptedMockAdapter(llm.LlmAdapter, [
      toolCallResponse(llm.ToolCallId, 'smoke-call-accepted', 'return_shadow_result', { text: 'smoke accepted' }),
      toolCallResponse(llm.ToolCallId, 'smoke-call-duplicate', 'return_shadow_result', { text: 'smoke duplicate' }),
      toolCallResponse(llm.ToolCallId, 'smoke-call-failure', 'return_shadow_result', { text: 'smoke callback failure' }),
      toolCallResponse(llm.ToolCallId, 'smoke-call-retry', 'return_shadow_result', { text: 'smoke retry accepted' }),
    ])

    context = new Context()
    const fakeSettingsState = new Map() // fakeSettingsState（隔离的设置写入记录）
    const fakeWorkspace = {
      id: `smoke-workspace-${randomUUID()}`, // id（合成工作区标识）
      path: join(tempData, 'workspace'), // path（临时工作区路径）
      async status() { return 'ok' },
      async attachSession() {},
    }
    context.provide('webServer', {
      register(route) { hostRoute = route; return () => { if (hostRoute === route) hostRoute = undefined } },
    })
    context.provide('workspaceRegistry', {
      async resolveByPath() { return fakeWorkspace },
      async create() { return fakeWorkspace },
      get(id) { return id === String(fakeWorkspace.id) ? fakeWorkspace : undefined },
    })
    context.provide('settings', {
      async update(namespace, patch) { fakeSettingsState.set(namespace, { ...(fakeSettingsState.get(namespace) ?? {}), ...patch }) },
      configure() { return () => {} },
    })
    context.provide('agentDefaultModel', { currentSelection: () => ({ provider: 'mock', model: 'local-smoke' }) })
    context.provide('sessionTitle', { rename() {} })
    let presetMountCalls = 0 // presetMountCalls（测试用预设挂载服务被调用次数）
    context.provide('agentPresets', {
      async resolve(id) { return { id } },
      async mount(_agentContext, id) { presetMountCalls += 1; return { id } },
    })

    await context.plugin(Loader).await()
    await context.plugin(llm.default).await()
    await context.plugin(SessionStore).await()
    await context.plugin(SessionProjectionRegistry).await()
    await context.plugin(SystemPrompt, { personaPrefix: '' }).await()
    await context.plugin(ToolRuntime).await()
    await context.plugin(AgentRegistry).await()
    await context.plugin(AgentLoop, { agents: [] }).await()
    context.llm.registerAdapter(['mock'], adapter) // registerAdapter（仅注册本地模拟模型）

    hostServer = createServer((req, res) => {
      const pathname = new URL(req.url ?? '/', 'http://127.0.0.1').pathname
      if (!hostRoute || !pathname.startsWith(`${hostRoute.path}/`) && pathname !== hostRoute.path) {
        json(res, 404, { result: 'not_found' })
        return
      }
      void hostRoute.handler(req, res)
    })
    const hostPort = await listen(hostServer) // hostPort（Bridge 宿主随机回环端口）
    const hostOrigin = `http://127.0.0.1:${hostPort}` // hostOrigin（只对本机可访问的 Bridge 地址）

    await context.plugin({
      name: bridgeModule.name, // name（已构建 Bridge 宿主插件名）
      inject: bridgeModule.inject, // inject（Bridge 服务依赖声明）
      Config: bridgeModule.Config,
      apply: bridgeModule.apply,
    }, {
      routePrefix: '/api/dsh-upgrade-smoke', // routePrefix（隔离冒烟路由前缀）
      maxBodyBytes: 131072, // maxBodyBytes（请求体上限）
      workspaceId: '', // workspaceId（所选工作区设置字段）
      defaultWorkspaceId: '', // defaultWorkspaceId（默认工作区设置字段）
      provider: '', // provider（模型供应方设置字段；空值表示沿用测试默认）
      model: '', // model（模型标识设置字段；空值表示沿用测试默认）
      reasoningEffort: '', // reasoningEffort（推理强度设置字段）
      fallbackNotice: '', // fallbackNotice（回退提示设置字段）
    }).await()

    const smokeFetch = async (path, body) => {
      const response = await fetch(`${hostOrigin}/api/dsh-upgrade-smoke/${path}`, {
        method: body ? 'POST' : 'GET',
        headers: { authorization: `Bearer ${smokeToken}`, ...(body ? { 'content-type': 'application/json' } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
      })
      const value = await response.json()
      assert.ok(response.ok, `Bridge HTTP ${path} 返回 ${response.status}: ${JSON.stringify(value)}`)
      return value
    }

    const health = await smokeFetch('health') // health（Bridge 健康状态响应）
    assert.equal(health.status, 'ok')
    assert.equal(health.bundle_version, '0.2.0', '确认运行的是统一插件构建产物')
    assert.equal(health.protocol_version, '2.0')

    const voiceSessionId = `voice-${randomUUID()}` // voiceSessionId（合成 Voice 会话标识）
    const dshSessionId = `dsh-smoke-${randomUUID()}` // dshSessionId（仅内存中的合成 DSH 会话标识）
    const envelope = requestId => ({ // envelope（Bridge 请求信封，所有字段均为模拟数据）
      protocol_version: '2.0', // protocol_version（协议版本）
      request_id: requestId, // request_id（幂等请求标识）
      voice_session_id: voiceSessionId, // voice_session_id（Voice 会话标识）
      dsh_session_id: dshSessionId, // dsh_session_id（DSH 会话标识）
      epoch: 1, // epoch（会话世代号）
    })
    const opened = await smokeFetch('session.open', envelope(randomUUID())) // session.open（打开隔离 Agent 会话）
    assert.equal(opened.result, 'accepted')
    const agent = context.agents.get(dshSessionId) // agent（真实 AgentLoop 创建的智能体）
    assert.ok(agent, 'Bridge 必须通过真实 DSH AgentRegistry/AgentLoop 创建 Agent')
    assert.equal(presetMountCalls, 1, 'Bridge session.open 必须经过 preset mount（预设挂载）服务')

    const endReasons = () => agent.session.snapshotEvents()
      .filter(event => event.type === 'turn/end')
      .map(event => event.data.reason)
    const wakeAndWait = async (activationId, priorEnds) => {
      const response = await smokeFetch('wake', {
        ...envelope(randomUUID()), // request_id（每次唤醒使用新的请求标识）
        context_revision: 0, // context_revision（本测试没有 Voice 上下文帧）
        activation_id: activationId, // activation_id（合成激活标识）
      })
      assert.equal(response.result, 'accepted')
      await waitFor(() => agent.status === 'idle' && endReasons().length > priorEnds, `激活 ${activationId} 的 AgentLoop 回合结束`)
    }

    await wakeAndWait(`activation-${randomUUID()}`, 0)
    assert.deepEqual(endReasons()[0], { kind: 'completed' }, 'accepted（首次接受）终态工具应以 completed（已完成）结束回合')

    await wakeAndWait(`activation-${randomUUID()}`, 1)
    assert.deepEqual(endReasons()[1], { kind: 'completed' }, 'duplicate（重复确认）终态工具也应以 completed（已完成）结束回合')

    await wakeAndWait(`activation-${randomUUID()}`, 2)
    const sessionEvents = agent.session.snapshotEvents() // sessionEvents（Agent 持久事件投影）
    const toolErrors = sessionEvents.filter(event => event.type === 'tool/result'
      && JSON.stringify(event.data).includes('callback rejected (503)'))
    assert.ok(toolErrors.length >= 1, 'callback failure（回调失败）应作为工具错误记录')
    assert.equal(adapter.requests.length, 4,
      '回调失败后 AgentLoop 必须继续请求模型并执行重试终态工具，不能过早结束回合')
    assert.deepEqual(endReasons(), [
      { kind: 'completed' }, { kind: 'completed' }, { kind: 'completed' },
    ], 'accepted/duplicate/failure-retry 三个激活都必须有明确的非空 turn/end.reason（回合结束原因）')
    assert.deepEqual(terminalCalls.map(item => item.text), [
      'smoke accepted', 'smoke duplicate', 'smoke callback failure', 'smoke retry accepted',
    ])
    assert.equal(presetMountCalls, 1)

    // Verify the real built browser settings and Bridge modules against a local settings mirror（验证实际编译浏览器包的设置服务注册/释放）.
    const browserCordis = cordis // browserCordis（与测试上下文相同的 Cordis 模块）
    const storeModulePath = browserRequire.resolve('@deepseek-ai/dsh-client-store') // storeModulePath（浏览器状态库编译入口）
    const storeModule = await import(pathToFileURL(storeModulePath).href)
    const clientRequireMap = new Map([
      ['@deepseek-ai/cordis', browserCordis],
      ['@deepseek-ai/dsh-client-store', storeModule],
      ['react', new Proxy({}, { get: () => () => undefined })],
      ['react/jsx-runtime', { jsx: () => ({}), jsxs: () => ({}) }],
      ['@deepseek-ai/dsh-client-ui-primitives', new Proxy({}, { get: () => function StubComponent() {} })],
    ])
    const uiSettingsBundle = await loadBrowserBundle(
      join(sourceRoot, 'packages', 'client', 'ui-settings', 'lib', 'client.js'),
      '@deepseek-ai/dsh-client-ui-settings', clientRequireMap,
    )
    const bridgeClientBundle = await loadBrowserBundle(
      join(dshRoot, 'bundles', 'voice-terminal', 'lib', 'client.js'),
      '@voice-terminal/dsh-plugin', clientRequireMap,
    )
    const configFormsResult = await testConfigFormsWhileServed(Context, uiSettingsBundle, bridgeClientBundle)

    console.log(JSON.stringify({
      result: 'passed', // result（总体验收结果）
      upstream: { tag: buildState.tag, version: buildState.version, commit: buildState.commit }, // upstream（上游构建身份）
      bridge: { bundle_version: health.bundle_version, protocol_version: health.protocol_version }, // bridge（桥接包版本与协议版本）
      terminal_tool: { accepted: 'completed', duplicate: 'completed', callback_failure: 'continued_then_completed', explicit_turn_end_reasons: endReasons().length }, // terminal_tool（终态工具行为）
      mock_provider_requests: adapter.requests.length, // mock_provider_requests（本地模拟模型请求数）
      config_forms: configFormsResult, // config_forms（设置表单服务注册/释放结果）
      preset: 'Bridge resolve/mount hook invoked; full shadow preset composition is covered separately in desktop validation', // preset（桥接挂载钩子已调用；完整预设由桌面验收覆盖）
      network: 'loopback only; no external model/provider requests', // network（仅本机回环，不访问外部模型/供应方）
      data: 'temporary directory only; no production task/session store or task delivery', // data（只用临时目录，不读写生产任务/会话数据，也不投递任务）
    }, null, 2))
  } finally {
    try { await context?.fiber?.dispose() } catch {}
    try { await close(hostServer) } catch {}
    try { await close(callbackServer) } catch {}
    process.chdir(oldCwd)
    globalThis.fetch = oldFetch
    if (oldBridgeToken === undefined) delete process.env.VOICE_DSH_BRIDGE_TOKEN
    else process.env.VOICE_DSH_BRIDGE_TOKEN = oldBridgeToken
    if (oldCallbackOrigin === undefined) delete process.env.VOICE_DSH_CALLBACK_ORIGIN
    else process.env.VOICE_DSH_CALLBACK_ORIGIN = oldCallbackOrigin
    await rm(tempData, { recursive: true, force: true })
  }
}

main().catch(error => {
  console.error(`DSH upgrade smoke failed（DSH 升级冒烟失败）: ${error instanceof Error ? error.stack : String(error)}`)
  process.exitCode = 1
})

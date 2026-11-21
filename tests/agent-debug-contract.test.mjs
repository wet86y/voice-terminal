/** 自动兼容契约：执行正式服务适配器，验证新增接口无需逐项调试适配。 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const root = new URL('../', import.meta.url)
const source = await readFile(new URL('apps/desktop/src-tauri/src/agent_debug/service.js', root), 'utf8')
const plain = value => JSON.parse(JSON.stringify(value))

function service({ remote = false, address = 'http://127.0.0.1:8765', schema, host = [], result } = {}) {
  const requests = [], sockets = []
  class Socket {
    constructor(url) { this.url = String(url); sockets.push(this); queueMicrotask(() => this.onopen()) }
    send(text) {
      this.message = JSON.parse(text)
      queueMicrotask(() => this.onmessage({ data: JSON.stringify({ request_id: this.message.request_id, payload: { accepted: true } }) }))
    }
    close() { this.closed = true }
  }
  const call = vm.runInNewContext('(' + source + ')', {
    __VOICE_APPLICATION__: { remote_mode: remote, business_ready: true, voice_api_url: address },
    location: { origin: 'http://127.0.0.1:5173' }, URL, AbortSignal, WebSocket: Socket,
    setTimeout, clearTimeout,
    fetch: async (target, init = {}) => {
      const url = new URL(target, 'http://127.0.0.1:5173')
      requests.push({ url: String(url), ...init })
      if (url.pathname === '/api/v1/application/operations') return Response.json({ operations: host })
      if (url.pathname === '/openapi.json') return Response.json(schema ?? { paths: {} })
      return result ? result(url, init) : Response.json({ accepted: true })
    },
  })
  return { call: async request => plain(await call(request)), requests, sockets }
}

const settingsSchema = () => ({
  paths: { '/api/v1/settings/{panel}': { put: {
    summary: '新增设置接口', parameters: [{ in: 'path', name: 'panel', required: true, schema: { type: 'string' } }],
    requestBody: { content: { 'application/json': { schema: { $ref: '#/components/schemas/NewSettings' } } } },
  } } },
  components: { schemas: { NewSettings: { type: 'object', properties: {
    newly_added: { type: 'object', description: '新增设置字段', properties: { enabled: { type: 'boolean', description: '启用状态' } } },
  } } } },
  'x-voice-commands': ['new.production.command'],
})

test('新增正式路由、请求字段及引用约束自动进入同一目录，后续新增也自动出现', async () => {
  const schema = settingsSchema()
  const { call } = service({ schema, host: [{ method: 'POST', path: '/api/v1/application/widgets/{widget}/apply' }] })
  const directory = await call(null)
  assert.deepEqual(directory.operations.map(value => value.operation), [
    'POST /api/v1/application/widgets/{widget}/apply', 'PUT /api/v1/settings/{panel}',
  ])
  assert.deepEqual(directory.schemas, schema.components.schemas)
  assert.equal(directory.operations[1].request_body.content['application/json'].schema.$ref, '#/components/schemas/NewSettings')
  assert.deepEqual(directory.realtime_operations, ['WS /ws:new.production.command'])
  schema.paths['/api/v1/new-feature'] = { post: { summary: '后续新增业务' } }
  assert.ok((await call(null)).operations.some(value => value.operation === 'POST /api/v1/new-feature'))
})

test('新增设置对象由正式路由处理，调试适配器不拆解字段或改写业务回执', async () => {
  const { call, requests } = service({ schema: settingsSchema(), result: (_url, init) => Response.json({ saved: JSON.parse(init.body) }) })
  const body = { newly_added: { enabled: true, future_fields: ['新值', 7] } }
  const response = await call({ operation: 'PUT /api/v1/settings/{panel}', call_id: 28,
    arguments: { path: { panel: '新面板' }, query: { refresh: true }, body } })
  assert.deepEqual(response.data.saved, body)
  const sent = requests.at(-1)
  assert.equal(new URL(sent.url).pathname, '/api/v1/settings/%E6%96%B0%E9%9D%A2%E6%9D%BF')
  assert.equal(new URL(sent.url).searchParams.get('refresh'), 'true')
  assert.equal(sent.method, 'PUT')
  assert.equal(sent.headers['X-Voice-Agent-Call'], '28')
})

test('正式协议枚举中的新增实时命令自动调用，并匹配正式请求编号', async () => {
  const { call, sockets } = service({ schema: settingsSchema() })
  const result = await call({ operation: 'WS /ws:new.production.command', call_id: 29, arguments: { newly_added: true } })
  assert.equal(result.state, 'received')
  assert.equal(result.event.request_id, 'agent-29')
  assert.deepEqual(sockets[0].message.payload, { newly_added: true })
  assert.equal(sockets[0].closed, true)
})

test('自动发现保持正式目录、同源、路径参数及远端目标的边界', async () => {
  const local = service({ schema: settingsSchema() })
  assert.ok((await local.call({ operation: 'POST /api/v1/undeclared', arguments: {}, call_id: 30 })).error)
  assert.equal(local.requests.length, 2) // 仅正式目录发现，不发送未登记业务。
  await assert.rejects(local.call({ operation: 'PUT /api/v1/settings/{panel}', arguments: { path: { panel: '../other' } }, call_id: 31 }), /路径参数无效/)
  const unsafe = service({ address: 'https://external.invalid', schema: settingsSchema() })
  assert.deepEqual((await unsafe.call(null)).operations, [])
  assert.ok(unsafe.requests.every(request => new URL(request.url).origin === 'http://127.0.0.1:5173'))
  const remote = service({ remote: true, schema: settingsSchema() })
  assert.ok((await remote.call({ operation: 'PUT /api/v1/settings/{panel}', arguments: {}, call_id: 32 })).error)
  assert.equal(remote.requests.length, 0)
})

function bypasses(source) {
  const found = []
  const ast = ts.createSourceFile('feature.ts', source, ts.ScriptTarget.Latest, true)
  function visit(node) {
    if (ts.isTypeNode(node) || ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node)) return
    if (ts.isImportDeclaration(node) && node.importClause?.isTypeOnly) return
    if ((ts.isIdentifier(node) || ts.isStringLiteral(node)) && node.text === '__TAURI_INTERNALS__') found.push('直接访问原生桥')
    if (ts.isStringLiteral(node) && node.text.startsWith('@tauri-apps/api/core')) found.push('绕过共用原生命令调用器')
    ts.forEachChild(node, visit)
  }
  visit(ast)
  return found
}

test('调用器约束识别真实绕过，允许类型声明及共用入口', () => {
  assert.equal(bypasses("const call = window.__TAURI_INTERNALS__; call.invoke('save')").length, 1)
  assert.equal(bypasses("import { invoke } from '@tauri-apps/api/core'").length, 1)
  assert.deepEqual(bypasses("interface Native { __TAURI_INTERNALS__?: unknown }; import type { InvokeArgs } from '@tauri-apps/api/core'"), [])
  assert.deepEqual(bypasses("import { locateNativeInvoker } from './runtime/desktop-preflight'; locateNativeInvoker()?.invoke('new_command')"), [])
})

test('现有及后续前端业务自动检查共用调用器，无需逐页面维护调试白名单', async () => {
  const base = new URL('apps/frontend/src/', root)
  // 两处基础设施分别提供受控调用器及官方事件订阅，业务模块直接复用。
  const providers = new Set(['runtime/desktop-preflight.ts', 'product/desktop-port.ts'])
  let checked = 0
  async function inspect(directory, relative = '') {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const name = relative + entry.name
      const url = new URL(entry.name + (entry.isDirectory() ? '/' : ''), directory)
      if (entry.isDirectory()) { await inspect(url, name + '/'); continue }
      if (!/\.(ts|vue)$/.test(name) || /\.(test|d)\.ts$/.test(name) || providers.has(name)) continue
      const text = await readFile(url, 'utf8')
      const scripts = name.endsWith('.vue') ? (() => {
        const { descriptor, errors } = parse(text, { filename: fileURLToPath(url) })
        assert.deepEqual(errors, [])
        return [descriptor.script?.content ?? '', descriptor.scriptSetup?.content ?? '']
      })() : [text]
      for (const script of scripts) assert.deepEqual(bypasses(script), [], `${name}：业务使用 locateNativeInvoker（共用调用器）或现有业务端口，自动获得调试关联。`)
      checked++
    }
  }
  await inspect(base)
  assert.ok(checked > 20)
})

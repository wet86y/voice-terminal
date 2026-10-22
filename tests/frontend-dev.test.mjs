/** Desktop gate integration（桌面入口集成测试）：覆盖资源与热更新升级的同一会话边界。 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { createDesktopServer } from '../scripts/frontend-dev.mjs'

const root = fileURLToPath(new URL('../', import.meta.url))
async function request(origin, path, headers = {}) {
  const response = await fetch(origin + path, { headers, redirect: 'manual' })
  return { status: response.status, headers: response.headers, body: await response.text() }
}
function upgrade(origin, headers = {}, path = '/') {
  return new Promise((resolve, reject) => {
    const request = http.request(origin + path, { headers: {
      Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13',
      'Sec-WebSocket-Key': randomBytes(16).toString('base64'), 'Sec-WebSocket-Protocol': 'vite-hmr',
      Origin: origin, ...headers,
    } })
    request.on('response', response => { response.resume(); resolve(response.statusCode) })
    request.on('upgrade', (response, socket) => { socket.destroy(); resolve(response.statusCode) })
    request.on('error', reject)
    request.setTimeout(5000, () => request.destroy(new Error('热更新升级连接超时')))
    request.end()
  })
}

test('浏览器无会话只见说明，桌面凭据单次消费，热更新连接也校验会话', async () => {
  const server = await createDesktopServer({ root, mode: 'ui-preview', port: 0 })
  let fallbackServer
  try {
    const hint = await request(server.origin, '/')
    assert.equal(hint.status, 200)
    assert.match(hint.body, /请通过桌面程序打开/)
    assert.equal(hint.headers.get('set-cookie'), null)
    assert.doesNotMatch(hint.body, /src\/ui-demo|@vite\/client|application-config/)
    for (const path of ['/src/ui-demo/main.ts', '/@vite/client', '/application-config.js', '/__desktop_ready']) {
      assert.equal((await request(server.origin, path)).status, 403)
    }
    assert.equal((await request(server.origin, '/desktop-entry?token=invalid')).status, 403)
    assert.equal((await request(server.origin, `/desktop-entry?token=${server.launchToken}&extra=invalid`)).status, 403)
    assert.equal((await request(server.origin, `/desktop-entry?token=${server.launchToken}&token=${server.launchToken}`)).status, 403)
    const entry = await request(server.origin, `/desktop-entry?token=${server.launchToken}`)
    assert.equal(entry.status, 200)
    assert.match(entry.headers.get('set-cookie'), /HttpOnly; SameSite=Strict/)
    assert.equal(entry.headers.get('referrer-policy'), 'no-referrer')
    assert.equal(entry.headers.get('cache-control'), 'no-store')
    assert.doesNotMatch(entry.body, new RegExp(server.launchToken))
    const Cookie = entry.headers.get('set-cookie').split(';')[0]
    assert.equal((await request(server.origin, `/desktop-entry?token=${server.launchToken}`)).status, 403)
    assert.equal((await request(server.origin, '/ui-demo.html', { Cookie })).status, 200)
    assert.equal((await request(server.origin, '/ui-demo.html', { Cookie })).status, 200) // 刷新沿用本次会话。
    assert.equal((await request(server.origin, '/src/ui-demo/main.ts', { Cookie })).status, 200)
    assert.equal((await request(server.origin, '/src/product/main.ts', { Cookie })).status, 403)
    assert.equal((await request(server.origin, '/application-config.js', { Cookie })).status, 403)
    const client = await request(server.origin, '/@vite/client', { Cookie })
    assert.equal(client.status, 200)
    const token = client.body.match(/const wsToken\s*=\s*["']([^"']+)["']/)?.[1]
    assert.ok(token, '热更新令牌应仅能从受认证客户端资源取得')
    assert.equal(await upgrade(server.origin, {}, `/?token=${token}`), 403)
    assert.equal(await upgrade(server.origin, { Cookie, Origin: 'http://example.com' }, `/?token=${token}`), 403)
    assert.equal(await upgrade(server.origin, { Cookie }, `/?token=${token}`), 101)
    assert.equal((await request(server.origin, '/ui-demo.html', { Cookie: Cookie + '; ' + Cookie })).status, 200)
    assert.match((await request(server.origin, '/ui-demo.html', { Cookie: Cookie + '; ' + Cookie })).body, /请通过桌面程序打开/)
    const firstAddress = new URL(server.origin)
    fallbackServer = await createDesktopServer({ root, mode: 'ui-preview', port: Number(firstAddress.port) })
    const fallbackAddress = new URL(fallbackServer.origin)
    assert.equal(firstAddress.hostname, '127.0.0.1')
    assert.equal(fallbackAddress.hostname, '127.0.0.1')
    const fallbackPort = Number(fallbackAddress.port)
    assert.ok(Number.isInteger(fallbackPort) && fallbackPort >= 1 && fallbackPort <= 65535)
    assert.notEqual(fallbackPort, Number(firstAddress.port))
    assert.equal((await request(fallbackServer.origin, '/')).status, 200)
  } finally {
    try { await fallbackServer?.close() }
    finally { await server.close() }
  }
})

test('样例入口凭据超过60秒不能建立会话', async () => {
  const server = await createDesktopServer({ root, mode: 'ui-preview', port: 0 })
  const clock = Date.now
  try {
    Date.now = () => clock() + 60001
    const entry = await request(server.origin, `/desktop-entry?token=${server.launchToken}`)
    assert.equal(entry.status, 403)
    assert.equal(entry.headers.get('set-cookie'), null)
  } finally { Date.now = clock; await server.close() }
})

test('产品开发要求宿主会话，受控入口及配置由已验证本机宿主提供', async () => {
  await assert.rejects(createDesktopServer({ root, mode: 'product', port: 0 }), /缺少受管桌面会话/)
  const cookieName = 'voice_application_test', session = randomBytes(32).toString('base64url')
  const Cookie = `${cookieName}=${session}`
  const upstream = http.createServer((request, response) => {
    if (request.url === '/desktop-entry?token=owned') {
      response.setHeader('Set-Cookie', `${Cookie}; Path=/; HttpOnly; SameSite=Strict`)
      return response.end('location.replace("/")')
    }
    if (request.headers.cookie !== Cookie) { response.writeHead(403); return response.end() }
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify({ path: request.url, origin: request.headers.origin }))
  })
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve))
  const hostOrigin = `http://127.0.0.1:${upstream.address().port}`
  let server
  try {
    server = await createDesktopServer({ root, mode: 'product', port: 0, hostOrigin, cookieName, session, instanceId: 'owned-instance' })
    assert.equal((await request(server.origin, '/application-config.js')).status, 403)
    const entry = await request(server.origin, '/desktop-entry?token=owned')
    assert.equal(entry.headers.get('set-cookie'), `${Cookie}; Path=/; HttpOnly; SameSite=Strict`)
    const ready = JSON.parse((await request(server.origin, '/__desktop_ready', { Cookie })).body)
    assert.equal(ready.instance_id, 'owned-instance')
    assert.equal(ready.pid, process.pid)
    assert.equal(ready.frontend_mode, 'product')
    assert.equal((await request(server.origin, '/application-config.js', { Cookie })).status, 200)
    const client = await request(server.origin, '/@vite/client', { Cookie })
    assert.equal(client.status, 200)
    assert.match(client.body, /__VOICE_DESKTOP_RECOVERY__/)
    assert.match(client.body, /hotModulesMap/)
    const oldToken = client.body.match(/const wsToken\s*=\s*["']([^"']+)["']/)?.[1]
    assert.ok(oldToken)
    const origin = server.origin
    await assert.rejects(createDesktopServer({ root, mode: 'product', port: Number(new URL(origin).port), strictPort: true,
      hostOrigin, cookieName, session, instanceId: 'occupied-instance' }), /无法绑定/)
    await server.close()
    server = await createDesktopServer({ root, mode: 'product', port: Number(new URL(origin).port), strictPort: true,
      hostOrigin, cookieName, session, instanceId: 'replacement-instance' })
    assert.equal(server.origin, origin)
    assert.equal((await request(origin, '/application-config.js', { Cookie })).status, 200)
    const restoredClient = await request(origin, '/@vite/client', { Cookie })
    assert.equal(restoredClient.body.match(/const wsToken\s*=\s*["']([^"']+)["']/)?.[1], oldToken)
    const accepted = await fetch(server.origin + '/api/v1/application/exit', { method: 'POST', headers: { Cookie, Origin: server.origin }, body: '{}' })
    assert.equal((await accepted.json()).origin, hostOrigin)
    assert.equal((await request(server.origin, '/ui-demo.html', { Cookie })).status, 403)
    const forbidden = await fetch(server.origin + '/api/v1/application/exit', { method: 'POST', headers: { Cookie, Origin: 'http://example.com' }, body: '{}' })
    assert.equal(forbidden.status, 403)
  } finally { await server?.close(); await new Promise(resolve => upstream.close(resolve)) }
})

/** Desktop development server（桌面开发服务）：统一验证资源、配置和热更新连接。 */
import http from 'node:http'
import { readFileSync } from 'node:fs'
import { randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createInterface } from 'node:readline'
import { createServer as createViteServer } from 'vite'

const ENTRY_HINT = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Voice Terminal · 桌面入口</title><style>body{font:16px system-ui;background:#101217;color:#edf0f6;margin:10vh auto;max-width:720px;padding:24px}</style><h1>请通过桌面程序打开</h1><p>主产品和样例预览均使用桌面窗口。开发请在项目中执行 npm run dev（真实业务）或 npm run dev:ui（样例展示）。</p><p>如果此说明出现在桌面窗口中，会话已失效，请退出应用后重新启动。</p></html>'
const startupStyle = readFileSync(new URL('../apps/desktop/boot/startup.css', import.meta.url), 'utf8') // startupStyle（共用加载样式）。
const startupIcon = readFileSync(new URL('../apps/desktop/boot/app-icon.svg', import.meta.url), 'utf8') // startupIcon（共用应用图标）。
const bootstrap = destination => `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Voice Terminal · 语音终端</title><style>html,body{margin:0;background:#0e1118}${startupStyle}</style><main class="startup-screen" role="status"><section class="startup-content"><div class="startup-icon" role="img" aria-label="语音终端">${startupIcon}</div><h1 class="startup-title">应用启动中</h1><p class="startup-status">正在加载应用界面</p><div class="startup-progress" data-indeterminate="true" role="progressbar" aria-label="应用启动进度"><span class="startup-progress-fill"></span></div><div class="startup-actions"><button class="startup-button" type="button" onclick="window.__TAURI_INTERNALS__?.invoke('skip_startup')">跳过</button></div></section></main><script>location.replace(${JSON.stringify(destination)})</script></html>`

function equal(left, right) {
  const a = Buffer.from(left || ''), b = Buffer.from(right || '')
  return a.length === b.length && timingSafeEqual(a, b)
}
function sessionMatches(header, name, value) {
  // Cookie（会话凭据）只接受一个同名项，拒绝歧义，不解码客户端提供的值。
  const values = (header || '').split(';').map(part => part.trim()).filter(part => part.startsWith(`${name}=`))
  return values.length === 1 && equal(values[0].slice(name.length + 1), value)
}
function send(response, body, status = 200, headers = {}) {
  const data = Buffer.from(body)
  response.writeHead(status, {
    'Content-Type': 'text/html; charset=utf-8', 'Content-Length': data.length,
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer', ...headers,
  })
  response.end(data)
}
function trustedHost(value) {
  const address = new URL(value)
  if (address.protocol !== 'http:' || address.hostname !== '127.0.0.1' || !address.port ||
      address.pathname !== '/' || address.search || address.hash || address.username || address.password) {
    throw new Error('应用宿主地址不是当前本机入口。')
  }
  return address.origin
}

/** createDesktopServer（建立受管开发服务）。
 * root（源码根目录）；mode（产品或样例模式）；cookieName（会话凭据名称）；session（会话随机值）；
 * instanceId（所属实例编号）；hostOrigin（产品宿主地址）；port（端口，仅供本地自动测试注入）。
 */
export async function createDesktopServer({ root, mode, cookieName, session, instanceId, hostOrigin, port }) {
  if (!['product', 'ui-preview'].includes(mode)) throw new Error('桌面开发模式无效。')
  const preview = mode === 'ui-preview'
  if (preview) {
    instanceId = randomUUID().replaceAll('-', '')
    cookieName = `voice_preview_${instanceId}`
    session = randomBytes(32).toString('base64url')
  } else {
    if (!/^voice_[A-Za-z0-9_]+$/.test(cookieName || '') || !session || !instanceId) {
      throw new Error('缺少受管桌面会话，请通过 npm run dev（桌面开发）启动。')
    }
    hostOrigin = trustedHost(hostOrigin)
  }
  let vite, origin, closing = false
  let launchToken, issuedAt
  const sockets = new Set()
  // hmrBridge（热更新桥）不监听端口；外层认证成功后才转发升级事件。
  const hmrBridge = http.createServer()
  const validSession = request => sessionMatches(request.headers.cookie, cookieName, session)
  const validHost = request => request.headers.host === new URL(origin).host
  const reject = response => send(response, '桌面会话无效，请退出应用后重新启动。', 403)

  function proxy(request, response) {
    const headers = { ...request.headers, host: new URL(hostOrigin).host }
    if (!['GET', 'HEAD'].includes(request.method)) headers.origin = hostOrigin
    const requested = new URL(request.url, origin)
    const upstream = http.request(new URL(requested.pathname + requested.search, hostOrigin), { method: request.method, headers }, result => {
      response.writeHead(result.statusCode, { ...result.headers, 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer' })
      result.pipe(response)
    })
    upstream.on('error', () => {
      if (!response.headersSent) send(response, '应用宿主暂时不可用，请重新启动桌面程序。', 502)
      else response.destroy()
    })
    request.on('aborted', () => upstream.destroy())
    response.on('close', () => upstream.destroy())
    request.pipe(upstream)
  }

  const server = http.createServer((request, response) => {
    if (!validHost(request)) return reject(response)
    let address
    try { address = new URL(request.url, origin) } catch { return reject(response) }
    if (address.origin !== origin) return reject(response)
    const pathname = address.pathname
    if (pathname === '/desktop-entry') {
      if (request.method !== 'GET') return reject(response)
      if (!preview) return proxy(request, response)
      const tokens = address.searchParams.getAll('token')
      if ([...address.searchParams].length !== 1 || tokens.length !== 1 || !launchToken || Date.now() - issuedAt >= 60000 || !equal(tokens[0], launchToken)) return reject(response)
      launchToken = undefined // 原子消费：事件循环内不跨异步边界。
      return send(response, bootstrap('/ui-demo.html'), 200, { 'Set-Cookie': `${cookieName}=${session}; Path=/; HttpOnly; SameSite=Strict` })
    }
    if (!validSession(request)) {
      if (request.method === 'GET' && ['/', '/index.html', '/ui-demo.html'].includes(pathname)) return send(response, ENTRY_HINT)
      return reject(response)
    }
    if (request.headers.origin && request.headers.origin !== origin) return reject(response)
    if (!['GET', 'HEAD'].includes(request.method) && request.headers.origin !== origin) return reject(response)
    if (pathname === '/__desktop_ready' && request.method === 'GET') {
      if (!vite) return send(response, '桌面界面正在准备。', 503)
      // pid（所属进程编号）；instance_id（实例编号）；origin（实际界面地址）；frontend_mode（前端模式）。
      return send(response, JSON.stringify({ pid: process.pid, instance_id: instanceId, origin, frontend_mode: mode }), 200, { 'Content-Type': 'application/json' })
    }
    if (!preview && (pathname === '/application-config.js' || pathname.startsWith('/api/v1/application/'))) return proxy(request, response)
    if (preview && (pathname === '/index.html' || pathname === '/application-config.js' || pathname.startsWith('/api/') || pathname.startsWith('/src/product/') || pathname.startsWith('/src/composables/'))) return reject(response)
    if (!preview && (pathname === '/ui-demo.html' || pathname.startsWith('/src/ui-demo/'))) return reject(response)
    if (preview && pathname === '/') return send(response, bootstrap('/ui-demo.html'))
    if (!vite) return send(response, '桌面界面正在准备。', 503)
    response.setHeader('Cache-Control', 'no-store')
    vite.middlewares(request, response, () => send(response, '界面资源不存在。', 404))
  })
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)) })
  server.on('upgrade', (request, socket, head) => {
    let address
    try { address = new URL(request.url, origin) } catch {}
    if (!address || address.origin !== origin || !validHost(request) || !validSession(request) || request.headers.origin !== origin || !vite) {
      socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
      return
    }
    hmrBridge.emit('upgrade', request, socket, head)
  })
  const close = async () => {
    if (closing) return
    closing = true
    await vite?.close()
    for (const socket of sockets) socket.destroy()
    if (server.listening) await new Promise(resolveClose => server.close(resolveClose))
  }
  try {
    await new Promise((resolveListen, rejectListen) => {
      server.once('error', rejectListen)
      server.listen({ host: '127.0.0.1', port: port ?? (preview ? 5174 : 5173), exclusive: true }, resolveListen)
    })
    const actualPort = server.address().port
    origin = `http://127.0.0.1:${actualPort}`
    vite = await createViteServer({
      root: resolve(root, 'apps/frontend'),
      configFile: resolve(root, `apps/frontend/${preview ? 'vite.ui.config.ts' : 'vite.config.ts'}`),
      server: {
        middlewareMode: { server: hmrBridge }, port: actualPort, host: '127.0.0.1',
        strictPort: true, open: false, cors: false,
        hmr: { server: hmrBridge, host: '127.0.0.1', port: actualPort, clientPort: actualPort, protocol: 'ws' },
      },
      customLogger: { info() {}, warn: () => { if (!closing) process.stderr.write('界面开发服务提示，请检查依赖。\n') }, warnOnce() {}, error: () => { if (!closing) process.stderr.write('界面开发服务构建失败。\n') }, clearScreen() {}, hasErrorLogged: () => false, hasWarned: false },
    })
    if (preview) { launchToken = randomBytes(32).toString('base64url'); issuedAt = Date.now() }
    return { origin, instanceId, launchToken, close }
  } catch (error) {
    await close()
    throw error.code === 'EADDRINUSE' ? new Error(`桌面开发端口 ${port ?? (preview ? 5174 : 5173)} 被占用，未连接或接管其他服务。`) : error
  }
}

async function main() {
  const args = process.argv.slice(2)
  const value = name => { const index = args.indexOf(name); return index < 0 ? undefined : args[index + 1] }
  const mode = value('--mode'), root = value('--root')
  if (!root || !['product', 'ui-preview'].includes(mode)) throw new Error('需要明确的桌面源码目录与运行模式。')
  const sendControl = (kind, values = {}) => process.stdout.write(`${JSON.stringify({ protocol: 1, kind, ...values })}\n`)
  let running, stopped = false
  const stop = async () => {
    if (stopped) return
    stopped = true
    await running?.close()
    if (mode === 'ui-preview') sendControl('stopped')
  }
  process.on('SIGTERM', () => stop().then(() => process.exit(0)))
  process.on('SIGINT', () => stop().then(() => process.exit(0)))
  if (mode === 'ui-preview') {
    const input = createInterface({ input: process.stdin, crlfDelay: Infinity })
    let started = false
    input.on('line', async line => {
      let frame
      try { if (line.length > 16384) throw new Error(); frame = JSON.parse(line) } catch { await stop(); process.exit(1) }
      if (frame.protocol !== 1) { await stop(); process.exit(1) }
      if (frame.kind === 'shutdown') { await stop(); process.exit(0) }
      if (frame.kind === 'start' && !started) {
        started = true
        try {
          running = await createDesktopServer({ root, mode })
          if (stopped) { await running.close(); return }
          sendControl('ready', { origin: running.origin, launch_token: running.launchToken, instance_id: running.instanceId, pid: process.pid })
        } catch (error) { process.stderr.write(`${error.message}\n`); await stop(); process.exit(1) }
      }
    })
    input.on('close', () => stop().then(() => process.exit(0)))
  } else {
    running = await createDesktopServer({ root, mode, hostOrigin: process.env.VOICE_APP_ORIGIN,
      cookieName: process.env.VOICE_FRONTEND_COOKIE_NAME, session: process.env.VOICE_FRONTEND_SESSION,
      instanceId: process.env.VOICE_APP_INSTANCE_ID })
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1 })
}

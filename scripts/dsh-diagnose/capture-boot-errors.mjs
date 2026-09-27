/**
 * Capture the DSH console's boot errors with full stacks, and list the sessions
 * the client actually subscribed to.
 *
 * The blank-对话 failure surfaces as
 * `[session-controller] event feed subscriber failed: TypeError: Cannot read
 * properties of null (reading 'kind')`. Knowing which session the failing feed
 * belongs to is what turns that message into a fixable diagnosis.
 *
 * Requires the console to be open in the ego-browser window.
 *
 * Usage: node scripts/dsh-diagnose/capture-boot-errors.mjs
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const browserInfo = JSON.parse(readFileSync(join(process.env.LOCALAPPDATA, 'ego-lite-linux', 'browser.json'), 'utf8'))
const ENDPOINT = `http://127.0.0.1:${browserInfo.port}`
const CONSOLE_ORIGIN = '127.0.0.1:3081'

const targets = await (await fetch(`${ENDPOINT}/json/list`)).json()
const target = targets.find(item => item.type === 'page' && item.url.includes(CONSOLE_ORIGIN))
if (!target) throw new Error(`no DSH console page open at ${CONSOLE_ORIGIN}; open it from the Voice Terminal top bar first`)

const socket = new WebSocket(target.webSocketDebuggerUrl)
let id = 0
const pending = new Map()
const messages = []
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const mid = ++id
  pending.set(mid, { resolve, reject })
  socket.send(JSON.stringify({ id: mid, method, params }))
})
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data)
  if (message.id !== undefined) {
    const waiter = pending.get(message.id)
    pending.delete(message.id)
    if (message.error) waiter?.reject(new Error(JSON.stringify(message.error)))
    else waiter?.resolve(message.result)
    return
  }
  if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
    const text = message.params.args.map(arg => arg.value ?? arg.description ?? arg.type).join(' ')
    const frames = (message.params.stackTrace?.callFrames ?? [])
      .map(frame => `      ${frame.functionName || '<anon>'} @${frame.lineNumber}:${frame.columnNumber}`)
      .join('\n')
    messages.push(`${text}\n${frames}`)
  }
})
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve)
  socket.addEventListener('error', reject)
})
await send('Runtime.enable')
await send('Page.enable')

// Record which sessions the client follows, so a crash can be attributed.
await send('Page.addScriptToEvaluateOnNewDocument', {
  source: `(() => {
    const original = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
      try {
        const text = String(data);
        if (text.includes('session/follow')) {
          const match = /"sessionId":"([^"]+)"/.exec(text);
          if (match) (window.__dshFollowed ??= []).push(match[1]);
        }
      } catch {}
      return original.call(this, data);
    };
  })()`,
})

await send('Page.reload', { ignoreCache: true })
await new Promise(resolve => setTimeout(resolve, 15000))

const followed = await send('Runtime.evaluate', { expression: 'JSON.stringify(window.__dshFollowed ?? [])', returnByValue: true })
console.log(`captured ${messages.length} console errors`)
console.log('followed sessions:', followed.result.value)
messages.slice(0, 3).forEach((entry, index) => {
  console.log(`\n--- error ${index + 1} ---`)
  console.log(entry.split('\n').map(line => line.length > 200 ? `${line.slice(0, 200)}…` : line).join('\n'))
})

socket.close()

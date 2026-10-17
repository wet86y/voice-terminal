/**
 * Reload the DSH console and report what it renders.
 *
 * Prints the console errors raised during boot, the sidebar sessions, and the
 * character count of the conversation pane for a few sessions. This is the
 * acceptance check for the blank-对话 failure described in
 * `docs/guides/dsh-plugin-maintenance.md`.
 *
 * Requires the console to be open in the ego-browser window.
 *
 * Usage: node scripts/dsh-diagnose/verify-console-state.mjs [session-prefix ...]
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { DSH_ROOT } from './lib/dsh-paths.mjs'

const browserInfo = JSON.parse(readFileSync(join(process.env.LOCALAPPDATA, 'ego-lite-linux', 'browser.json'), 'utf8'))
const ENDPOINT = `http://127.0.0.1:${browserInfo.port}`
const CONSOLE_ORIGIN = '127.0.0.1:3081'

const targets = await (await fetch(`${ENDPOINT}/json/list`)).json()
const target = targets.find(item => item.type === 'page' && item.url.includes(CONSOLE_ORIGIN))
if (!target) throw new Error(`no DSH console page open at ${CONSOLE_ORIGIN}; open it from the Voice Terminal top bar first`)

const socket = new WebSocket(target.webSocketDebuggerUrl)
let id = 0
const pending = new Map()
const errors = []
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
    errors.push(message.params.args.map(arg => arg.value ?? arg.description ?? '').join(' ').slice(0, 140))
  }
})
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve)
  socket.addEventListener('error', reject)
})
await send('Runtime.enable')
await send('Page.enable')
await send('Page.reload', { ignoreCache: true })
await new Promise(resolve => setTimeout(resolve, 15000))

const evaluate = async expression => (await send('Runtime.evaluate', { expression, returnByValue: true })).result.value

console.log(`boot console errors: ${errors.length}`)
for (const line of errors.slice(0, 5)) console.log('  ', line)
console.log('sidebar:', await evaluate(`JSON.stringify([...document.querySelectorAll('[role="treeitem"]')].map(el => el.textContent.trim().replace(/\\s+/g,' ').slice(0,30)))`))

const prefixes = process.argv.slice(2)
for (const prefix of prefixes.length > 0 ? prefixes : []) {
  const before = errors.length
  await evaluate(`(() => {
    const items = [...document.querySelectorAll('[role="treeitem"]')];
    const target = items.find(el => el.textContent.trim().startsWith(${JSON.stringify(prefix)}));
    if (target) target.click();
  })()`)
  await new Promise(resolve => setTimeout(resolve, 6000))
  const text = await evaluate(`(() => {
    const view = document.querySelector('[class*="viewArea"]');
    return view ? (view.innerText || '').trim() : '';
  })()`)
  console.log(`\n[${prefix}] new console errors: ${errors.length - before}`)
  console.log(`  conversation chars=${text.length} :: ${text.slice(0, 110).replace(/\n/g, ' | ')}`)
}

socket.close()
console.log(`\nDSH home: ${DSH_ROOT}`)

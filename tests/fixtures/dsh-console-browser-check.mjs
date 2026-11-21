/** Playwright（浏览器自动化）验收客户端入口：原生插件界面、双页面及原生实时连接。 */
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { openVoiceSettings } from './dsh-browser-settings.mjs'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const require = createRequire(join(repo, '.local/build/dsh/source/apps/web/package.json'))
const { chromium } = require('playwright')
const url = process.env.VOICE_TEST_BROWSER_URL
assert.ok(url && new URL(url).hostname.startsWith('127.'))
const browserOrigin = new URL(url).origin
const browser = await chromium.launch(existsSync(chromium.executablePath()) ? { headless:true } : { headless:true, channel:'msedge' })
const errors = []
const failed = []
let websocketCount = 0
let pluginResponses = 0
let closedSockets = 0
let page
try {
  const context = await browser.newContext({ viewport:{ width:1440, height:1000 }, locale:'zh-CN' })
  await context.route('**/*', route => new URL(route.request().url()).origin === browserOrigin ? route.continue() : route.abort())
  page = await context.newPage()
  const observe = page => {
    page.on('pageerror', error => errors.push(error.message))
    page.on('websocket', socket => {
      if (new URL(socket.url()).pathname === '/api/remote.mux') {
        websocketCount++
        socket.on('close', () => closedSockets++)
      }
    })
    page.on('response', response => {
      const address = new URL(response.url())
      if (address.pathname.startsWith('/plugins/') && response.ok()) pluginResponses++
      if (response.status() >= 400) failed.push({ path:address.pathname, status:response.status() })
    })
  }
  observe(page)
  await page.goto(url, { waitUntil:'load' })
  assert.equal(new URL(page.url()).pathname, '/')
  await page.waitForFunction(() => JSON.stringify(globalThis.__DSH_BOOT__ ?? {}).includes('@voice-terminal/dsh-plugin'), undefined, { timeout:20_000 })
  await page.getByRole('button', { name:'账号菜单', exact:true }).waitFor({ timeout:30_000 })
  const notice = page.getByRole('dialog', { name:'预览版说明' })
  await notice.waitFor({ timeout:30_000 })
  await notice.getByRole('button', { name:'继续', exact:true }).click()
  await notice.waitFor({ state:'hidden' })
  const dialog = await openVoiceSettings(page)
  assert.ok((await dialog.innerText()).length > 20)
  const other = await context.newPage()
  observe(other)
  await other.goto(new URL('/', url).href, { waitUntil:'load' })
  await other.getByRole('button', { name:'账号菜单', exact:true }).waitFor({ timeout:30_000 })
  assert.ok(websocketCount >= 2, '两个页面都应打开原生实时连接')
  assert.ok(pluginResponses >= 2, '插件脚本应随原站加载')
  await other.reload({ waitUntil:'load' })
  await other.getByRole('button', { name:'账号菜单', exact:true }).waitFor({ timeout:30_000 })
  assert.deepEqual(errors, [])
  assert.deepEqual(failed, [])
  assert.ok(!(await context.cookies()).some(cookie => cookie.name.startsWith('dsh-auth-')), 'DSH 凭据不得出现在客户端浏览器')
  assert.ok(!(await context.cookies('http://127.0.0.1:1/')).some(cookie => cookie.name.startsWith('voice-dsh-console-')),
    '独立回环主机上的控制台凭据不能被带到其他普通本机服务')
  console.log(JSON.stringify({ result:'passed', pages:2, plugin_settings:true, websocket_connections:websocketCount,
    plugin_resources:pluginResponses, refresh:true, upstream_credentials_exposed:false }))
} catch (error) {
  if (page) {
    await page.screenshot({ path:join(repo, '.local/cache/reports/dsh-console-browser-failure.png'), fullPage:true })
    console.error(JSON.stringify({ errors, failed, websocketCount, closedSockets, pluginResponses,
      page_text:(await page.locator('body').innerText()).slice(0,4000) }))
  }
  throw error
} finally {
  await browser.close()
}

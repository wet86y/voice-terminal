/** Frontend layout check（真实弹窗布局回归）：固定数据、正式组件、无业务写入。 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { extname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'
import { projectRoot } from './project-root.mjs'

const root = projectRoot(import.meta.url) // root（源码根目录）。
const report = join(root, '.local/cache/reports/frontend-dialog-layout') // report（固定验收报告目录）。
const output = join(report, 'render') // output（固定夹具构建目录，文件名固定覆盖）。
const profile = join(root, '.local/cache/frontend-dialog-layout-browser') // profile（独立的浏览器渲染缓存）。
const candidates = [process.env.VOICE_TERMINAL_LAYOUT_BROWSER,
  join(process.env['PROGRAMFILES(X86)'] || 'C:/Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe'),
  join(process.env.PROGRAMFILES || 'C:/Program Files', 'Microsoft/Edge/Application/msedge.exe'),
].filter(Boolean) // candidates（浏览器候选路径）；VOICE_TERMINAL_LAYOUT_BROWSER（自定义渲染引擎）。
let executable
for (const candidate of candidates) { try { await access(candidate); executable = candidate; break } catch {} }
assert.ok(executable, '未找到渲染浏览器，请设置 VOICE_TERMINAL_LAYOUT_BROWSER（渲染引擎路径）。')
await mkdir(output, { recursive: true }); await mkdir(profile, { recursive: true })
await build({
  configFile: false, root: join(root, 'apps/frontend'), logLevel: 'error', plugins: [vue()],
  build: { outDir: output, emptyOutDir: false, rollupOptions: {
    input: join(root, 'tests/fixtures/ui-dialog-layout.mjs'),
    output: { entryFileNames: 'fixture.js', chunkFileNames: '[name].js', assetFileNames: '[name][extname]' },
  } },
})
await writeFile(join(output, 'index.html'), `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/ui-dialog-layout.css"><style>.layout-fixture{height:100%;overflow:auto;padding:24px}</style><div id="app"></div><script type="module" src="/fixture.js"></script></html>`)
const expectedVersions = (await readdir(join(root, 'docs/releases'))).filter(name => /^v\d+\.\d+\.\d+\.md$/.test(name)).length // expectedVersions（随包版本日志数量）。
const server = createServer(async (request, response) => {
  try {
    const relative = decodeURIComponent(new URL(request.url, 'http://localhost').pathname).slice(1) || 'index.html'
    const file = resolve(output, relative)
    assert.ok(file.startsWith(output + '/') || file.startsWith(output + '\\'))
    response.setHeader('Content-Type', ({ '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[extname(file)] || 'application/octet-stream')
    response.end(await readFile(file))
  } catch { response.writeHead(404); response.end() }
})
server.listen(0, '127.0.0.1'); await once(server, 'listening')
const origin = `http://127.0.0.1:${server.address().port}` // origin（隔离页面地址，仅监听本机）。
const browser = spawn(executable, ['--headless=new', '--no-first-run', '--disable-background-networking', '--disable-sync',
  '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' })
let socket, sequence = 0
const pending = new Map(), checks = [], errors = [] // pending（待回执调用）、checks（验收结果）、errors（页面异常）。
const sceneNames = { resource: '资源清单', hotword: '热词编辑器', workspace: '工作区清理', sync: '版本同步', image: '图片查看器', long: '长说明' } // sceneNames（场景中文名称）。
function call(method, params = {}) {
  const id = ++sequence
  return new Promise((done, fail) => {
    const timer = setTimeout(() => { pending.delete(id); fail(new Error(`渲染调用超时：${method}`)) }, 15000)
    pending.set(id, { done, fail, timer }); socket.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
  return result.result.value
}
async function click(selector) {
  // text=（按钮文字选择）：按真实操作名称定位，避免依赖按钮排列顺序。
  const point = await evaluate(`(()=>{const s=${JSON.stringify(selector)},e=s.startsWith('text=')?[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===s.slice(5)):document.querySelector(s);if(!e)throw Error('未找到目标控件：'+s);e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`)
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', ...point })
  for (const type of ['mousePressed', 'mouseReleased']) await call('Input.dispatchMouseEvent', { type, ...point, button: 'left', clickCount: 1 })
  await delay(180)
}
async function pressEscape() {
  for (const type of ['keyDown', 'keyUp']) await call('Input.dispatchKeyEvent', { type, key: 'Escape', windowsVirtualKeyCode: 27 })
  await delay(100)
}
async function screenshot(name) { const result = await call('Page.captureScreenshot', { format: 'png' }); await writeFile(join(report, name), Buffer.from(result.data, 'base64')) }
async function viewport(width, height) { await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false }); await delay(180) }
async function show(scene) { await evaluate(`window.__UI_LAYOUT__.show(${JSON.stringify(scene)})`); await delay(180) }
async function dialogBounds() {
  const bounds = await evaluate(`(()=>{const d=document.querySelector('dialog:modal'),p=d.querySelector('.apply-dialog'),b=d.querySelector('.dialog-body'),f=d.querySelector('.dialog-actions');const r=p.getBoundingClientRect(),fr=f?.getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,viewportWidth:innerWidth,viewportHeight:innerHeight,footerVisible:!fr||fr.top>=0&&fr.bottom<=innerHeight,contentHeight:b.clientHeight,contentScroll:b.scrollHeight,horizontalOverflow:b.scrollWidth>b.clientWidth+1}})()`)
  assert.ok(bounds.left >= 0 && bounds.top >= 0 && bounds.right <= bounds.viewportWidth + 1 && bounds.bottom <= bounds.viewportHeight + 1, JSON.stringify(bounds))
  assert.ok(bounds.footerVisible && bounds.contentHeight > 0 && !bounds.horizontalOverflow, JSON.stringify(bounds))
  return bounds
}
async function listGeometry(selector, count, scrollsWithDialog = true) { // scrollsWithDialog（列表随公共内容区滚动）。
  const data = await evaluate(`(()=>{const rows=[...document.querySelectorAll(${JSON.stringify(selector)})],list=rows[0]?.parentElement;return {count:rows.length,listClipped:list&&list.scrollHeight>list.clientHeight+1,clipped:rows.map((e,index)=>({index,height:e.clientHeight,content:e.scrollHeight})).filter(e=>!e.height||e.content>e.height+1)}})()`)
  assert.equal(data.count, count); assert.deepEqual(data.clipped, [], JSON.stringify(data))
  if (scrollsWithDialog) assert.equal(data.listClipped, false, '普通列表不能再建立限高裁切层。')
  return data
}
try {
  let targets
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      const port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0])
      targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      if (targets.some(target => target.type === 'page')) break
    } catch {}
    await delay(100)
  }
  assert.ok(targets?.some(target => target.type === 'page'), '隔离渲染引擎未就绪。')
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl)
  socket.addEventListener('message', event => {
    const message = JSON.parse(String(event.data))
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text)
    const item = pending.get(message.id)
    if (item) { clearTimeout(item.timer); pending.delete(message.id); message.error ? item.fail(new Error(message.error.message)) : item.done(message.result) }
  })
  await once(socket, 'open'); await call('Runtime.enable'); await call('Page.enable')
  await call('Emulation.setFocusEmulationEnabled', { enabled: true }); await viewport(1200, 900)
  await call('Page.navigate', { url: origin })
  for (let count = 0; count < 100 && !await evaluate('window.__UI_LAYOUT__?.ready'); count++) await delay(100)
  assert.ok(await evaluate('window.__UI_LAYOUT__?.ready'))
  await click('text=更新日志')
  for (const [width, height] of [[1200, 900], [960, 720], [390, 600], [800, 360]]) {
    await viewport(width, height)
    await evaluate('document.querySelector(".dialog-body").scrollTop=0')
    await listGeometry('.release-history-entry', expectedVersions)
    assert.ok((await dialogBounds()).contentScroll > (await dialogBounds()).contentHeight)
    await screenshot(`history-${width}x${height}.png`)
    await click('.release-history-toggle')
    await listGeometry('.release-history-entry', expectedVersions)
    assert.ok(await evaluate('document.querySelector(".release-history-body").clientHeight > 100'))
    await screenshot(`history-expanded-${width}x${height}.png`)
    await click('.release-history-toggle')
    await evaluate('document.querySelector(".dialog-body").scrollTop=1e7')
    assert.ok(await evaluate('(()=>{const r=document.querySelector(".release-history-entry:last-child").getBoundingClientRect(),b=document.querySelector(".dialog-body").getBoundingClientRect();return r.bottom<=b.bottom+1&&r.bottom>b.top})()'))
    await dialogBounds()
    checks.push(`版本日志 ${width}×${height}：${expectedVersions} 个版本折叠、展开、末项可达、文字完整`)
  }
  for (const [scene, open, selector, count] of [
    ['resource', null, '.install-bundle-list li', 30],
    ['hotword', 'text=导入词表', null, 0],
    ['workspace', '.workspace-cleanup-action', '.cleanup-entries li', 40],
    ['sync', null, null, 0],
    ['image', '.capture-thumbnail', null, 0],
    ['long', null, '.dialog-body>p', 60],
  ]) {
    await viewport(1200, 900); await show(scene)
    if (open) await click(open)
    for (const [width, height] of [[1200, 900], [390, 600], [800, 360]]) {
      await viewport(width, height)
      const bounds = await dialogBounds()
      if (selector) await listGeometry(selector, count, scene !== 'long')
      if (scene === 'hotword') assert.ok(await evaluate('document.querySelector("dialog textarea").clientHeight >= 220'))
      if (scene === 'image') assert.ok(await evaluate('document.querySelector(".image-stage").clientHeight > 0'))
      if (scene === 'resource' || scene === 'long') assert.ok(bounds.contentScroll > bounds.contentHeight)
      await screenshot(`${scene}-${width}x${height}.png`)
      checks.push(`${sceneNames[scene]} ${width}×${height}：内容、滚动及操作区完整`)
    }
  }
  await pressEscape()
  assert.deepEqual(await evaluate('window.__UI_LAYOUT__.blocked'), [])
  assert.deepEqual(errors, [])
  await writeFile(join(report, 'result.json'), JSON.stringify({ 结果: '通过', 检查数量: checks.length, 检查: checks, 页面异常: errors }, null, 2))
  console.log(`真实弹窗布局回归通过：${checks.length} 项；报告：${report}`)
} catch (error) {
  try { await screenshot('failure.png') } catch {}
  await writeFile(join(report, 'result.json'), JSON.stringify({ 结果: '失败', 原因: error.stack, 已完成检查: checks, 页面异常: errors }, null, 2))
  throw error
} finally {
  try { await call('Browser.close') } catch {}
  socket?.close(); server.closeAllConnections(); await new Promise(done => server.close(done))
  if (browser.exitCode === null) await Promise.race([once(browser, 'exit'), delay(5000)])
}

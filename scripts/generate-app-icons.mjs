import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { access, copyFile, mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'

// icon generation（图标生成）：系统网页引擎渲染同一份矢量标识，无须安装图片处理依赖。
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const source = join(root, 'apps/frontend/src/assets/app-icon.svg')
const directory = join(root, 'apps/desktop/src-tauri/icons')
const sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256] // sizes（Windows 常用图标像素尺寸）。
const candidates = [
  process.env.VOICE_TERMINAL_ICON_BROWSER,
  join(process.env['PROGRAMFILES(X86)'] || 'C:/Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe'),
  join(process.env.PROGRAMFILES || 'C:/Program Files', 'Microsoft/Edge/Application/msedge.exe'),
].filter(Boolean)
let browser
for (const candidate of candidates) {
  try { await access(candidate); browser = candidate; break } catch { }
}
assert.ok(browser, '未找到图标渲染引擎，请设置 VOICE_TERMINAL_ICON_BROWSER（图标渲染浏览器路径）。')
const runtime = join(root, '.runtime')
await mkdir(runtime, { recursive: true })
const profile = await mkdtemp(join(runtime, 'app-icon-render-'))
const child = spawn(browser, [
  '--headless=new', '--no-first-run', '--disable-background-networking',
  '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, 'about:blank',
], { windowsHide: true, stdio: 'ignore' })
let launchError
child.on('error', error => { launchError = error })
let socket
try {
  let port
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (launchError) throw launchError
    try { port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break } catch { await delay(100) }
  }
  assert.ok(port, '图标渲染引擎启动超时。')
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  const page = targets.find(target => target.type === 'page')
  assert.ok(page, '未找到图标渲染页面。')
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((done, fail) => {
    socket.addEventListener('open', done, { once: true })
    socket.addEventListener('error', fail, { once: true })
  })
  const svg = await readFile(source, 'utf8')
  const expression = `(async () => {
    const svg = ${JSON.stringify(svg)}, sizes = ${JSON.stringify(sizes)}, result = [];
    for (const size of sizes) {
      // 小尺寸的声波对齐整像素，避免标题栏中的细线糊在一起。
      const step = size <= 48 ? Math.max(2, Math.round(size*.047)) : null;
      const compact = step ? '<g fill="#18305f">' + [-2,-1,0,1,2].map((offset, index) => {
        const width = 1;
        const height = [Math.max(2, Math.round(size*.11)), Math.round(size*.2), Math.round(size*.28), Math.round(size*.2), Math.max(2, Math.round(size*.11))][index];
        const x = Math.round(size/2 + offset*step - width/2), y = Math.round((size-height)/2);
        return '<rect x="'+(x*256/size)+'" y="'+(y*256/size)+'" width="'+(width*256/size)+'" height="'+(height*256/size)+'" rx="'+(width*128/size)+'"/>';
      }).join('') + '</g>' : null;
      const markup = compact ? svg.replace(/<g id="waveform"[\\s\\S]*?<\\/g>/, compact) : svg;
      const url = URL.createObjectURL(new Blob([markup], {type:'image/svg+xml'}));
      const image = new Image(); image.src = url; await image.decode();
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = size;
      const context = canvas.getContext('2d'); context.drawImage(image,0,0,size,size);
      result.push({size, png:canvas.toDataURL('image/png').split(',')[1],
        rgba:size <= 48 ? btoa(String.fromCharCode(...context.getImageData(0,0,size,size).data)) : null});
      URL.revokeObjectURL(url);
    }
    return result;
  })()`
  const response = await new Promise((done, fail) => {
    const timer = setTimeout(() => fail(new Error('图标渲染超时。')), 10000)
    socket.addEventListener('message', event => {
      const message = JSON.parse(String(event.data))
      if (message.id !== 1) return
      clearTimeout(timer)
      if (message.error) fail(new Error(message.error.message)); else done(message.result)
    })
    socket.send(JSON.stringify({id:1, method:'Runtime.evaluate', params:{expression, returnByValue:true, awaitPromise:true}}))
  })
  assert.ok(!response.exceptionDetails, JSON.stringify(response.exceptionDetails))
  const frames = response.result.value.map(frame => ({size:frame.size, data:Buffer.from(frame.png, 'base64'), rgba:frame.rgba ? Buffer.from(frame.rgba, 'base64') : null}))
  // ICO（Windows 多尺寸图标文件）：为每种缩放比例保留独立的透明 PNG（位图）帧。
  const header = Buffer.alloc(6 + frames.length * 16)
  header.writeUInt16LE(1, 2); header.writeUInt16LE(frames.length, 4)
  let offset = header.length
  frames.forEach((frame, index) => {
    const position = 6 + index * 16
    header[position] = header[position + 1] = frame.size === 256 ? 0 : frame.size
    header.writeUInt16LE(1, position + 4); header.writeUInt16LE(32, position + 6)
    header.writeUInt32LE(frame.data.length, position + 8); header.writeUInt32LE(offset, position + 12)
    offset += frame.data.length
  })
  await mkdir(directory, { recursive: true })
  const icon = Buffer.concat([header, ...frames.map(frame => frame.data)])
  await writeFile(join(directory, 'icon.ico'), icon)
  await writeFile(join(directory, 'icon.png'), frames.at(-1).data)
  // RGBA（红绿蓝与透明度像素）：托盘直接选择同一小尺寸帧，避免从大图缩小造成声波失真。
  for (const frame of frames.filter(frame => frame.rgba)) await writeFile(join(directory, `tray-${frame.size}.rgba`), frame.rgba)
  await writeFile(join(root, 'apps/frontend/src/assets/favicon.ico'), icon)
  await copyFile(source, join(root, 'apps/desktop/boot/app-icon.svg'))
  const previews = join(runtime, 'app-icon-preview')
  await mkdir(previews, {recursive:true})
  for (const frame of frames) await writeFile(join(previews, `${frame.size}.png`), frame.data)
  console.log(`已生成统一应用图标，尺寸：${sizes.join('、')} 像素。`)
} finally {
  socket?.close()
  // 只结束本次独立的无界面渲染进程，不影响用户已打开的浏览器。
  child.kill()
}

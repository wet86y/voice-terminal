/** AgentClient（代理调试客户端）：只封装正式入口，按请求编号管理回执和持续连接。 */
import { spawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createInterface } from 'node:readline'
import { parseArgs } from 'node:util'

const root = fileURLToPath(new URL('../', import.meta.url))
const defaultEntry = resolve(root, '.local/VoiceTerminal.exe')

export class AgentError extends Error {
  constructor(message, exitCode = 2) { super(message); this.exitCode = exitCode }
}

export class AgentClient extends EventEmitter {
  /** entry（入口路径）、timeoutMs（调用超时毫秒数）、floatingPreview（连接独立悬浮窗测试台）。 */
  constructor({ entry, timeoutMs = 30000, floatingPreview = false } = {}, startProcess = spawn) {
    super()
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1) throw new AgentError('调用超时必须为正整数毫秒。')
    this.timeoutMs = timeoutMs
    this.pending = new Map()
    this.sequence = 0
    this.ended = false
    this.failure = null
    this.closed = false
    this.stderr = ''
    const executable = entry ?? (floatingPreview ? resolve(root, '.local/build/desktop/product/debug/VoiceTerminalDesktop.exe') : defaultEntry)
    const parameters = floatingPreview ? ['--agent-stdio', '--ui-preview', '--floating-preview', '--root', root] : ['--agent-stdio']
    this.child = startProcess(executable, parameters, { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
    this.completion = new Promise((resolveDone, rejectDone) => {
      this.child.once('error', error => this.fail(new AgentError(`无法启动正式调试入口：${error.message}。请检查 --entry（入口路径）。`)))
      this.child.once('close', code => {
        this.closed = true
        clearTimeout(this.closeTimer)
        if (!this.failure && (code !== 0 || this.pending.size)) {
          this.fail(new AgentError(`调试连接未完整结束（退出码 ${code}）。${this.stderr.trim() || '仍有请求未收到回执。'} 请确认程序已用 --agent-debug（代理调试启动）启动且允许外部调试。`))
        }
        if (this.failure) rejectDone(this.failure)
        else resolveDone()
      })
    })
    // completion（连接完成承诺）可晚于请求等待，提前安装异常处理以免产生未处理拒绝。
    this.completion.catch(() => {})
    this.child.stdin.on('error', error => this.fail(new AgentError(`调试输入连接中断：${error.message}。业务可能已接受，请先查询状态。`)))
    this.child.stderr.on('data', chunk => { this.stderr = (this.stderr + chunk.toString('utf8')).slice(-4096) })
    this.lines = createInterface({ input: this.child.stdout, crlfDelay: Infinity })
    this.lines.on('line', line => {
      let value
      try { value = JSON.parse(line) } catch { this.fail(new AgentError('正式入口返回了无效的结构化回执。')); return }
      if (value?.jsonrpc !== '2.0') { this.fail(new AgentError('正式入口返回了无效的协议版本。')); return }
      if (!Object.hasOwn(value, 'id')) {
        if (typeof value.method !== 'string' || Object.hasOwn(value, 'result') || Object.hasOwn(value, 'error')) {
          this.fail(new AgentError('正式入口返回了无效的通知消息。')); return
        }
        this.emit('notification', value); return
      }
      const pending = this.pending.get(value.id)
      if (!pending) { this.fail(new AgentError('收到无法匹配请求编号的回执。')); return }
      if (Object.hasOwn(value, 'result') === Object.hasOwn(value, 'error')) {
        this.fail(new AgentError('回执必须包含结果或错误之一。')); return
      }
      clearTimeout(pending.timer)
      this.pending.delete(value.id)
      pending.resolve(value)
    })
  }

  fail(error) {
    if (this.failure) return
    this.failure = error
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(error) }
    this.pending.clear()
    // 只结束本工具建立的转发进程；正式宿主及已提交业务由正式入口管理。
    this.child.kill()
  }

  /** request（发送请求）：回执含 result（结果）或 error（业务错误），调用者按 id（编号）匹配。 */
  request(method, params = {}) {
    if (this.failure) return Promise.reject(this.failure)
    if (this.ended) return Promise.reject(new AgentError('调试输入已结束，不能追加请求。'))
    if (typeof method !== 'string' || !method || !params || Array.isArray(params) || typeof params !== 'object') {
      return Promise.reject(new AgentError('方法名不能为空，参数必须为结构化对象。'))
    }
    const id = ++this.sequence
    let line
    try { line = JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n' }
    catch { return Promise.reject(new AgentError('参数无法编码为结构化消息。')) }
    if (Buffer.byteLength(line) > 8 * 1024 * 1024) return Promise.reject(new AgentError('请求超过单帧 8 MiB（兆二进制字节）上限。'))
    return new Promise((resolveReply, rejectReply) => {
      const timer = setTimeout(() => this.fail(new AgentError(`等待 ${method}（调试方法）回执超时。业务可能仍在运行，请查询状态，避免重复提交。`, 4)), this.timeoutMs)
      this.pending.set(id, { resolve: resolveReply, reject: rejectReply, timer })
      this.child.stdin.write(line, 'utf8')
    })
  }

  /** close（结束输入并排空回执）：可在请求等待期间调用，不以关闭输入取消业务。 */
  close() {
    if (!this.ended && !this.closed) {
      this.ended = true
      this.child.stdin.end()
      this.closeTimer = setTimeout(() => this.fail(new AgentError('等待调试连接排空超时；业务结果需重新查询。', 4)), this.timeoutMs)
    }
    return this.completion
  }
}

async function save(path, content) {
  await mkdir(dirname(resolve(path)), { recursive: true })
  await writeFile(path, content)
}

export async function runCli(args = process.argv.slice(2)) {
  const { values } = parseArgs({ args, options: {
    method: { type: 'string' }, params: { type: 'string' }, 'params-file': { type: 'string' },
    'timeout-ms': { type: 'string' }, output: { type: 'string' }, screenshot: { type: 'string' },
    entry: { type: 'string' }, 'floating-preview': { type: 'boolean' }, help: { type: 'boolean' },
  } })
  if (values.help) {
    console.log(`正式代理调试调用：npm run agent:call -- --method ui.snapshot --params '{"window":"main"}'
--method 方法名；--params 参数对象；--params-file 从 UTF-8（统一字符编码）文件读取参数（两者互斥）
--timeout-ms 超时毫秒数（默认 30000）；--entry 正式入口路径（默认当前工作区）
--floating-preview 连接当前工作区单独运行的悬浮窗测试台
--output 原始完整回执文件；--screenshot 截图保存路径（仅界面快照，保存 JPEG 图片）
退出码：0 成功，2 参数/连接/文件错误，3 正式接口错误回执，4 等待超时。
本工具复用 --agent-stdio（标准流连接），请先通过正式入口进入代理调试模式。`)
    return 0
  }
  if (!values.method) throw new AgentError('缺少 --method（方法名）；使用 --help（帮助）查看示例。')
  if (values.params !== undefined && values['params-file'] !== undefined) throw new AgentError('--params（参数对象）与 --params-file（参数文件）不能同时使用。')
  let params
  try { params = JSON.parse(values['params-file'] !== undefined ? (await readFile(values['params-file'], 'utf8')).replace(/^\uFEFF/, '') : values.params ?? '{}') }
  catch (error) { throw new AgentError(`读取参数失败：${error.message}。参数文件须为 UTF-8（统一字符编码）的对象。`) }
  if (!params || Array.isArray(params) || typeof params !== 'object') throw new AgentError('参数必须为结构化对象。')
  if (values.screenshot) {
    if (values.method !== 'ui.snapshot') throw new AgentError('--screenshot（截图路径）仅适用于 ui.snapshot（界面快照）。')
    params = { window: 'main', ...params, screenshot: true }
  }
  const client = new AgentClient({ entry: values.entry, floatingPreview: values['floating-preview'], timeoutMs: values['timeout-ms'] === undefined ? 30000 : Number(values['timeout-ms']) })
  const response = client.request(values.method, params)
  // 单次工具立即结束输入，依靠服务端结束语义等待完整回执，不插入客户端延时。
  const completion = client.close()
  const reply = await response
  if (values.output) {
    await save(values.output, JSON.stringify(reply, null, 2) + '\n')
    console.error(`原始结构化回执已保存：${resolve(values.output)}`)
  }
  await completion
  if (reply.error) throw new AgentError(`正式接口拒绝：${reply.error.message}（错误码 ${reply.error.code}）。${values.output ? '原始错误回执已保存。' : ''}`, 3)
  if (values.screenshot) {
    const encoded = reply.result?.screenshot_jpeg
    if (typeof encoded !== 'string' || !encoded) throw new AgentError('该窗口未返回截图；请选择网页窗口，并检查窗口是否正在切换。', 3)
    await save(values.screenshot, Buffer.from(encoded, 'base64'))
    console.error(`界面截图已保存：${resolve(values.screenshot)}`)
  }
  const displayed = JSON.parse(JSON.stringify(reply))
  if (displayed.result?.screenshot_jpeg) displayed.result.screenshot_jpeg = '（图片编码已省略；完整内容可用 --output 保存，图片可用 --screenshot 保存）'
  if (!values.output) console.log(JSON.stringify(displayed, null, 2))
  return 0
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = await runCli() }
  catch (error) { console.error(`代理调试调用失败：${error.message}`); process.exitCode = error.exitCode ?? 2 }
}

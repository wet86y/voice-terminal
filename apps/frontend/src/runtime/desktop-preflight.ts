/** DesktopFrontendMode（桌面前端模式）：桌面宿主允许加载的受控入口。 */
export type DesktopFrontendMode = 'product' | 'ui-preview'

/** DesktopStatus（桌面状态）：desktop_status（桌面状态命令）的受限响应。 */
export interface DesktopStatus {
  /** ready（就绪标识）：网页宿主是否已完成启动。 */
  ready: boolean
  /** error（启动错误）：宿主启动失败原因；成功时为空。 */
  error: string | null
  /** frontend_mode（前端模式）：本窗口被授权加载的入口。 */
  frontend_mode: DesktopFrontendMode
  /** development（开发标识）：是否由调试构建启动；样例入口必须为 true。 */
  development: boolean
}

/** NativeCommandInvoker（原生命令调用器）：受控调用 Tauri（桌面运行时）命令。 */
export interface NativeCommandInvoker {
  invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>
}

type NativeWindow = Window & {
  __TAURI_INTERNALS__?: NativeCommandInvoker
}

/** locateNativeInvoker（查找原生命令调用器）：浏览器运行时不提供替代实现。 */
export function locateNativeInvoker(target: Window | undefined = typeof window === 'undefined' ? undefined : window): NativeCommandInvoker | null {
  return (target as NativeWindow | undefined)?.__TAURI_INTERNALS__ ?? null
}

/** validateDesktopStatus（验证桌面状态）：任何字段不匹配都阻止应用模块加载。 */
export function validateDesktopStatus(value: unknown, expectedMode: DesktopFrontendMode): string | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return '桌面启动程序返回了无效状态。'
  }
  const status = value as Partial<DesktopStatus>
  if (typeof status.error === 'string' && status.error.trim()) {
    return status.error
  }
  if (status.error !== null && status.error !== '') {
    return '桌面启动程序的错误状态无效。'
  }
  if (status.ready !== true) {
    return '桌面启动程序尚未就绪，请稍后重试。'
  }
  if (status.frontend_mode !== expectedMode) {
    return `桌面启动程序授权的前端模式与当前入口不匹配（需要 ${frontendModeLabel(expectedMode)}）。`
  }
  if (typeof status.development !== 'boolean') {
    return '桌面启动程序没有提供有效的 development（开发标识）。'
  }
  if (expectedMode === 'ui-preview' && status.development !== true) {
    return 'ui-preview（样例）入口只允许调试构建启动。'
  }
  return null
}

function frontendModeLabel(mode: DesktopFrontendMode) {
  return mode === 'product' ? 'product（产品）' : 'ui-preview（样例）'
}

export interface DesktopPreflightOptions {
  /** expectedMode（预期前端模式）：必须与桌面授权模式完全一致。 */
  expectedMode: DesktopFrontendMode
  /** startApplication（启动应用）：状态核验成功后才会执行动态导入。 */
  startApplication: () => Promise<void>
  /** root（挂载容器）：显示核验进度、错误、重试和退出操作。 */
  root?: HTMLElement | null
  /** invoke（原生命令调用器）：测试时显式注入，生产环境从桌面窗口读取。 */
  invoke?: NativeCommandInvoker | null
}

function makeElement<K extends keyof HTMLElementTagNameMap>(tag: K, text: string, className?: string) {
  const element = document.createElement(tag)
  element.textContent = text
  if (className) element.className = className
  return element
}

/**
 * runDesktopPreflight（运行桌面预检）：预检失败时只显示说明，不加载 Vue（界面框架）或业务模块。
 */
export async function runDesktopPreflight(options: DesktopPreflightOptions): Promise<void> {
  const root = options.root ?? (typeof document === 'undefined' ? null : document.getElementById('app'))
  const invoke = options.invoke === undefined ? locateNativeInvoker() : options.invoke
  let busy = false

  function show(title: string, detail: string, retryable: boolean, error = false) {
    if (!root || typeof document === 'undefined') return
    const section = makeElement('section', '', 'desktop-preflight')
    section.setAttribute('role', error ? 'alert' : 'status')
    const heading = makeElement('h1', title)
    const description = makeElement('p', detail)
    section.append(heading, description)
    if (error) {
      const instructions = makeElement('p', '请从 Voice Terminal 桌面应用重新打开此窗口；浏览器页面不会连接语音服务、网络接口或设备。')
      section.append(instructions)
    }
    const actions = makeElement('div', '', 'desktop-preflight-actions')
    if (retryable) {
      const retry = makeElement('button', '重试')
      retry.type = 'button'
      retry.disabled = busy
      retry.addEventListener('click', () => { void attempt() })
      actions.append(retry)
    }
    if (invoke) {
      const exit = makeElement('button', '退出应用')
      exit.type = 'button'
      exit.disabled = busy
      exit.addEventListener('click', () => { void exitApplication() })
      actions.append(exit)
    }
    if (actions.childElementCount) section.append(actions)
    root.replaceChildren(section)
  }

  async function exitApplication() {
    if (!invoke || busy) return
    busy = true
    show('正在退出应用', '正在请求桌面启动程序退出。', false)
    try {
      await invoke.invoke<void>('exit_application')
    } catch (reason) {
      busy = false
      show('无法退出应用', reason instanceof Error ? reason.message : String(reason), true, true)
    }
  }

  async function attempt() {
    if (busy) return
    busy = true
    show('正在验证桌面启动程序', `正在检查 ${frontendModeLabel(options.expectedMode)}（预期前端模式）入口权限。`, false)
    try {
      if (!invoke) throw new Error('没有检测到桌面原生运行时。')
      const status = await invoke.invoke<unknown>('desktop_status')
      const problem = validateDesktopStatus(status, options.expectedMode)
      if (problem) throw new Error(problem)
      await options.startApplication()
      busy = false
    } catch (reason) {
      busy = false
      show('应用未能启动', reason instanceof Error ? reason.message : String(reason), true, true)
    }
  }

  show('正在验证桌面启动程序', '尚未加载应用界面。', false)
  await attempt()
}

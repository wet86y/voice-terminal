// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, onUnmounted } from 'vue'
import { runDesktopPreflight, type NativeCommandInvoker } from './desktop-preflight'
import { mountApplication } from './mount-application'

const source = readFileSync(resolve(process.cwd(), '../desktop/boot/startup-guard.js'), 'utf8')
type Guard = { start(): void; ready(): void; failed: boolean }
let guard: Guard
let events: EventTarget
let reload: ReturnType<typeof vi.fn>
const status = { ready: true, error: null, role: 'product', frontend_mode: 'product', development: false }

beforeEach(() => {
  vi.useFakeTimers()
  document.body.innerHTML = '<div id="app"><section class="startup-screen"><img class="startup-icon"><p>正在加载</p></section></div>'
  events = new EventTarget(); reload = vi.fn()
  const surface = { setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
    location: { reload }, addEventListener: events.addEventListener.bind(events), __VOICE_STARTUP__: undefined as Guard | undefined }
  new Function('window', 'document', source)(surface, document)
  guard = surface.__VOICE_STARTUP__!
  Object.assign(window, { __VOICE_STARTUP__: guard })
})
afterEach(() => {
  guard.ready()
  delete (window as Window & { __VOICE_STARTUP__?: Guard }).__VOICE_STARTUP__
  vi.useRealTimers(); document.body.innerHTML = ''
})
function native(invoke: (command: string) => Promise<unknown>): NativeCommandInvoker {
  return { invoke: invoke as NativeCommandInvoker['invoke'] }
}

describe('Startup fallback（首屏兜底与挂载交接）', () => {
  it('入口脚本加载失败时显示错误及重新加载操作', () => {
    const event = new Event('error')
    Object.defineProperty(event, 'target', { value: document.createElement('script') })
    events.dispatchEvent(event)
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('界面脚本加载失败')
    document.querySelector<HTMLButtonElement>('button')!.click()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('启动超时后拒绝原生状态的迟到响应，保留可操作的错误画面', async () => {
    let accept!: (value: unknown) => void
    const pending = new Promise(done => { accept = done })
    const mount = vi.fn(async () => undefined)
    const running = runDesktopPreflight({ expectedMode: 'product', invoke: native(() => pending), startApplication: mount })
    await vi.advanceTimersByTimeAsync(30001)
    expect(document.body.textContent).toContain('启动等待超时')
    accept(status); await running
    expect(mount).not.toHaveBeenCalled()
    expect(document.querySelector('[role="alert"]')).not.toBeNull()
  })

  it('原生预检失败后重新尝试会重新开始超时保护', async () => {
    let count = 0
    const call = native(async () => { if (++count === 1) throw new Error('临时读取失败'); return new Promise(() => {}) })
    await runDesktopPreflight({ expectedMode: 'product', invoke: call, startApplication: vi.fn() })
    const retry = [...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '重试')!
    retry.click(); await vi.advanceTimersByTimeAsync(30001)
    expect(guard.failed).toBe(true)
    expect(document.body.textContent).toContain('启动等待超时')
  })

  it('界面已经挂载时，缓慢的原生交接确认不会触发加载超时或清除界面', async () => {
    let complete!: () => void
    const pending = new Promise<void>(done => { complete = done })
    let app: ReturnType<typeof mountApplication> | undefined
    const running = runDesktopPreflight({ expectedMode: 'product', invoke: native(async () => status), startApplication: async () => {
      app = mountApplication(defineComponent(() => () => h('main', '界面已经挂载')))
      await pending
    } })
    await vi.advanceTimersByTimeAsync(31000)
    expect(document.body.textContent).toContain('界面已经挂载')
    expect(document.querySelector('[role="alert"]')).toBeNull()
    complete(); await running; app?.unmount()
  })

  it('首轮组件渲染失败会卸载实例，再显示失败与重试，禁止报告空界面就绪', async () => {
    const stopped = vi.fn()
    await runDesktopPreflight({ expectedMode: 'product', invoke: native(async () => status), startApplication: async () => {
      mountApplication(defineComponent({ setup() { onUnmounted(stopped); throw new Error('首轮组件渲染失败') } }))
    } })
    expect(stopped).toHaveBeenCalledOnce()
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('首轮组件渲染失败')
    expect(document.body.textContent).toContain('重试')
  })

  it('业务未就绪且报错时仍能进入授权的管理页，但拒绝安装器身份', async () => {
    const mount = vi.fn(async () => undefined)
    await runDesktopPreflight({ expectedMode: 'product', management: true, invoke: native(async () => ({ ...status, ready: false, error: '解释器缺失' })), startApplication: mount })
    expect(mount).toHaveBeenCalledOnce()
    await runDesktopPreflight({ expectedMode: 'product', management: true, invoke: native(async () => ({ ...status, role: 'installer' })), startApplication: mount })
    expect(mount).toHaveBeenCalledOnce()
    expect(document.body.textContent).toContain('未获得正式程序授权')
  })
})

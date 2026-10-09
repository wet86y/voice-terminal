// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { runDesktopPreflight, validateDesktopStatus, type NativeCommandInvoker } from './desktop-preflight'
import { APPLICATION_EXIT_EVENT } from './application-motion'

function native(status: unknown): NativeCommandInvoker {
  const call = vi.fn(async (command: string) => command === 'desktop_status' ? status : undefined)
  return { invoke: call as NativeCommandInvoker['invoke'] }
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = '<div id="app"></div>'
})

describe('desktop preflight（桌面预检）', () => {
  it('核验尚未完成时收到原生退出通知，保留退出动效并拒绝迟到的产品挂载', async () => {
    document.body.innerHTML = '<div id="app"></div>'
    const root = document.getElementById('app')!
    let resolve!: (value: unknown) => void
    const pending = new Promise<unknown>(done => { resolve = done })
    const startApplication = vi.fn(async () => undefined)
    const running = runDesktopPreflight({ expectedMode: 'product', root,
      invoke: { invoke: vi.fn(() => pending) as NativeCommandInvoker['invoke'] }, startApplication })
    window.dispatchEvent(new Event(APPLICATION_EXIT_EVENT))
    expect(root.querySelector('.startup-screen')?.classList.contains('is-exiting')).toBe(true)
    expect(root.querySelector('.startup-title')?.textContent).toBe('正在退出应用')
    expect(root.querySelector('.startup-progress')?.getAttribute('aria-label')).toBe('应用退出进度')
    resolve({ ready: true, error: null, role: 'product', frontend_mode: 'product', development: false })
    await running
    expect(startApplication).not.toHaveBeenCalled()
    expect(root.querySelector('.startup-title')?.textContent).toBe('正在退出应用')
  })

  it('等待原生权限验证时可以跳过画面，仍然等验证成功后才加载界面', async () => {
    let resolve!: (value: unknown) => void
    const pending = new Promise<unknown>(done => { resolve = done })
    const call = vi.fn((command: string) => command === 'desktop_status' ? pending : Promise.resolve())
    const startApplication = vi.fn(async () => undefined)
    const onSkip = vi.fn()
    const root = document.createElement('div')
    const running = runDesktopPreflight({ expectedMode: 'product', invoke: { invoke: call as NativeCommandInvoker['invoke'] }, startApplication, onSkip, root })
    const skip = root.querySelector<HTMLButtonElement>('.startup-button')!
    expect(skip.textContent).toBe('跳过')
    skip.click()
    expect(onSkip).toHaveBeenCalledOnce()
    expect(startApplication).not.toHaveBeenCalled()
    resolve({ ready: true, error: null, role: 'product', frontend_mode: 'product', development: false })
    await running
    expect(startApplication).toHaveBeenCalledWith(expect.objectContaining({ startup_skipped: true }))
    expect(call).toHaveBeenCalledWith('skip_startup')
  })

  it('接力原生等待页的跳过选择', async () => {
    const onSkip = vi.fn()
    const startApplication = vi.fn(async () => undefined)
    await runDesktopPreflight({ expectedMode: 'product', invoke: native({ ready: true, error: null, role: 'product', frontend_mode: 'product', development: false, startup_skipped: true }), startApplication, onSkip })
    expect(onSkip).toHaveBeenCalledOnce()
    expect(startApplication).toHaveBeenCalledWith(expect.objectContaining({ startup_skipped: true }))
  })

  it('首屏按钮在模块加载前选择跳过，桌面验证后仍进入设置', async () => {
    const root = document.createElement('div')
    root.dataset.startupSkipped = 'true'
    const startApplication = vi.fn(async () => undefined)
    const onSkip = vi.fn()
    await runDesktopPreflight({ expectedMode: 'product', invoke: native({ ready: true, error: null, role: 'product', frontend_mode: 'product', development: false }), startApplication, onSkip, root })
    expect(onSkip).toHaveBeenCalledOnce()
    expect(startApplication).toHaveBeenCalledWith(expect.objectContaining({ startup_skipped: true }))
  })

  it.each([
    [undefined, '桌面启动程序返回了无效状态'],
    [{ ready: true, error: '宿主启动失败', role: 'product', frontend_mode: 'product', development: false }, '宿主启动失败'],
    [{ ready: false, error: null, role: 'product', frontend_mode: 'product', development: false }, '尚未就绪'],
    [{ ready: true, error: null, role: 'ui-preview', frontend_mode: 'ui-preview', development: true }, '入口不匹配'],
    [{ ready: true, error: null, role: 'product', frontend_mode: 'product', development: 'yes' }, 'development（开发标识）'],
    [{ ready: true, error: null, role: 'product', frontend_mode: 'product' }, 'development（开发标识）'],
  ])('关闭无效状态下的应用导入', async (status, message) => {
    const startApplication = vi.fn(async () => undefined)
    const root = document.createElement('div')
    await runDesktopPreflight({
      expectedMode: 'product',
      invoke: native(status),
      startApplication,
      root,
    })
    expect(startApplication).not.toHaveBeenCalled()
    expect(root.textContent).toContain(message)
    if (message === '入口不匹配') expect(root.textContent).toContain('product（产品）')
    expect(root.querySelector('button')?.textContent).toBe('重试')
  })

  it('拒绝使用正式构建启动 ui-preview（样例）入口', async () => {
    const root = document.createElement('div')
    const startApplication = vi.fn(async () => undefined)
    await runDesktopPreflight({
      expectedMode: 'ui-preview',
      invoke: native({ ready: true, error: null, role: 'ui-preview', frontend_mode: 'ui-preview', development: false }),
      startApplication,
      root,
    })
    expect(startApplication).not.toHaveBeenCalled()
    expect(root.textContent).toContain('ui-preview（样例）入口只允许调试构建启动')
  })

  it('在浏览器缺少原生接口时显示说明，且不执行启动回调', async () => {
    const startApplication = vi.fn(async () => undefined)
    await runDesktopPreflight({ expectedMode: 'ui-preview', invoke: null, startApplication })
    expect(startApplication).not.toHaveBeenCalled()
    expect(document.getElementById('app')?.textContent).toContain('没有检测到桌面原生运行时')
    expect(document.getElementById('app')?.textContent).toContain('浏览器页面不会连接语音服务')
  })

  it('只在状态完整且模式匹配时调用 Vue 应用加载器', async () => {
    const startApplication = vi.fn(async () => undefined)
    const invoke = native({ ready: true, error: null, role: 'ui-preview', frontend_mode: 'ui-preview', development: true })
    await runDesktopPreflight({ expectedMode: 'ui-preview', invoke, startApplication })
    expect(invoke.invoke).toHaveBeenCalledOnce()
    expect(invoke.invoke).toHaveBeenCalledWith('desktop_status')
    expect(startApplication).toHaveBeenCalledOnce()
    expect(validateDesktopStatus({ ready: true, error: null, role: 'product', frontend_mode: 'product', development: false }, 'product')).toBeNull()
    expect(validateDesktopStatus({ ready: true, error: null, role: 'product', frontend_mode: 'product', development: true }, 'product')).toBeNull()
  })

  it('状态失败时不会加载应用配置脚本或请求业务接口', async () => {
    const startApplication = vi.fn(async () => {
      const script = document.createElement('script')
      script.src = '/application-config.js'
      document.head.append(script)
    })
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    await runDesktopPreflight({
      expectedMode: 'product',
      invoke: native({ ready: false, error: null, role: 'product', frontend_mode: 'product', development: false }),
      startApplication,
    })
    expect(startApplication).not.toHaveBeenCalled()
    expect(document.querySelector('script[src="/application-config.js"]')).toBeNull()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('允许错误态重试，并在重试状态通过后启动一次', async () => {
    let status: unknown = { ready: false, error: null, role: 'product', frontend_mode: 'product', development: false }
    const call = vi.fn(async () => status)
    const invoke: NativeCommandInvoker = { invoke: call as NativeCommandInvoker['invoke'] }
    const startApplication = vi.fn(async () => undefined)
    await runDesktopPreflight({ expectedMode: 'product', invoke, startApplication })
    status = { ready: true, error: null, role: 'product', frontend_mode: 'product', development: false }
    document.querySelector('button')?.click()
    await vi.waitFor(() => expect(startApplication).toHaveBeenCalledOnce())
    expect(call).toHaveBeenCalledTimes(2)
  })
})

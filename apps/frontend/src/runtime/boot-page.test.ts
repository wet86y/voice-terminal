// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const html = readFileSync(resolve(process.cwd(), '../desktop/boot/index.html'), 'utf8')
beforeEach(() => vi.useFakeTimers())
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); document.body.innerHTML = '' })

function openBoot(status: (command?: string) => Promise<unknown>) {
  const parsed = new DOMParser().parseFromString(html, 'text/html')
  document.body.innerHTML = parsed.body.innerHTML
  const script = document.querySelector('script')!.textContent!
  document.querySelector('script')!.remove()
  const events = new EventTarget()
  const surface = { __TAURI__: { core: { invoke: vi.fn(status) } }, addEventListener: events.addEventListener.bind(events) }
  new Function('window', 'document', script)(surface, document)
}

describe('Boot page（共享启动与维护画面）', () => {
  it('首次状态读取失败后重试先重新识别入口，不向安装器发送启动业务命令', async () => {
    const commands: string[] = []
    openBoot(async command => {
      commands.push(command!)
      if (commands.length === 1) throw new Error('暂时无法读取状态')
      return { role: 'installer', initialization: { phase: '请选择工作区' } }
    })
    await vi.advanceTimersByTimeAsync(0)
    document.getElementById('retry')!.click()
    await vi.advanceTimersByTimeAsync(0)
    expect(commands).toEqual(['desktop_status', 'desktop_status'])
    expect(document.getElementById('message')?.textContent).toBe('请选择工作区')
    expect((document.getElementById('choose') as HTMLButtonElement).hidden).toBe(false)
  })
  it('完整性维护始终显示真实阶段及字节进度，隐藏安装目录选择入口', async () => {
    let phase = '正在准备完整性校验'
    openBoot(async () => ({ role: 'installer', operation: 'integrity', workspace_busy: true, initialization: { phase, completed_bytes: 25, total_bytes: 100, can_cancel: true } }))
    await vi.advanceTimersByTimeAsync(0)
    expect(document.getElementById('title')?.textContent).toBe('校验与修复')
    expect(document.getElementById('message')?.textContent).toBe(phase)
    expect(document.getElementById('progress')?.getAttribute('aria-valuenow')).toBe('25')
    expect((document.getElementById('choose') as HTMLButtonElement).hidden).toBe(true)
    expect(document.getElementById('cancel')?.textContent).toBe('取消校验')
    phase = '正在校验模型文件内容'
    await vi.advanceTimersByTimeAsync(300)
    expect(document.getElementById('message')?.textContent).toBe(phase)
  })

  it('原生状态读取挂起时保留启动画面并给出超时错误', async () => {
    openBoot(() => new Promise(() => {}))
    expect(document.querySelector('.startup-screen')).not.toBeNull()
    await vi.advanceTimersByTimeAsync(10001)
    expect(document.getElementById('message')?.textContent).toContain('读取启动状态超时')
    expect(document.getElementById('message')?.classList.contains('is-error')).toBe(true)
    expect((document.getElementById('exit') as HTMLButtonElement).hidden).toBe(false)
  })

  it('修复失败显示维修错误而不是安装标题，并保持同版本重试', async () => {
    openBoot(async () => ({ role: 'installer', operation: 'integrity', error: '模型校验失败', initialization: {} }))
    await vi.advanceTimersByTimeAsync(0)
    expect(document.getElementById('title')?.textContent).toBe('完整性校验未完成')
    expect(document.getElementById('message')?.textContent).toBe('模型校验失败')
    expect((document.getElementById('retry') as HTMLButtonElement).hidden).toBe(false)
    expect((document.getElementById('reselect') as HTMLButtonElement).hidden).toBe(true)
  })
})

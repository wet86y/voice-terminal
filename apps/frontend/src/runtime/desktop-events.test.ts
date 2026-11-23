// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { listenDesktopEvent } from './desktop-events'

afterEach(() => vi.unstubAllGlobals())

describe('桌面窗口事件归属', () => {
  it.each([false, true])('定向布局只传给对应网页，代理调试为 %s', async debug => {
    const subscriptions: Array<{ target: { kind: string; label?: string }; callback: (value: unknown) => void }> = []
    async function subscribe(label: string, handler: ReturnType<typeof vi.fn>) {
      let callback: (value: unknown) => void = () => undefined
      vi.stubGlobal('__TAURI_INTERNALS__', {
        metadata: { currentWebview: { label } },
        transformCallback: (value: typeof callback) => { callback = value; return 1 },
        unregisterCallback: vi.fn(),
        invoke: vi.fn(async (command, args) => {
          if (command === 'plugin:event|listen') {
            subscriptions.push({ target: args.target, callback })
            return subscriptions.length
          }
        }),
      })
      vi.stubGlobal('__TAURI_EVENT_PLUGIN_INTERNALS__', { unregisterListener: vi.fn() })
      vi.stubGlobal('__VOICE_AGENT_DEBUG__', debug)
      return listenDesktopEvent('voice-terminal:floating-layout', handler)
    }
    const orb = vi.fn()
    const display = vi.fn()
    const stopOrb = await subscribe('floating', orb)
    const stopDisplay = await subscribe('floating-display', display)
    function emit(label: string | null, payload: unknown) {
      for (const subscription of subscriptions) {
        // target（监听目标）复现 Tauri（桌面运行时）的全局及定向事件筛选规则。
        if (label === null || subscription.target.kind === 'Any' || subscription.target.label === label) {
          subscription.callback({ payload })
        }
      }
    }
    emit('floating', { revision: 4, orb_frame: null })
    expect(orb).toHaveBeenCalledOnce()
    expect(display).not.toHaveBeenCalled()
    emit('floating-display', { revision: 4, active: true, request: { id: 1 } })
    expect(display).toHaveBeenCalledOnce()
    expect(orb).toHaveBeenCalledOnce()
    emit(null, { revision: 5 })
    expect(orb).toHaveBeenCalledTimes(2)
    expect(display).toHaveBeenCalledTimes(2)
    stopOrb()
    stopDisplay()
  })
})

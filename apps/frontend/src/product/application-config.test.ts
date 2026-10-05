// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadProductApplicationConfig, readProductApplicationConfig } from './application-config'
import { runDesktopPreflight } from '../runtime/desktop-preflight'

const validConfig = {
  managed: true,
  origin: 'http://127.0.0.1:42123',
  voice_api_url: '',
  voice_ws_url: '',
}

afterEach(() => {
  document.head.replaceChildren()
  Reflect.deleteProperty(window, '__VOICE_APPLICATION__')
})

describe('Host application configuration（宿主运行配置）', () => {
  it('accepts an initially empty voice endpoint while preserving the managed context', () => {
    expect(readProductApplicationConfig(validConfig)).toEqual(validConfig)
  })

  it.each([
    undefined,
    null,
    {},
    { ...validConfig, managed: false },
    { ...validConfig, origin: '' },
    { ...validConfig, voice_api_url: undefined },
    { ...validConfig, voice_ws_url: null },
  ])('rejects missing or incomplete Host configuration', value => {
    expect(() => readProductApplicationConfig(value)).toThrow()
  })

  it('loads the config script only when requested and rejects a missing Host context', async () => {
    const pending = loadProductApplicationConfig()
    const script = document.querySelector<HTMLScriptElement>('script[data-voice-application-config]')
    expect(script?.src).toBe(`${window.location.origin}/application-config.js`)
    expect(window.__VOICE_APPLICATION__).toBeUndefined()
    window.__VOICE_APPLICATION__ = { ...validConfig }
    script?.dispatchEvent(new Event('load'))
    await expect(pending).resolves.toEqual(validConfig)

    Reflect.deleteProperty(window, '__VOICE_APPLICATION__')
    const missing = loadProductApplicationConfig()
    document.querySelector<HTMLScriptElement>('script[data-voice-application-config]')?.dispatchEvent(new Event('load'))
    await expect(missing).rejects.toThrow('没有提供正式运行配置')
  })

  it('reports a script load failure so the native preflight can offer a retry', async () => {
    const pending = loadProductApplicationConfig()
    document.querySelector<HTMLScriptElement>('script[data-voice-application-config]')?.dispatchEvent(new Event('error'))
    await expect(pending).rejects.toThrow('无法加载应用宿主运行配置')
  })

  it('Host context 缺失时不导入或挂载正式 Vue 应用', async () => {
    const root = document.createElement('div')
    const mountProductApplication = vi.fn()
    const pending = runDesktopPreflight({
      expectedMode: 'product',
      root,
      invoke: { invoke: async <T>() => ({ ready: true, error: null, frontend_mode: 'product', development: false } as T) },
      startApplication: async () => {
        await loadProductApplicationConfig()
        mountProductApplication()
      },
    })
    await vi.waitFor(() => expect(document.querySelector('script[data-voice-application-config]')).not.toBeNull())
    document.querySelector('script[data-voice-application-config]')?.dispatchEvent(new Event('load'))
    await pending
    expect(mountProductApplication).not.toHaveBeenCalled()
    expect(root.textContent).toContain('没有提供正式运行配置')
  })
})

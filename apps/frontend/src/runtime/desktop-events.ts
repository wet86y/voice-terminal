import { locateNativeInvoker } from './desktop-preflight'

/** DesktopEvent（桌面事件）：在窗口内订阅的受控原生通知。 */
export interface DesktopEvent<T> {
  event: string // event（事件名）。
  id: number // id（订阅编号）。
  payload: T // payload（事件数据）。
}

type NativeEventWindow = Window & {
  __TAURI_EVENT_PLUGIN_INTERNALS__?: {
    unregisterListener(event: string, eventId: number): void
  }
}

/** listenDesktopEvent（订阅原生事件）：释放对应的插件和网页回调。 */
export async function listenDesktopEvent<T>(eventName: string, handler: (event: DesktopEvent<T>) => void) {
  const eventWindow = typeof window === 'undefined' ? undefined : window as NativeEventWindow
  const runtime = locateNativeInvoker(eventWindow)
  const plugin = eventWindow?.__TAURI_EVENT_PLUGIN_INTERNALS__
  if (!runtime?.transformCallback || !plugin) throw new Error('桌面事件接口不可用。')
  const callbackId = runtime.transformCallback(handler)
  let eventId: number
  try {
    eventId = await runtime.invoke<number>('plugin:event|listen', {
      event: eventName,
      target: { kind: 'Any' },
      handler: callbackId,
    })
  } catch (error) {
    runtime.unregisterCallback?.(callbackId)
    throw error
  }
  let active = true
  return () => {
    if (!active) return
    active = false
    plugin.unregisterListener(eventName, eventId)
    void runtime.invoke<void>('plugin:event|unlisten', { event: eventName, eventId })
      .finally(() => runtime.unregisterCallback?.(callbackId)).catch(() => undefined)
  }
}

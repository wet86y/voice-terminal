import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import type { FloatingDock, FloatingOrbFrame, FloatingWindowModel } from '../../interface/floating/floating-window'
import type { DisplayEvent, DisplayFrame, DisplayRequest } from '../../interface/floating/floating-display'
import { listenDesktopEvent } from '../../runtime/desktop-events'
import { locateNativeInvoker } from '../../runtime/desktop-preflight'

export type PreviewVoice = 'standby' | 'listening' | 'thinking' | 'speaking' // PreviewVoice（模拟语音状态）。

export interface PreviewSnapshot {
  revision: number // revision（状态递增序号）。
  active: boolean // active（悬浮测试是否启用）。
  dock: FloatingDock // dock（球体吸附边）。
  request: DisplayRequest | null // request（最近提交的展示）。
  visible: number | null // visible（当前可见的展示编号）。
  focused: boolean // focused（展示框受到鼠标关注）。
  orb_frame: FloatingOrbFrame | null // orb_frame（菱形和字幕的贴边布局）。
  display_frame: DisplayFrame | null // display_frame（固定画布中的展示框布局）。
  notice: string // notice（展示校验结果）。
  model: FloatingWindowModel // model（模拟语音球及字幕状态）。
}

/** PreviewAction（测试台操作）：与原生测试台契约共用字段。 */
type PreviewAction =
  | { kind: 'attach' | 'snapshot' | 'ready' | 'clear' | 'return' | 'toggle' }
  | { kind: 'drag'; x: number; y: number }
  | { kind: 'mode'; enabled: boolean }
  | { kind: 'place'; dock: FloatingDock }
  | { kind: 'voice'; state: PreviewVoice; caption: string }
  | ({ kind: 'show' } & Omit<DisplayRequest, 'id'>)
  | DisplayEvent

export function useFloatingPreview(initial: 'attach' | 'ready' | 'snapshot') {
  const native = locateNativeInvoker()
  const snapshot = shallowRef<PreviewSnapshot | null>(null)
  const error = ref('')
  let unlisten: (() => void) | undefined
  let disposed = false

  function accept(value: PreviewSnapshot) {
    if (!disposed && value.revision >= (snapshot.value?.revision ?? 0)) snapshot.value = value
  }

  async function run(action: PreviewAction) {
    if (!native || disposed) return null
    try {
      error.value = ''
      const value = await native.invoke<PreviewSnapshot>('floating_preview_command', { action })
      accept(value)
      return value
    } catch (failure) {
      error.value = failure instanceof Error ? failure.message : String(failure)
      return null
    }
  }

  onMounted(async () => {
    try {
      unlisten = await listenDesktopEvent<PreviewSnapshot>('voice-terminal:floating-preview', event => accept(event.payload))
      if (disposed) { unlisten(); return }
      await run({ kind: initial })
    } catch {
      error.value = '桌面测试窗口未能连接，请重新打开测试台。'
    }
  })
  onBeforeUnmount(() => { disposed = true; unlisten?.() })
  return { snapshot, error, run }
}

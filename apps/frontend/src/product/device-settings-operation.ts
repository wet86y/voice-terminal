import { reactive, readonly } from 'vue'

// 页面卸载只停止展示与轮询；已经发起的准备、授权和保存任务由同一窗口继续跟踪。
const state = reactive({ active: false, phase: '', error: '', revision: 0 })
export const deviceSettingsOperation = readonly(state) // deviceSettingsOperation（窗口共享的设置任务状态，不保存配对码或凭据）。

export function setDeviceSettingsPhase(message: string) {
  state.phase = message
}

export async function runDeviceSettingsOperation(action: () => Promise<void>) {
  if (state.active) return
  state.active = true
  state.phase = '正在准备设备设置…'
  state.error = ''
  state.revision++
  try { await action() }
  catch (failure) { state.error = failure instanceof Error ? failure.message : String(failure) }
  finally { state.active = false }
}

export function recordDeviceSettingsError(message: string) {
  state.error = message
}

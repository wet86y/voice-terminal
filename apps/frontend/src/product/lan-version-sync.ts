import { reactive } from 'vue'
import { nativeDeviceApi, type DeviceVersionCheck } from './native-device-api'
import type { AppUpdateStatus } from '../interface/app-update'

/** lanVersionSync（局域网版本同步状态）：工作台、设备页及后台重连共用。 */
export const lanVersionSync = reactive({
  check: null as DeviceVersionCheck | null, // check（当前配对目标的正式版本预检）。
  open: false, // open（项目弹窗是否显示）。
  busy: false, // busy（已经确认并进入传输或重启等待）。
  waitingRestart: false, // waitingRestart（安装交接后等待目标重新连接）。
  waitingSince: 0, // waitingSince（进入重启等待的本机时间，超时后允许人工重试）。
  canCancel: false, // canCancel（交接之前是否允许取消）。
  completedBytes: 0, // completedBytes（已完成的传输字节）。
  totalBytes: 0, // totalBytes（分发器总字节）。
  phase: '', // phase（用户可读的当前阶段）。
  error: '', // error（失败原因）。
  dismissedKey: '', // dismissedKey（已拒绝的设备与版本组合，不随自动重连重复弹窗）。
})
function key(check: DeviceVersionCheck) { return `${check.host_id}/${check.local_version}/${check.remote_version}` }

/** ensureLanVersion（业务通道前检查版本）：构建编号不参与判断。 */
export async function ensureLanVersion(): Promise<boolean> {
  if (lanVersionSync.busy && !lanVersionSync.waitingRestart) return false
  let check: DeviceVersionCheck
  try { check = await nativeDeviceApi.versionCheck() }
  catch (error) {
    if (lanVersionSync.waitingRestart && Date.now() - lanVersionSync.waitingSince < 180000) return false
    if (lanVersionSync.waitingRestart) {
      lanVersionSync.busy = false
      lanVersionSync.waitingRestart = false
      lanVersionSync.error = '尚未确认目标设备更新后重新连接，请检查目标设备后重试。'
      return false
    }
    throw error
  }
  if (lanVersionSync.check && lanVersionSync.check.host_id !== check.host_id) {
    lanVersionSync.busy = false
    lanVersionSync.waitingRestart = false
    lanVersionSync.dismissedKey = ''
  }
  lanVersionSync.check = check
  if (check.comparison === 'same') {
    lanVersionSync.open = false
    lanVersionSync.busy = false
    lanVersionSync.waitingRestart = false
    lanVersionSync.dismissedKey = ''
    lanVersionSync.error = ''
    return true
  }
  if (lanVersionSync.waitingRestart && (['failed', 'cancelled'].includes(check.remote_update_state || '') || Date.now() - lanVersionSync.waitingSince >= 180000)) {
    lanVersionSync.busy = false
    lanVersionSync.waitingRestart = false
    lanVersionSync.error = check.remote_update_error || '尚未确认目标设备完成更新，请检查目标设备后重试。'
  }
  if (!lanVersionSync.waitingRestart && lanVersionSync.dismissedKey !== key(check)) lanVersionSync.open = true
  return false
}

export function showLanVersionSync() { if (lanVersionSync.check) lanVersionSync.open = true }
export function dismissLanVersionSync() {
  if (lanVersionSync.busy) return
  if (lanVersionSync.check) lanVersionSync.dismissedKey = key(lanVersionSync.check)
  lanVersionSync.open = false
}
export async function confirmLanVersionSync() {
  const check = lanVersionSync.check
  if (!check || !check.can_sync || lanVersionSync.busy) return
  lanVersionSync.busy = true
  lanVersionSync.error = ''
  lanVersionSync.phase = '正在复核版本并准备局域网分发器…'
  lanVersionSync.canCancel = true
  lanVersionSync.completedBytes = 0
  lanVersionSync.totalBytes = 0
  try { updateLanVersionProgress(await nativeDeviceApi.versionSync(check.host_id, check.remote_version)) }
  catch (error) {
    lanVersionSync.busy = false
    lanVersionSync.canCancel = false
    lanVersionSync.error = error instanceof Error ? error.message : String(error)
  }
}
export async function cancelLanVersionSync() {
  if (!lanVersionSync.canCancel) return
  try { await nativeDeviceApi.cancelVersionSync(); lanVersionSync.canCancel = false; lanVersionSync.phase = '正在取消版本同步…' }
  catch (error) { lanVersionSync.error = error instanceof Error ? error.message : String(error) }
}
export function updateLanVersionProgress(status: AppUpdateStatus) {
  if (!lanVersionSync.busy || status.source !== 'lan') return
  // 命令初始回执可能晚于进度事件；已经交接后不能重新开放取消。
  if (lanVersionSync.waitingRestart && ['checking', 'downloading'].includes(status.state)) return
  if (status.state === 'checking' && lanVersionSync.completedBytes > 0) return
  lanVersionSync.phase = status.phase || lanVersionSync.phase
  lanVersionSync.completedBytes = status.completed_bytes ?? 0
  lanVersionSync.totalBytes = status.total_bytes ?? 0
  lanVersionSync.canCancel = status.can_cancel === true
  if (status.state === 'failed' || status.state === 'cancelled') {
    lanVersionSync.busy = false
    lanVersionSync.waitingRestart = false
    lanVersionSync.error = status.error || (status.state === 'cancelled' ? '版本同步已取消，可重新同步。' : '版本同步未完成。')
  } else if (status.state === 'current' || status.state === 'handing_off') {
    if (!lanVersionSync.waitingRestart) lanVersionSync.waitingSince = Date.now()
    lanVersionSync.waitingRestart = true
    lanVersionSync.canCancel = false
  }
}

import { watch } from 'vue'
import { matchesRemoteManagementEnvelope, nativeDeviceApi } from './native-device-api'
import { deviceState } from './device-state'
import { ensureLanVersion } from './lan-version-sync'

export type RemoteClientRuntimeState =
  | { state: 'ready'; core_state: 'ready'; task: Awaited<ReturnType<typeof nativeDeviceApi.remoteTaskChannel>>; media: Awaited<ReturnType<typeof nativeDeviceApi.remoteMediaChannel>> }
  | { state: 'management_only'; core_state: 'starting' | 'unavailable' }
  | { state: 'waiting'; reason: 'local_mode' | 'pairing_required' | 'reconnect_suspended' | 'version_mismatch' }

let startupInFlight: Promise<RemoteClientRuntimeState> | null = null
let supervisorStop: (() => void) | null = null
let versionBlockedHost: string | null = null // versionBlockedHost（已经停止旧业务通道的异版目标）。
// 上次待机申请对应的任务会话和语音租约代次。切换或重连后才重新申请。
let standby: { host: string; connection: number | undefined; executor: number | undefined; interaction: number | undefined } | null = null

/** ensureRemoteClientRuntime（准备已配对远端客户端）：单例化连接、任务与语音通道启动。 */
export function ensureRemoteClientRuntime(): Promise<RemoteClientRuntimeState> {
  if (startupInFlight) return startupInFlight
  startupInFlight = (async () => {
    const status = await nativeDeviceApi.deviceConfigStatus()
    if (status.runtime.mode !== 'remote' || status.device_config?.mode !== 'remote') {
      return { state: 'waiting', reason: 'local_mode' } as const
    }
    const config = status.device_config
    if (!config.paired_host) return { state: 'waiting', reason: 'pairing_required' } as const
    if (config.reconnect_suspended_reason) return { state: 'waiting', reason: 'reconnect_suspended' } as const

    const host = await nativeDeviceApi.sshHostStatus()
    if (!host.connected) await nativeDeviceApi.connectSshHost()

    if (!await ensureLanVersion()) {
      if (versionBlockedHost !== config.paired_host.host_id) {
        await nativeDeviceApi.remoteMediaChannel('stop')
        await nativeDeviceApi.remoteTaskChannel('stop')
        standby = null
        versionBlockedHost = config.paired_host.host_id
      }
      return { state: 'waiting', reason: 'version_mismatch' } as const
    }
    versionBlockedHost = null

    let task = await nativeDeviceApi.remoteTaskChannel('status')
    if (task.state !== 'running') {
      const manager = await nativeDeviceApi.remoteManagementRequest('management.status', {})
      if (manager.status !== 200 || !matchesRemoteManagementEnvelope(manager, config.paired_host.host_id, 'management.status')) {
        throw new Error(typeof manager.body?.detail === 'string' ? manager.body.detail : '服务端原生管理服务尚未就绪。')
      }
      const core = manager.body.core_state
      if (core !== 'ready') {
        return { state: 'management_only', core_state: core === 'starting' ? 'starting' : 'unavailable' } as const
      }
      task = await nativeDeviceApi.remoteTaskChannel('start')
    }
    if (task.state !== 'running' || task.device_id !== config.device_id) {
      throw new Error(task.error || '远程服务端任务通道尚未就绪。')
    }

    let media = await nativeDeviceApi.remoteMediaChannel('status')
    const snapshot = deviceState.value?.self_device_id === config.device_id ? deviceState.value : null
    const scope = {
      host: config.paired_host.host_id, connection: task.connection_generation,
      executor: task.executor_generation, interaction: snapshot?.interaction_generation,
    }
    if (media.state !== 'running') {
      if (standby?.host === scope.host && standby.connection === scope.connection
          && standby.executor === scope.executor && standby.interaction === scope.interaction) {
        media = { state: 'standby', fields: { state: '等待服务端选择本设备' } }
      } else {
        media = await nativeDeviceApi.remoteMediaChannel('start')
      }
      if (media.state === 'standby') standby = scope
      else if (media.state !== 'running') throw new Error(media.error || '远程服务端语音通道尚未就绪。')
    }
    if (media.state === 'running') standby = null
    return { state: 'ready', core_state: 'ready', task, media } as const
  })().finally(() => { startupInFlight = null })
  return startupInFlight
}

/** startRemoteClientRuntimeSupervisor（远程客户端后台监督）：连接与通道故障按退避节奏恢复。 */
export function startRemoteClientRuntimeSupervisor(): () => void {
  if (supervisorStop) return supervisorStop
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let failures = 0
  const retryDelays = [1000, 2000, 4000, 8000, 15000]

  const schedule = (delay: number) => {
    if (stopped) return
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(() => { void run() }, delay)
  }
  const run = async () => {
    if (stopped) return
    try {
      const state = await ensureRemoteClientRuntime()
      if (state.state === 'ready' || state.state === 'management_only') {
        failures = 0
        schedule(state.state === 'ready' ? 2000 : 3000)
      } else {
        schedule(state.reason === 'version_mismatch' ? 3000 : 15000)
      }
    } catch {
      const delay = retryDelays[Math.min(failures, retryDelays.length - 1)]
      failures = Math.min(failures + 1, retryDelays.length - 1)
      schedule(delay)
    }
  }
  const unsubscribe = watch(() => deviceState.value?.pending_device_id, deviceId => {
    if (deviceId && deviceId === deviceState.value?.self_device_id) schedule(0)
  })
  const stop = () => {
    unsubscribe()
    standby = null
    versionBlockedHost = null
    if (stopped) return
    stopped = true
    if (timer !== null) clearTimeout(timer)
    timer = null
    if (supervisorStop === stop) supervisorStop = null
    if (typeof window !== 'undefined') window.removeEventListener('pagehide', stop)
  }
  supervisorStop = stop
  if (typeof window !== 'undefined') window.addEventListener('pagehide', stop, { once: true })
  void run()
  return stop
}

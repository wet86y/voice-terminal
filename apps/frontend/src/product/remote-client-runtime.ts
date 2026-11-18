import { matchesRemoteManagementEnvelope, nativeDeviceApi } from './native-device-api'

export type RemoteClientRuntimeState =
  | { state: 'ready'; core_state: 'ready'; task: Awaited<ReturnType<typeof nativeDeviceApi.remoteTaskChannel>>; media: Awaited<ReturnType<typeof nativeDeviceApi.remoteMediaChannel>> }
  | { state: 'management_only'; core_state: 'starting' | 'unavailable' }
  | { state: 'waiting'; reason: 'local_mode' | 'pairing_required' | 'reconnect_suspended' }

let startupInFlight: Promise<RemoteClientRuntimeState> | null = null
let supervisorStop: (() => void) | null = null
interface LastOwnedInteraction {
  connection_generation: number // connection_generation（上次确认所有权时的设备连接代次）。
  executor_generation: number // executor_generation（上次确认所有权时的设备执行器代次）。
  interaction_generation: number // interaction_generation（上次确认所有权时的语音占用代次）。
}

interface RemoteDeviceOwnership {
  state: 'owned' | 'taken_over' // state（当前租约是否仍属于本客户端）。
  interaction_generation: number // interaction_generation（本次快照中的语音占用代次）。
}

const lastOwnedInteraction = new Map<string, LastOwnedInteraction>()

function remoteClientScope(hostId: string, deviceId: string) { return `${hostId}\u0000${deviceId}` }

async function suspendTakenOverClient(config: NonNullable<Awaited<ReturnType<typeof nativeDeviceApi.deviceConfigStatus>>['device_config']>) {
  await nativeDeviceApi.saveDeviceConfig({ ...config, reconnect_suspended_reason: 'device_taken_over' })
  lastOwnedInteraction.delete(remoteClientScope(config.paired_host?.host_id ?? '', config.device_id))
  // stop（停止）在管道租约可能已过期时允许失败；先持久化暂停原因，避免清理错误触发自动重连。
  await Promise.allSettled([
    nativeDeviceApi.remoteMediaChannel('stop'),
    nativeDeviceApi.remoteTaskChannel('stop'),
  ])
  await nativeDeviceApi.disconnectSshHost().catch(() => undefined)
}

async function remoteDeviceSnapshot(hostId: string, deviceId: string, taskConnectionGeneration: number,
  taskExecutorGeneration: number, taskWasAlreadyRunning: boolean,
  mediaInteractionGeneration?: number): Promise<RemoteDeviceOwnership> {
  const snapshot = await nativeDeviceApi.remoteDeviceSnapshot()
  const ownDevice = snapshot.devices.find(row => row.device_id === deviceId)
  if (!ownDevice || !ownDevice.connected || !Number.isSafeInteger(ownDevice.connection_generation)
    || ownDevice.connection_generation < 1 || !Number.isSafeInteger(ownDevice.executor_generation)
    || ownDevice.executor_generation < 1 || !Number.isSafeInteger(snapshot.interaction_generation)
    || snapshot.interaction_generation < 0) {
    throw new Error('服务端设备租约快照尚未登记本客户端。')
  }
  const scope = remoteClientScope(hostId, deviceId)
  const lastOwned = lastOwnedInteraction.get(scope)
  const connectionGenerationReplaced = ownDevice.connection_generation > taskConnectionGeneration
  const executorGenerationReplaced = ownDevice.executor_generation > taskExecutorGeneration
  if (connectionGenerationReplaced || executorGenerationReplaced) {
    return { state: 'taken_over', interaction_generation: snapshot.interaction_generation }
  }
  if (ownDevice.connection_generation !== taskConnectionGeneration
    || ownDevice.executor_generation !== taskExecutorGeneration) {
    throw new Error('服务端设备快照的连接或执行器代次落后于本地任务通道。')
  }

  const anotherRemoteDeviceActive = snapshot.active_device_id !== null
    && snapshot.active_device_id !== 'local' && snapshot.active_device_id !== deviceId
  const previousLeaseGeneration = mediaInteractionGeneration ?? lastOwned?.interaction_generation
  const previouslyOwned = taskWasAlreadyRunning || lastOwned !== undefined
  const anotherClientAdvancedLease = anotherRemoteDeviceActive && previousLeaseGeneration !== undefined
    && snapshot.interaction_generation > previousLeaseGeneration
  if (anotherRemoteDeviceActive && (previouslyOwned || anotherClientAdvancedLease)) {
    return { state: 'taken_over', interaction_generation: snapshot.interaction_generation }
  }

  if (snapshot.active_device_id === deviceId) {
    lastOwnedInteraction.set(scope, {
      connection_generation: taskConnectionGeneration,
      executor_generation: taskExecutorGeneration,
      interaction_generation: snapshot.interaction_generation,
    })
  }
  return { state: 'owned', interaction_generation: snapshot.interaction_generation }
}

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

    const manager = await nativeDeviceApi.remoteManagementRequest('management.status', {})
    if (manager.status !== 200 || !manager.body || typeof manager.body !== 'object'
      || !matchesRemoteManagementEnvelope(manager, config.paired_host.host_id, 'management.status')) {
      throw new Error(typeof manager.body?.detail === 'string' ? manager.body.detail : '服务端原生管理服务尚未就绪。')
    }
    const coreState = manager.body.core_state
    if (coreState !== 'ready') {
      return { state: 'management_only', core_state: coreState === 'starting' ? 'starting' : 'unavailable' } as const
    }

    const taskStatus = await nativeDeviceApi.remoteTaskChannel('status')
    const taskWasAlreadyRunning = taskStatus.state === 'running'
    const task = taskStatus.state === 'running'
      ? taskStatus
      : await nativeDeviceApi.remoteTaskChannel('start')
    if (task.state !== 'running') throw new Error(task.error || '远程服务端任务通道尚未就绪。')
    if (task.device_id !== config.device_id) {
      throw new Error('远程服务端任务通道返回了不匹配的设备编号。')
    }
    const taskConnectionGeneration = task.connection_generation
    const taskExecutorGeneration = task.executor_generation
    if (typeof taskConnectionGeneration !== 'number' || !Number.isSafeInteger(taskConnectionGeneration)
      || taskConnectionGeneration < 1 || typeof taskExecutorGeneration !== 'number'
      || !Number.isSafeInteger(taskExecutorGeneration) || taskExecutorGeneration < 1) {
      throw new Error('远程服务端任务通道没有返回有效连接或执行器代次。')
    }

    // 已运行客户端再次恢复时，若服务端已将租约授予其他设备，或连接/执行器代次已被替换，
    // 先持久暂停自动重连，避免下一次媒体启动把租约抢回来。首次连接仍按最新就绪客户端规则取得租约。
    const taskOwner = await remoteDeviceSnapshot(
      config.paired_host.host_id, config.device_id, taskConnectionGeneration,
      taskExecutorGeneration, taskWasAlreadyRunning,
    )
    if (taskOwner.state === 'taken_over') {
      await suspendTakenOverClient(config)
      return { state: 'waiting', reason: 'reconnect_suspended' } as const
    }

    const mediaStatus = await nativeDeviceApi.remoteMediaChannel('status')
    const media = mediaStatus.state === 'running'
      ? mediaStatus
      : await nativeDeviceApi.remoteMediaChannel('start')
    if (media.state !== 'running') throw new Error(media.error || '远程服务端语音通道尚未就绪。')
    const mediaGenerations = media as typeof media & {
      connection_generation?: number // connection_generation（媒体回执声明的设备连接代次）。
      executor_generation?: number // executor_generation（媒体回执声明的设备执行器代次）。
    }
    const mediaConnectionGeneration = mediaGenerations.connection_generation
    const mediaExecutorGeneration = mediaGenerations.executor_generation
    const mediaInteractionGeneration = media.interaction_generation
    if (media.device_id !== config.device_id || mediaConnectionGeneration !== undefined
      && (typeof mediaConnectionGeneration !== 'number' || !Number.isSafeInteger(mediaConnectionGeneration)
        || mediaConnectionGeneration !== taskConnectionGeneration)
      || mediaExecutorGeneration !== undefined && (typeof mediaExecutorGeneration !== 'number'
        || !Number.isSafeInteger(mediaExecutorGeneration) || mediaExecutorGeneration !== taskExecutorGeneration)
      || typeof mediaInteractionGeneration !== 'number'
      || !Number.isSafeInteger(mediaInteractionGeneration) || mediaInteractionGeneration < 1) {
      throw new Error('远程服务端语音通道返回了不匹配的设备或租约代次。')
    }
    const mediaOwner = await remoteDeviceSnapshot(
      config.paired_host.host_id, config.device_id, taskConnectionGeneration,
      taskExecutorGeneration, taskWasAlreadyRunning, mediaInteractionGeneration,
    )
    if (mediaOwner.state === 'taken_over') {
      await suspendTakenOverClient(config)
      return { state: 'waiting', reason: 'reconnect_suspended' } as const
    }
    // start（启动）会以最新快照准备租约；代次多跳表示检查后另一个设备先接管，再被本次启动抢回。
    if (mediaStatus.state !== 'running') {
      const expectedInteractionGeneration = taskOwner.interaction_generation + 1
      if (mediaInteractionGeneration > expectedInteractionGeneration) {
        await suspendTakenOverClient(config)
        return { state: 'waiting', reason: 'reconnect_suspended' } as const
      }
      if (!Number.isSafeInteger(expectedInteractionGeneration)
        || mediaInteractionGeneration !== expectedInteractionGeneration) {
        throw new Error('远程服务端语音租约代次与启动前快照不连续。')
      }
    }
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
        schedule(15000)
      }
    } catch {
      const delay = retryDelays[Math.min(failures, retryDelays.length - 1)]
      failures = Math.min(failures + 1, retryDelays.length - 1)
      schedule(delay)
    }
  }
  const stop = () => {
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

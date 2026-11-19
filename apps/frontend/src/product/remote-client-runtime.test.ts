import { afterEach, describe, expect, it, vi } from 'vitest'
import { nativeDeviceApi, type DeviceConfigStatus } from './native-device-api'
import { ensureRemoteClientRuntime } from './remote-client-runtime'
import { deviceState, type DeviceState } from './device-state'

const connectionGeneration = 4
const executorGeneration = 2

function remoteStatus(paired: boolean, suspended: boolean, deviceId = 'device-a'): DeviceConfigStatus {
  const status = {
    runtime: { mode: 'remote', config_state: 'valid', device_id: deviceId },
    device_config: {
      schema_version: 3, device_id: deviceId, mode: 'remote', reconnect_suspended_reason: suspended ? 'user_disconnected' : null,
      paired_host: paired ? { host_id: 'server-a', display_name: '服务端 A' } : null,
    },
  }
  return status as DeviceConfigStatus
}

function mockReadyManager(deviceId = 'device-a') {
  vi.spyOn(nativeDeviceApi, 'deviceConfigStatus').mockResolvedValue(remoteStatus(true, false, deviceId))
  vi.spyOn(nativeDeviceApi, 'sshHostStatus').mockResolvedValue({ state: 'connected', connected: true, fields: {} })
  vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue({
    request_id: 'a'.repeat(32), target_host_id: 'server-a', session_generation: 4,
    operation: 'management.status', status: 200, body: { core_state: 'ready' }, fields: {},
  })
}

function runningTask(deviceId: string, currentConnectionGeneration = connectionGeneration,
  currentExecutorGeneration = executorGeneration) {
  return {
    state: 'running' as const, device_id: deviceId,
    connection_generation: currentConnectionGeneration, executor_generation: currentExecutorGeneration, fields: {},
  }
}

function runningMedia(deviceId: string, interactionGeneration: number,
  currentConnectionGeneration = connectionGeneration, currentExecutorGeneration = executorGeneration) {
  return {
    state: 'running' as const, device_id: deviceId,
    connection_generation: currentConnectionGeneration, executor_generation: currentExecutorGeneration,
    interaction_generation: interactionGeneration, fields: {},
  }
}

describe('remote-client-runtime（远程客户端运行监督）', () => {
  afterEach(() => vi.restoreAllMocks())

  it('已配对远程模式会自动建立 SSH、任务与语音通道', async () => {
    mockReadyManager()
    vi.spyOn(nativeDeviceApi, 'sshHostStatus').mockResolvedValue({ state: 'disconnected', connected: false, fields: {} })
    vi.spyOn(nativeDeviceApi, 'connectSshHost').mockResolvedValue({
      state: 'ready', device_id: 'device-a', address: '192.168.1.20', port: 22,
      host_key_sha256: 'SHA256:host', gateway_port: 18765, fields: {},
    })
    const tasks = vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockImplementation(async action => {
      if (action === 'status') return { state: 'stopped', fields: {} }
      if (action === 'start') return runningTask('device-a')
      return { state: 'stopped', fields: {} }
    })
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel').mockImplementation(async action => {
      if (action === 'status') return { state: 'stopped', fields: {} }
      if (action === 'start') return runningMedia('device-a', 10)
      return { state: 'stopped', fields: {} }
    })
    const snapshots = vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot')

    const state = await ensureRemoteClientRuntime()

    expect(state.state).toBe('ready')
    expect(nativeDeviceApi.connectSshHost).toHaveBeenCalledOnce()
    expect(tasks).toHaveBeenNthCalledWith(1, 'status')
    expect(tasks).toHaveBeenNthCalledWith(2, 'start')
    expect(media).toHaveBeenNthCalledWith(1, 'status')
    expect(media).toHaveBeenNthCalledWith(2, 'start')
    expect(snapshots).not.toHaveBeenCalled()
  })

  it('未配对或用户暂停重连时等待，不触发 SSH 连接', async () => {
    const status = vi.spyOn(nativeDeviceApi, 'deviceConfigStatus')
    status.mockResolvedValueOnce(remoteStatus(false, false)).mockResolvedValueOnce(remoteStatus(true, true))
    const connect = vi.spyOn(nativeDeviceApi, 'connectSshHost')

    await expect(ensureRemoteClientRuntime()).resolves.toEqual({ state: 'waiting', reason: 'pairing_required' })
    await expect(ensureRemoteClientRuntime()).resolves.toEqual({ state: 'waiting', reason: 'reconnect_suspended' })
    expect(connect).not.toHaveBeenCalled()
  })

  it('核心未就绪时保持原生管理连接，不尝试启动核心依赖的设备通道', async () => {
    vi.spyOn(nativeDeviceApi, 'deviceConfigStatus').mockResolvedValue(remoteStatus(true, false))
    vi.spyOn(nativeDeviceApi, 'sshHostStatus').mockResolvedValue({ state: 'connected', connected: true, fields: {} })
    vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue({
      request_id: 'a'.repeat(32), target_host_id: 'server-a', session_generation: 4,
      operation: 'management.status', status: 200, body: { core_state: 'unavailable' }, fields: {},
    })
    const tasks = vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockResolvedValue({ state: 'stopped', fields: {} })
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel')

    await expect(ensureRemoteClientRuntime()).resolves.toEqual({ state: 'management_only', core_state: 'unavailable' })
    expect(tasks).not.toHaveBeenCalledWith('start')
    expect(media).not.toHaveBeenCalled()
  })

  it.each([
    ['目标主机', { target_host_id: 'server-b' }],
    ['操作名称', { operation: 'resource.jobs' }],
    ['正整数代次', { session_generation: 0 }],
  ])('管理状态回执的%s无效时不启动任务或语音通道', async (_label, invalid) => {
    vi.spyOn(nativeDeviceApi, 'deviceConfigStatus').mockResolvedValue(remoteStatus(true, false))
    vi.spyOn(nativeDeviceApi, 'sshHostStatus').mockResolvedValue({ state: 'connected', connected: true, fields: {} })
    vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue({
      request_id: 'a'.repeat(32), target_host_id: 'server-a', session_kind: 'management', session_generation: 4,
      operation: 'management.status', status: 200, body: { core_state: 'ready' }, fields: {}, ...invalid,
    } as never)
    const tasks = vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockResolvedValue({ state: 'stopped', fields: {} })
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel')

    await expect(ensureRemoteClientRuntime()).rejects.toThrow('服务端原生管理服务尚未就绪')
    expect(tasks).not.toHaveBeenCalledWith('start')
    expect(media).not.toHaveBeenCalled()
  })

  it('另一设备持有语音时保留管理和任务连接，服务端改选后才再次申请媒体', async () => {
    const id = 'device-standby'
    mockReadyManager(id)
    const tasks = vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockResolvedValue(runningTask(id))
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel').mockImplementation(async action => ({
      state: action === 'start' ? 'standby' : 'stopped', fields: {},
    }))
    const disconnect = vi.spyOn(nativeDeviceApi, 'disconnectSshHost')
    deviceState.value = { self_device_id: id, active_device_id: 'device-b', interaction_generation: 12 } as DeviceState

    await expect(ensureRemoteClientRuntime()).resolves.toMatchObject({ state: 'ready', media: { state: 'standby' } })
    await expect(ensureRemoteClientRuntime()).resolves.toMatchObject({ state: 'ready', media: { state: 'standby' } })
    expect(media.mock.calls.filter(([action]) => action === 'start')).toHaveLength(1)
    expect(tasks).not.toHaveBeenCalledWith('stop')
    expect(disconnect).not.toHaveBeenCalled()
    expect(nativeDeviceApi.remoteManagementRequest).not.toHaveBeenCalled()

    deviceState.value = { ...deviceState.value, pending_device_id: id, interaction_generation: 13 }
    media.mockImplementation(async action => action === 'start' ? runningMedia(id, 13) : { state: 'stopped', fields: {} })
    await expect(ensureRemoteClientRuntime()).resolves.toMatchObject({ state: 'ready', media: { state: 'running' } })
    expect(media.mock.calls.filter(([action]) => action === 'start')).toHaveLength(2)
    deviceState.value = null
  })
})

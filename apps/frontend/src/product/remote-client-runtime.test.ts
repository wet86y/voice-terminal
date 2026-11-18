import { afterEach, describe, expect, it, vi } from 'vitest'
import { nativeDeviceApi, type DeviceConfigStatus, type RemoteDeviceSnapshot } from './native-device-api'
import { ensureRemoteClientRuntime } from './remote-client-runtime'

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

function remoteSnapshot(deviceId: string, activeDeviceId: string | null, interactionGeneration: number,
  currentConnectionGeneration = connectionGeneration, currentExecutorGeneration = executorGeneration): RemoteDeviceSnapshot {
  return {
    protocol_version: 1, ready: true, active_device_id: activeDeviceId, interaction_generation: interactionGeneration,
    devices: [{
      device_id: deviceId, name: '远程客户端', connected: true,
      connection_generation: currentConnectionGeneration, executor_generation: currentExecutorGeneration,
      capabilities: ['audio_stream'], error: null, interaction_active: activeDeviceId === deviceId,
    }],
    capability_labels: {}, fields: {},
  }
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
    vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot')
      .mockResolvedValueOnce(remoteSnapshot('device-a', 'local', 9))
      .mockResolvedValueOnce(remoteSnapshot('device-a', 'device-a', 10))

    const state = await ensureRemoteClientRuntime()

    expect(state.state).toBe('ready')
    expect(nativeDeviceApi.connectSshHost).toHaveBeenCalledOnce()
    expect(tasks).toHaveBeenNthCalledWith(1, 'status')
    expect(tasks).toHaveBeenNthCalledWith(2, 'start')
    expect(media).toHaveBeenNthCalledWith(1, 'status')
    expect(media).toHaveBeenNthCalledWith(2, 'start')
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
    const tasks = vi.spyOn(nativeDeviceApi, 'remoteTaskChannel')
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel')

    await expect(ensureRemoteClientRuntime()).resolves.toEqual({ state: 'management_only', core_state: 'unavailable' })
    expect(tasks).not.toHaveBeenCalled()
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
    const tasks = vi.spyOn(nativeDeviceApi, 'remoteTaskChannel')
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel')

    await expect(ensureRemoteClientRuntime()).rejects.toThrow('服务端原生管理服务尚未就绪')
    expect(tasks).not.toHaveBeenCalled()
    expect(media).not.toHaveBeenCalled()
  })

  it('模块重启后发现另一设备已取得租约时暂停自动重连，不重新启动媒体通道', async () => {
    const deviceId = 'device-restarted'
    mockReadyManager(deviceId)
    vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockImplementation(async action => {
      if (action === 'status') return runningTask(deviceId)
      return { state: 'stopped', fields: {} }
    })
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel').mockImplementation(async () => ({
      state: 'stopped', fields: {},
    }))
    vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot').mockResolvedValue(
      remoteSnapshot(deviceId, 'device-b', 12),
    )
    const saveConfig = vi.spyOn(nativeDeviceApi, 'saveDeviceConfig').mockResolvedValue(remoteStatus(true, true, deviceId))
    vi.spyOn(nativeDeviceApi, 'disconnectSshHost').mockResolvedValue({
      state: 'disconnected', disconnected: true, fields: {},
    })

    await expect(ensureRemoteClientRuntime()).resolves.toEqual({ state: 'waiting', reason: 'reconnect_suspended' })

    expect(saveConfig).toHaveBeenCalledWith(expect.objectContaining({
      device_id: deviceId, reconnect_suspended_reason: 'device_taken_over',
    }))
    expect(media).toHaveBeenCalledWith('stop')
    expect(media).not.toHaveBeenCalledWith('start')
    expect(nativeDeviceApi.remoteTaskChannel).toHaveBeenCalledWith('stop')
  })

  it('任务通道已运行但服务端选择本机时仍允许媒体连接', async () => {
    const deviceId = 'device-local-fallback'
    mockReadyManager(deviceId)
    const tasks = vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockImplementation(async action => {
      if (action === 'status') return runningTask(deviceId)
      return { state: 'stopped', fields: {} }
    })
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel').mockImplementation(async action => {
      if (action === 'status') return { state: 'stopped', fields: {} }
      if (action === 'start') return runningMedia(deviceId, 10)
      return { state: 'stopped', fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot')
      .mockResolvedValueOnce(remoteSnapshot(deviceId, 'local', 9))
      .mockResolvedValueOnce(remoteSnapshot(deviceId, deviceId, 10))
    const saveConfig = vi.spyOn(nativeDeviceApi, 'saveDeviceConfig')
    const disconnect = vi.spyOn(nativeDeviceApi, 'disconnectSshHost')

    await expect(ensureRemoteClientRuntime()).resolves.toMatchObject({ state: 'ready' })

    expect(media).toHaveBeenCalledWith('start')
    expect(saveConfig).not.toHaveBeenCalled()
    expect(tasks).not.toHaveBeenCalledWith('stop')
    expect(disconnect).not.toHaveBeenCalled()
  })

  it('设备快照的连接代次落后时拒绝启动媒体通道', async () => {
    const deviceId = 'device-stale-connection'
    mockReadyManager(deviceId)
    vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockImplementation(async action => {
      if (action === 'status') return runningTask(deviceId, 8, executorGeneration)
      return { state: 'stopped', fields: {} }
    })
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel')
    vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot').mockResolvedValue(
      remoteSnapshot(deviceId, 'local', 11, 7, executorGeneration),
    )

    await expect(ensureRemoteClientRuntime()).rejects.toThrow('连接或执行器代次落后')

    expect(media).not.toHaveBeenCalled()
  })

  it('执行器已被新代次取代时暂停旧任务通道的自动恢复', async () => {
    const deviceId = 'device-restarted-executor'
    mockReadyManager(deviceId)
    vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockImplementation(async action => {
      if (action === 'status') return runningTask(deviceId, connectionGeneration, executorGeneration)
      return { state: 'stopped', fields: {} }
    })
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel').mockImplementation(async () => ({
      state: 'stopped', fields: {},
    }))
    vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot').mockResolvedValue(
      remoteSnapshot(deviceId, deviceId, 13, connectionGeneration, executorGeneration + 1),
    )
    vi.spyOn(nativeDeviceApi, 'saveDeviceConfig').mockResolvedValue(remoteStatus(true, true, deviceId))
    vi.spyOn(nativeDeviceApi, 'disconnectSshHost').mockResolvedValue({
      state: 'disconnected', disconnected: true, fields: {},
    })

    await expect(ensureRemoteClientRuntime()).resolves.toEqual({ state: 'waiting', reason: 'reconnect_suspended' })

    expect(media).not.toHaveBeenCalledWith('start')
    expect(nativeDeviceApi.remoteTaskChannel).toHaveBeenCalledWith('stop')
  })

  it('媒体启动期间租约代次跳变时暂停自动重连，避免把接管抢回', async () => {
    const deviceId = 'device-start-race'
    mockReadyManager(deviceId)
    vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockImplementation(async action => {
      if (action === 'status') return { state: 'stopped', fields: {} }
      if (action === 'start') return runningTask(deviceId)
      return { state: 'stopped', fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot')
      .mockResolvedValueOnce(remoteSnapshot(deviceId, 'local', 10))
      .mockResolvedValueOnce(remoteSnapshot(deviceId, deviceId, 12))
    const media = vi.spyOn(nativeDeviceApi, 'remoteMediaChannel').mockImplementation(async action => {
      if (action === 'status') return { state: 'stopped', fields: {} }
      if (action === 'start') return runningMedia(deviceId, 12)
      return { state: 'stopped', fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'saveDeviceConfig').mockResolvedValue(remoteStatus(true, true, deviceId))
    vi.spyOn(nativeDeviceApi, 'disconnectSshHost').mockResolvedValue({
      state: 'disconnected', disconnected: true, fields: {},
    })

    await expect(ensureRemoteClientRuntime()).resolves.toEqual({ state: 'waiting', reason: 'reconnect_suspended' })

    expect(media).toHaveBeenCalledWith('start')
    expect(media).toHaveBeenCalledWith('stop')
    expect(nativeDeviceApi.remoteTaskChannel).toHaveBeenCalledWith('stop')
  })
})

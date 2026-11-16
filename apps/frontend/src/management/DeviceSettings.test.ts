// @vitest-environment jsdom
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nativeDeviceApi, type DeviceConfigStatus, type NativeSshIdentity, type NativeSshTransportIdentity, type OpenSshServerStatus, type RemoteDeviceSnapshot } from '../product/native-device-api'
import { productApi, type DevicePairingStatus } from '../product/api'
import DeviceSettings from './DeviceSettings.vue'

const readySharedHostRuntime: NonNullable<DeviceConfigStatus['device_runtime']> = {
  actual_role: 'shared_host', core_ready: true,
  device_gateway: { enabled: true, ready: true, error: null },
  remote_client_connected: false, fields: {},
}

describe('DeviceSettings（设备设置）远端设备快照', () => {
  let wrapper: VueWrapper | undefined
  const requestGateway = vi.fn()
  const readSnapshot = vi.fn()
  const startWorker = vi.fn()
  const readHelperHealth = vi.fn()
  const taskChannel = vi.fn()
  const mediaChannel = vi.fn()
  const readLocalSnapshot = vi.fn()
  const selectLocalDevice = vi.fn()

  beforeEach(() => {
    const config: DeviceConfigStatus = {
      runtime: { config_state: 'valid', device_id: 'client-a', desired_role: 'remote_client', audio_mode: 'pcm', error: null },
      device_config: {
        schema_version: 2,
        device_id: 'client-a',
        desired_role: 'remote_client',
        audio_mode: 'pcm',
        host: {
          address: '192.0.2.10', port: 22, username: 'voice',
          host_key_sha256: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
          credential_ref: 'device-ssh:client-a',
        },
      },
      fields: {},
    }
    const snapshot: RemoteDeviceSnapshot = {
      protocol_version: 1,
      ready: true,
      active_device_id: 'host-local',
      interaction_generation: 3,
      devices: [{ device_id: 'host-local', name: '主机本机', connected: true, connection_generation: 3,
        executor_generation: 2, capabilities: ['screen_capture'], error: null, interaction_active: true }],
      capability_labels: { screen_capture: '屏幕捕获' },
      fields: {},
    }
    vi.spyOn(nativeDeviceApi, 'deviceConfigStatus').mockResolvedValue(config)
    const opensshStatus: OpenSshServerStatus = {
      feature_id: 'openssh-server', capability_state: 'NotPresent', service_present: false,
      service_state: null, startup_type: null, ready: false, fields: {},
    }
    vi.spyOn(nativeDeviceApi, 'openSshServerStatus').mockResolvedValue(opensshStatus)
    vi.spyOn(nativeDeviceApi, 'resourceJobs').mockResolvedValue([])
    vi.spyOn(nativeDeviceApi, 'enableOpenSshServer').mockResolvedValue({
      job_id: 'system-feature-openssh-server', fields: {},
    })
    vi.spyOn(nativeDeviceApi, 'sshIdentityStatus').mockResolvedValue(null)
    vi.spyOn(nativeDeviceApi, 'sshTransportIdentityStatus').mockResolvedValue(null)
    vi.spyOn(nativeDeviceApi, 'sshHostStatus').mockResolvedValue({
      state: 'connected', connected: true, fields: {},
    })
    requestGateway.mockReset().mockResolvedValue({
      status: 200, body: { protocol_version: 1, ready: true, fields: {} },
    })
    readSnapshot.mockReset().mockResolvedValue(snapshot)
    startWorker.mockReset().mockResolvedValue({ state: 'ready', fields: {} })
    readHelperHealth.mockReset().mockResolvedValue({
      protocol_version: 1, request_id: '1', ok: true, state: 'ready',
      data: { health: { protocol_version: 1, request_id: '1', ok: true, result: { session_id: 1 } }, fields: {} },
      fields: {},
    })
    taskChannel.mockReset().mockImplementation(async (action: 'start' | 'status' | 'stop') => ({
      state: action === 'start' ? 'running' : 'stopped', stopped: action === 'stop', fields: {},
    }))
    mediaChannel.mockReset().mockImplementation(async (action: 'start' | 'status' | 'stop') => ({
      state: action === 'start' ? 'running' : 'stopped', stopped: action === 'stop',
      capture_active: action === 'start', fields: {},
    }))
    readLocalSnapshot.mockReset().mockResolvedValue({
      devices: [{ device_id: 'local', name: '本机', connected: true, connection_generation: 1,
        executor_generation: 1, capabilities: ['audio_stream'], error: null, interaction_active: false }],
      active_device_id: 'remote-a', interaction_generation: 4,
      capability_labels: { audio_stream: '语音音频流' }, fields: {},
    })
    selectLocalDevice.mockReset().mockResolvedValue({
      protocol_version: 1, result: 'active', device_id: 'local', interaction_generation: 5, fields: {},
    })
    vi.spyOn(nativeDeviceApi, 'requestSshGateway').mockImplementation(requestGateway)
    vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot').mockImplementation(readSnapshot)
    vi.spyOn(nativeDeviceApi, 'startWorker').mockImplementation(startWorker)
    vi.spyOn(nativeDeviceApi, 'desktopHelperHealth').mockImplementation(readHelperHealth)
    vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockImplementation(taskChannel)
    vi.spyOn(nativeDeviceApi, 'remoteMediaChannel').mockImplementation(mediaChannel)
    vi.spyOn(productApi, 'deviceSnapshot').mockImplementation(readLocalSnapshot)
    vi.spyOn(productApi, 'selectInteractionDevice').mockImplementation(selectLocalDevice)
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = undefined
    vi.restoreAllMocks()
  })

  it('将目标角色与当前实际核心角色分别显示', async () => {
    const configured = await nativeDeviceApi.deviceConfigStatus() as DeviceConfigStatus
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValueOnce({
      ...configured,
      device_runtime: {
        actual_role: 'standalone',
        core_ready: true,
        device_gateway: { enabled: false, ready: false, error: null },
        remote_client_connected: false,
        fields: {},
      },
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()

    expect(wrapper.text()).toContain('实际角色：独立运行；当前核心留在本机。')
    expect((wrapper.get('select').element as HTMLSelectElement).value).toBe('remote_client')
  })

  it('只有固定 SSH 主机连接回执才把目标标为实际远端客户端', async () => {
    const configured = await nativeDeviceApi.deviceConfigStatus() as DeviceConfigStatus
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValueOnce({
      ...configured,
      device_runtime: {
        actual_role: 'remote_client',
        core_ready: false,
        device_gateway: { enabled: false, ready: false, error: null },
        remote_client_connected: true,
        fields: {},
      },
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()

    expect(wrapper.text()).toContain('实际角色：远端客户端；固定主机 SSH 连接已认证。')
  })

  it('SSH 认证成功后刷新实际角色状态', async () => {
    const configured = await nativeDeviceApi.deviceConfigStatus() as DeviceConfigStatus
    const pending = {
      ...configured,
      device_runtime: {
        actual_role: 'none' as const, core_ready: false,
        device_gateway: { enabled: false, ready: false, error: null },
        remote_client_connected: false, fields: {},
      },
    }
    const connected = {
      ...configured,
      device_runtime: {
        actual_role: 'remote_client' as const, core_ready: false,
        device_gateway: { enabled: false, ready: false, error: null },
        remote_client_connected: true, fields: {},
      },
    }
    const disconnected = {
      ...configured,
      device_runtime: {
        actual_role: 'none' as const, core_ready: false,
        device_gateway: { enabled: false, ready: false, error: null },
        remote_client_connected: false, fields: {},
      },
    }
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockReset()
      .mockResolvedValueOnce(pending).mockResolvedValueOnce(connected).mockResolvedValue(disconnected)
    vi.mocked(nativeDeviceApi.sshHostStatus).mockReset()
      .mockResolvedValueOnce({ state: 'disconnected', connected: false, fields: {} })
      .mockResolvedValueOnce({ state: 'connected', connected: true, fields: {} })
      .mockResolvedValue({ state: 'disconnected', connected: false, fields: {} })
    vi.spyOn(nativeDeviceApi, 'connectSshHost').mockResolvedValue({
      state: 'ready', device_id: 'client-a', address: '192.0.2.10', port: 22,
      host_key_sha256: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', gateway_port: 18765, fields: {},
    })
    vi.spyOn(nativeDeviceApi, 'disconnectSshHost').mockResolvedValue({ state: 'disconnected', disconnected: true, fields: {} })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.text()).toContain('实际角色：尚未就绪')

    const connect = wrapper.findAll('button').find(button => button.text().trim() === '连接主机')
    if (!connect) throw new Error('没有出现 SSH 主机连接按钮')
    await connect.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('实际角色：远端客户端；固定主机 SSH 连接已认证。')
    const disconnect = wrapper.findAll('button').find(button => button.text().trim() === '断开')
    if (!disconnect) throw new Error('没有出现 SSH 连接断开按钮')
    await disconnect.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('实际角色：尚未就绪')
  })

  it('从受信 SSH 会话读取并显示主机设备状态', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()

    const refreshButton = wrapper.findAll('button').find(button => button.text().trim() === '刷新主机状态与设备')
    if (!refreshButton) throw new Error('没有出现主机设备刷新按钮')
    await refreshButton.trigger('click')
    await flushPromises()

    expect(requestGateway).toHaveBeenCalledWith('/v1/status', { protocol_version: 1 })
    expect(readSnapshot).toHaveBeenCalledOnce()
    expect(wrapper.get('.remote-device-list').text()).toContain('共享主机登记设备')
    expect(wrapper.get('.remote-device-list').text()).toContain('主机本机')
    expect(wrapper.get('.remote-device-list').text()).toContain('在线')
    expect(wrapper.get('.remote-device-list').text()).toContain('当前语音设备')
    expect(wrapper.get('.remote-device-list').text()).toContain('屏幕捕获')
    expect(wrapper.text()).toContain('共登记 1 台设备')
  })

  it('仅对已启动设备进程发出无副作用桌面辅助健康检查', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()

    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查桌面辅助进程')
    if (!check) throw new Error('没有出现桌面辅助健康检查按钮')
    await check.trigger('click')
    await flushPromises()

    expect(startWorker).toHaveBeenCalledOnce()
    expect(readHelperHealth).toHaveBeenCalledOnce()
    expect(startWorker.mock.invocationCallOrder[0]).toBeLessThan(readHelperHealth.mock.invocationCallOrder[0])
    expect(wrapper.get('.helper-card').text()).toContain('辅助进程就绪')
    expect(wrapper.text()).toContain('Windows 交互会话编号 1')
  })

  it('远端桌面任务通道须由用户显式启动并可安全停止', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()

    const start = wrapper.findAll('button').find(button => button.text().trim() === '启动远端桌面通道')
    if (!start) throw new Error('没有出现远端桌面通道启动按钮')
    await start.trigger('click')
    await flushPromises()
    expect(taskChannel).toHaveBeenCalledWith('start')
    expect(wrapper.get('.task-channel-card').text()).toContain('任务通道运行中')
    expect(wrapper.text()).toContain('实际设备动作仍走 DSH 审批')

    const stop = wrapper.findAll('button').find(button => button.text().trim() === '停止远端桌面通道')
    if (!stop) throw new Error('没有出现远端桌面通道停止按钮')
    await stop.trigger('click')
    await flushPromises()
    expect(taskChannel).toHaveBeenCalledWith('stop')
    expect(wrapper.get('.task-channel-card').text()).toContain('任务通道未启动')
  })

  it('远端语音媒体通道须在任务会话运行后由用户显式开启和停止', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()

    const startTasks = wrapper.findAll('button').find(button => button.text().trim() === '启动远端桌面通道')
    if (!startTasks) throw new Error('没有出现远端桌面通道启动按钮')
    await startTasks.trigger('click')
    await flushPromises()

    const startMedia = wrapper.findAll('button').find(button => button.text().trim() === '连接并启用远端语音设备')
    if (!startMedia) throw new Error('没有出现远端语音设备启动按钮')
    await startMedia.trigger('click')
    await flushPromises()
    expect(mediaChannel).toHaveBeenCalledWith('start')
    expect(wrapper.get('.media-channel-card').text()).toContain('主机已请求麦克风采集')

    const stopMedia = wrapper.findAll('button').find(button => button.text().trim() === '停止远端语音设备')
    if (!stopMedia) throw new Error('没有出现远端语音设备停止按钮')
    await stopMedia.trigger('click')
    await flushPromises()
    expect(mediaChannel).toHaveBeenCalledWith('stop')
    expect(wrapper.get('.media-channel-card').text()).toContain('语音媒体通道未启动')
  })

  it('Opus 实验档可明确启动双向连接且不回退到 PCM', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()

    const audioSelect = wrapper.findAll('select').find(select => select.find('option[value="opus_experimental"]').exists())
    if (!audioSelect) throw new Error('没有出现 Opus 实验档选择项')
    await audioSelect.setValue('opus_experimental')
    await flushPromises()
    expect(wrapper.text()).toContain('识别结果对照尚未完成')
    expect(wrapper.text()).toContain('不会自动改用 PCM')

    const start = wrapper.findAll('button').find(button => button.text().trim() === '启动远端桌面通道')
    if (!start) throw new Error('没有出现远端桌面通道启动按钮')
    await start.trigger('click')
    await flushPromises()

    const mediaStart = wrapper.findAll('button').find(button => button.text().trim() === '连接并启用远端语音设备')
    if (!mediaStart) throw new Error('没有出现远端语音设备启动按钮')
    expect(mediaStart.attributes('disabled')).toBeUndefined()
    await mediaStart.trigger('click')
    await flushPromises()
    expect(mediaChannel).toHaveBeenCalledWith('start')
    expect(wrapper.get('.media-channel-card').text()).toContain('语音媒体通道运行中')
  })

  it('共享主机仅在用户明确点击后切回本机语音设备', async () => {
    vi.spyOn(nativeDeviceApi, 'deviceConfigStatus').mockResolvedValue({
      runtime: { config_state: 'valid', device_id: 'host-a', desired_role: 'shared_host', audio_mode: 'pcm', error: null },
      device_config: { schema_version: 2, device_id: 'host-a', desired_role: 'shared_host', audio_mode: 'pcm', host: null },
      local_core_available: true, device_runtime: readySharedHostRuntime,
      fields: {},
    })
    vi.spyOn(productApi, 'devicePairingStatus').mockResolvedValue({
      window_open: false, window_expires_at_ms: null, failed_attempts_remaining: 10,
      pending_requests: [], trust: { schema_version: 1, state: 'valid', devices: [] }, fields: {},
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()

    expect(readLocalSnapshot).toHaveBeenCalledOnce()
    const switchButton = wrapper.findAll('button').find(button => button.text().trim() === '明确切回主机本机语音')
    if (!switchButton) throw new Error('没有出现明确切回本机语音按钮')
    await switchButton.trigger('click')
    await flushPromises()

    expect(selectLocalDevice).toHaveBeenCalledWith('local', 4)
    expect(readLocalSnapshot).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('已按本机明确操作切回主机语音设备')
  })

  it('系统 SSH 功能状态只在已保存的共享主机角色下查询', async () => {
    const standalone: DeviceConfigStatus = {
      runtime: { config_state: 'valid', device_id: 'host-a', desired_role: 'standalone', audio_mode: 'pcm', error: null },
      device_config: { schema_version: 2, device_id: 'host-a', desired_role: 'standalone', audio_mode: 'pcm', host: null },
      fields: {},
    }
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValue(standalone)
    wrapper = mount(DeviceSettings)
    await flushPromises()

    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    await wrapper.get('fieldset select').setValue('shared_host')
    expect(wrapper.get('.system-feature-card').text()).toContain('先保存“共享主机”角色')
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
  })

  it('全新轻量工作区默认显示远端客户端且不自动保存角色或请求配对', async () => {
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValue({
      local_core_available: false,
      runtime: { config_state: 'missing', device_id: null, desired_role: null, audio_mode: null, error: null },
      device_config: null,
      device_runtime: { ...readySharedHostRuntime, actual_role: 'none', core_ready: false,
        device_gateway: { enabled: false, ready: false, error: null } },
      fields: {},
    })
    const save = vi.spyOn(nativeDeviceApi, 'saveDeviceConfig')
    const pairingStatus = vi.spyOn(productApi, 'devicePairingStatus')
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.get('fieldset select').element).toHaveProperty('value', 'remote_client')
    expect(wrapper.text()).toContain('待配置主机')
    expect(wrapper.text()).toContain('本机核心未启动')
    expect(wrapper.text()).not.toContain('当前核心留在本机')
    expect(save).not.toHaveBeenCalled()
    expect(pairingStatus).not.toHaveBeenCalled()
  })

  it('轻量客户端保留误保存的共享主机配置并阻止配对与系统安装请求', async () => {
    const configured = await nativeDeviceApi.deviceConfigStatus() as DeviceConfigStatus
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValue({
      ...configured,
      local_core_available: false,
      runtime: { ...configured.runtime, desired_role: 'shared_host' },
      device_config: { ...configured.device_config!, desired_role: 'shared_host', host: null },
      device_runtime: { ...readySharedHostRuntime, actual_role: 'none', core_ready: false,
        device_gateway: { enabled: false, ready: false, error: null } },
    })
    const pairingStatus = vi.spyOn(productApi, 'devicePairingStatus')
    const save = vi.spyOn(nativeDeviceApi, 'saveDeviceConfig')
    wrapper = mount(DeviceSettings)
    await flushPromises()

    expect(wrapper.get('fieldset select').element).toHaveProperty('value', 'shared_host')
    expect(wrapper.get('option[value="shared_host"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('option[value="standalone"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('option[value="remote_client"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('轻量客户端请选择“远端客户端”')
    expect(wrapper.get('.pairing-card').text()).toContain('配对管理暂不可用')
    expect(wrapper.get('.system-feature-card').text()).toContain('轻量客户端无需安装 SSH 入站服务')
    expect(pairingStatus).not.toHaveBeenCalled()
    expect(readLocalSnapshot).not.toHaveBeenCalled()
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.resourceJobs).not.toHaveBeenCalled()
    expect(wrapper.findAll('button').some(button => button.text().includes('开启配对窗口'))).toBe(false)
    expect(save).not.toHaveBeenCalled()
  })

  it('完整工作区核心尚未就绪时不查询配对但仍可只读检查系统服务', async () => {
    const configured = await nativeDeviceApi.deviceConfigStatus() as DeviceConfigStatus
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValue({
      ...configured, local_core_available: true,
      runtime: { ...configured.runtime, desired_role: 'shared_host' },
      device_config: { ...configured.device_config!, desired_role: 'shared_host', host: null },
      device_runtime: { ...readySharedHostRuntime, actual_role: 'none', core_ready: false },
    })
    const pairingStatus = vi.spyOn(productApi, 'devicePairingStatus')
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.get('.pairing-card').text()).toContain('配对管理暂不可用')
    expect(pairingStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.openSshServerStatus).toHaveBeenCalledOnce()
  })

  it('安装 OpenSSH Server 前披露 TCP 22 防火墙副作用并要求额外确认', async () => {
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValue({
      runtime: { config_state: 'valid', device_id: 'host-a', desired_role: 'shared_host', audio_mode: 'pcm', error: null },
      device_config: { schema_version: 2, device_id: 'host-a', desired_role: 'shared_host', audio_mode: 'pcm', host: null },
      local_core_available: true, device_runtime: readySharedHostRuntime,
      fields: {},
    })
    vi.spyOn(productApi, 'devicePairingStatus').mockResolvedValue({
      window_open: false, window_expires_at_ms: null, failed_attempts_remaining: 10,
      pending_requests: [], trust: { schema_version: 1, state: 'valid', devices: [] }, fields: {},
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    wrapper = mount(DeviceSettings)
    await flushPromises()

    expect(nativeDeviceApi.openSshServerStatus).toHaveBeenCalledOnce()
    expect(wrapper.get('.system-feature-card').text()).toContain('OpenSSH-Server-In-TCP')
    const enable = wrapper.findAll('button').find(button => button.text().trim() === '安装并启动 OpenSSH Server')
    if (!enable) throw new Error('没有出现 OpenSSH Server 安装按钮')
    await enable.trigger('click')
    expect(confirm).toHaveBeenCalledOnce()
    expect(confirm.mock.calls[0][0]).toContain('TCP 端口 22')
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
  })

  it('仅在用户接受系统副作用后登记不可取消的 OpenSSH 受管任务', async () => {
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValue({
      runtime: { config_state: 'valid', device_id: 'host-a', desired_role: 'shared_host', audio_mode: 'pcm', error: null },
      device_config: { schema_version: 2, device_id: 'host-a', desired_role: 'shared_host', audio_mode: 'pcm', host: null },
      local_core_available: true, device_runtime: readySharedHostRuntime,
      fields: {},
    })
    vi.spyOn(productApi, 'devicePairingStatus').mockResolvedValue({
      window_open: false, window_expires_at_ms: null, failed_attempts_remaining: 10,
      pending_requests: [], trust: { schema_version: 1, state: 'valid', devices: [] }, fields: {},
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(nativeDeviceApi.resourceJobs)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([{
        job_id: 'system-feature-openssh-server', resource_id: 'openssh-server', state: 'running',
        phase: '等待 Windows 管理员授权', error: null, can_cancel: false, fields: {},
      }])
    wrapper = mount(DeviceSettings)
    await flushPromises()

    const enable = wrapper.findAll('button').find(button => button.text().trim() === '安装并启动 OpenSSH Server')
    if (!enable) throw new Error('没有出现 OpenSSH Server 安装按钮')
    await enable.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.enableOpenSshServer).toHaveBeenCalledOnce()
    expect(wrapper.get('.system-feature-card').text()).toContain('等待 Windows 管理员授权')
  })

  it('共享主机通过本地会话管理限时配对、批准及撤销', async () => {
    const hostConfig: DeviceConfigStatus = {
      runtime: { config_state: 'valid', device_id: 'host-a', desired_role: 'shared_host', audio_mode: 'pcm', error: null },
      device_config: { schema_version: 2, device_id: 'host-a', desired_role: 'shared_host', audio_mode: 'pcm', host: null },
      local_core_available: true, device_runtime: readySharedHostRuntime,
      fields: {},
    }
    vi.mocked(nativeDeviceApi.deviceConfigStatus).mockResolvedValue(hostConfig)

    const pendingStatus: DevicePairingStatus = {
      window_open: true,
      window_expires_at_ms: 1_900_000_000_000,
      failed_attempts_remaining: 10,
      pending_requests: [{ request_id: 'a'.repeat(32), device_id: 'client-b', fingerprint: 'SHA256:device-fingerprint', created_at_ms: 1_800_000_000_000, expires_at_ms: 1_900_000_000_000 }],
      trust: { schema_version: 1, state: 'valid', devices: [] },
      fields: {},
    }
    const trustedStatus: DevicePairingStatus = {
      ...pendingStatus,
      pending_requests: [],
      trust: { schema_version: 1, state: 'valid', devices: [{ device_id: 'client-b', fingerprint: 'SHA256:device-fingerprint', state: 'trusted', trusted_at_ms: 1_800_000_000_000, revoked_at_ms: null }] },
    }
    const revokedStatus: DevicePairingStatus = {
      ...trustedStatus,
      trust: { schema_version: 1, state: 'valid', devices: [{ ...trustedStatus.trust.devices[0], state: 'revoked', revoked_at_ms: 1_800_000_001_000 }] },
    }
    vi.spyOn(productApi, 'devicePairingStatus').mockResolvedValue(pendingStatus)
    const openPairing = vi.spyOn(productApi, 'openDevicePairing').mockResolvedValue({ ...pendingStatus, pairing_code: '38472910' })
    const approvePairing = vi.spyOn(productApi, 'approveDevicePairing').mockResolvedValue(trustedStatus)
    const revokeDevice = vi.spyOn(productApi, 'revokeDeviceTrust').mockResolvedValue(revokedStatus)
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(productApi.devicePairingStatus).toHaveBeenCalledOnce()

    await wrapper.get('.pairing-card').findAll('button').find(button => button.text().includes('开启配对窗口'))?.trigger('click')
    await flushPromises()
    expect(openPairing).toHaveBeenCalledWith(120)
    expect(wrapper.get('.pairing-code-box').text()).toContain('38472910')

    const approveButton = wrapper.get('.pairing-card').findAll('button').find(button => button.text().trim() === '明确批准')
    if (!approveButton) throw new Error('没有出现明确批准按钮')
    await approveButton.trigger('click')
    await flushPromises()
    expect(approvePairing).toHaveBeenCalledWith('a'.repeat(32))
    expect(wrapper.get('.pairing-card').text()).toContain('client-b')
    expect(wrapper.get('.pairing-card').text()).toContain('可信')

    const revokeButton = wrapper.get('.pairing-card').findAll('button').find(button => button.text().trim() === '撤销信任')
    if (!revokeButton) throw new Error('没有出现撤销信任按钮')
    await revokeButton.trigger('click')
    await flushPromises()
    expect(revokeDevice).toHaveBeenCalledWith('client-b')
    expect(wrapper.get('.pairing-card').text()).toContain('已撤销')
  })

  it('远端客户端只通过本机私钥签署已固定 SSH 会话的配对申请', async () => {
    const identity: NativeSshIdentity = {
      credential_ref: `VoiceTerminal/device/${'a'.repeat(64)}`,
      public_key: 'ssh-ed25519 AAAA client-a',
      fingerprint: 'SHA256:client-fingerprint',
    }
    vi.spyOn(nativeDeviceApi, 'sshIdentityStatus').mockResolvedValue(identity)
    const submit = vi.spyOn(nativeDeviceApi, 'submitSshPairingRequest').mockResolvedValue({
      status: 202,
      body: { protocol_version: 1, request_id: 'b'.repeat(32), device_id: 'client-a', fingerprint: identity.fingerprint },
      fields: {},
    })

    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.get('input[placeholder="8 位数字"]').setValue('38472910')
    const submitButton = wrapper.findAll('button').find(button => button.text().trim() === '提交签名配对申请')
    if (!submitButton) throw new Error('没有出现签名配对申请按钮')
    await submitButton.trigger('click')
    await flushPromises()

    expect(submit).toHaveBeenCalledWith('38472910')
    expect(wrapper.text()).toContain('须由主机本机明确批准')
    expect(wrapper.text()).toContain('等待主机批准')
    expect(wrapper.find('input[placeholder="8 位数字"]').element).toHaveProperty('value', '')
  })

  it('将 SSH 登录私钥交由原生层导入，并与产品设备签名身份分开保存', async () => {
    const identity: NativeSshTransportIdentity = {
      credential_ref: `VoiceTerminal/ssh-transport/${'b'.repeat(64)}`,
      public_key: 'ssh-ed25519 AAAA transport-login',
      fingerprint: 'SHA256:transport-fingerprint',
    }
    const configured: DeviceConfigStatus = {
      runtime: { config_state: 'valid', device_id: 'client-a', desired_role: 'remote_client', audio_mode: 'pcm', error: null },
      device_config: {
        schema_version: 2, device_id: 'client-a', desired_role: 'remote_client', audio_mode: 'pcm',
        host: {
          address: '192.0.2.10', port: 22, username: 'voice',
          host_key_sha256: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
          credential_ref: `VoiceTerminal/device/${'a'.repeat(64)}`,
          transport_credential_ref: identity.credential_ref,
        },
      },
      fields: {},
    }
    vi.spyOn(nativeDeviceApi, 'importSshTransportIdentity').mockResolvedValue(identity)
    vi.spyOn(nativeDeviceApi, 'sshTransportIdentityStatus').mockResolvedValue(identity)
    vi.spyOn(nativeDeviceApi, 'saveDeviceConfig').mockResolvedValue(configured)
    vi.spyOn(nativeDeviceApi, 'deviceConfigStatus')
      .mockResolvedValueOnce({ ...configured, device_config: { ...configured.device_config!, host: {
        ...configured.device_config!.host!, transport_credential_ref: undefined,
      } } })
      .mockResolvedValue(configured)

    wrapper = mount(DeviceSettings)
    await flushPromises()
    const pathInput = wrapper.get('input[placeholder*="id_ed25519"]')
    await pathInput.setValue('C:\\Users\\tester\\.ssh\\id_ed25519')
    const importButton = wrapper.findAll('button').find(button => button.text().trim() === '在本机导入登录密钥')
    if (!importButton) throw new Error('没有出现 SSH 登录密钥导入按钮')
    await importButton.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.importSshTransportIdentity).toHaveBeenCalledWith('C:\\Users\\tester\\.ssh\\id_ed25519')
    expect(wrapper.text()).toContain('transport-fingerprint')
    expect(wrapper.text()).toContain('产品配对批准不会修改主机登录账户')
    const saveButton = wrapper.findAll('button').find(button => button.text().trim() === '保存设备配置')
    if (!saveButton) throw new Error('没有出现设备配置保存按钮')
    await saveButton.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      host: expect.objectContaining({
        credential_ref: expect.stringMatching(/^VoiceTerminal\/device\//),
        transport_credential_ref: identity.credential_ref,
      }),
    }), false)
  })
})

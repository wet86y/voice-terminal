// @vitest-environment jsdom
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nativeDeviceApi, type DeviceConfigStatus, type DiscoveredDevice, type DeviceConfig, type ManagedResourceJob, type OpenSshServerStatus, type RemoteManagementOperation, type RemoteManagementResponse, type RemoteSshSystemFeatureSnapshot, type RemoteSshSystemFeatureJob } from '../product/native-device-api'
import DeviceSettings from './DeviceSettings.vue'

const foundHost: DiscoveredDevice = {
  host_id: 'server-a', display_name: '服务端 A', hostname: 'voice-terminal-server-a.local.',
  addresses: ['192.168.1.20'], pairing_port: 18766, ssh_port: 22, protocol_version: 1,
  compatible: true, state: 'ready', capabilities: ['remote_management', 'device_tasks', 'audio'], fields: {},
}

function makeConfig(mode: DeviceConfig['mode'] = 'local', broadcast = false): DeviceConfigStatus {
  const desiredRole = mode === 'remote' ? 'remote_client' : broadcast ? 'shared_host' : 'standalone'
  return {
    local_core_available: true,
    runtime: { config_state: 'valid', device_id: 'device-a', mode, broadcast_enabled: broadcast, desired_role: desiredRole, audio_mode: 'pcm', error: null },
    device_config: {
      schema_version: 3, device_id: 'device-a', mode, broadcast_enabled: broadcast,
      client_online_tts_local: true, audio_compression: false, reconnect_suspended_reason: null,
      desired_role: desiredRole, audio_mode: 'pcm', paired_host: null,
    },
    device_runtime: {
      mode, broadcast_enabled: broadcast, expected_mode: mode, actual_mode: mode,
      switch_phase: 'ready', broadcast_requested: broadcast, broadcast_actual: broadcast,
      actual_role: desiredRole, core_ready: mode === 'local',
      device_gateway: { enabled: broadcast, ready: broadcast, error: null },
      remote_client_connected: mode === 'remote', fields: {},
    },
    fields: {},
  }
}

function makeSshStatus(ready = true): OpenSshServerStatus {
  return {
    feature_id: 'openssh-server', capability_state: ready ? 'Installed' : 'NotPresent',
    service_present: ready, service_state: ready ? 'Running' : null, startup_type: ready ? 'Automatic' : null,
    source_path: ready ? 'C:\\Windows\\System32\\OpenSSH\\sshd.exe' : null,
    source_version: ready ? '9.5.0.0' : null, signature_status: ready ? 'Valid' : null,
    signature_subject: ready ? 'Microsoft Corporation' : null, official_binary: ready,
    service_path_official: ready, effective_port: ready ? 22 : null, listening_ports: ready ? [22] : [],
    configuration_valid: ready, authorized_keys_path: null, authorized_keys_exists: false,
    account_name: 'MACHINE\\tester', account_ssh_username: 'tester', account_sid: 'S-1-5-21-42',
    account_is_admin: false, account_is_entra: false, project_access_enabled: ready, project_access_valid: ready,
    project_network_scope: ready ? ['192.168.1.0/24'] : [], pairing_access_enabled: ready,
    pairing_access_valid: ready, pairing_network_scope: ready ? ['192.168.1.0/24'] : [], existing_ssh_inbound_rules: [],
    local_lan_addresses: ['192.168.1.10'],
    local_lan_scopes: ['192.168.1.0/24'], ready, fields: {},
  }
}

function makeRemoteSshSnapshot(ready: boolean, job: RemoteSshSystemFeatureJob | null = null): RemoteSshSystemFeatureSnapshot {
  return { status: makeSshStatus(ready), job, fields: { status: '服务端 SSH 系统功能快照', job: '固定系统功能任务' } }
}

function makeRemoteSshJob(
  state: RemoteSshSystemFeatureJob['state'], phase: string, error: string | null = null,
): RemoteSshSystemFeatureJob {
  return { job_id: 'system-feature-openssh-server', resource_id: 'openssh-server', state, phase,
    error, can_cancel: false, fields: { state: '任务状态', phase: '任务阶段' } }
}

function makeLocalSshJob(state: string): ManagedResourceJob {
  return { job_id: 'system-feature-openssh-server', resource_id: 'openssh-server', state, phase: state,
    error: null, can_cancel: false, fields: { state: '任务状态', phase: '任务阶段' } }
}

function makeManagementResponse(
  operation: RemoteManagementOperation, body: Record<string, unknown>, status = 200,
): RemoteManagementResponse {
  return { request_id: 'b'.repeat(32), target_host_id: foundHost.host_id, session_kind: 'management',
    session_generation: 4, operation, status, body, fields: {} }
}

describe('DeviceSettings（设备设置）', () => {
  let wrapper: VueWrapper | undefined
  let currentStatus: DeviceConfigStatus
  let sshReady: boolean
  let sshConnected: boolean
  let taskRunning: boolean
  let mediaRunning: boolean
  let interactionGeneration: number
  const clipboardWrite = vi.fn()

  function prepareRemoteHost() {
    currentStatus = makeConfig('remote', false)
    currentStatus.device_config!.paired_host = {
      host_id: foundHost.host_id, display_name: foundHost.display_name, addresses: foundHost.addresses,
      address: foundHost.addresses[0], port: foundHost.ssh_port, username: 'tester',
      host_key_sha256: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      credential_ref: 'VoiceTerminal/device/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    }
    sshConnected = true
  }

  beforeEach(() => {
    currentStatus = makeConfig('local', false)
    sshReady = true
    sshConnected = false
    taskRunning = false
    mediaRunning = false
    interactionGeneration = 0
    clipboardWrite.mockReset().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: clipboardWrite } })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.spyOn(nativeDeviceApi, 'deviceConfigStatus').mockImplementation(async () => currentStatus)
    vi.spyOn(nativeDeviceApi, 'saveDeviceConfig').mockImplementation(async (config: DeviceConfig) => {
      currentStatus = makeConfig(config.mode, config.broadcast_enabled)
      currentStatus.device_config = config
      return currentStatus
    })
    vi.spyOn(nativeDeviceApi, 'searchDevices').mockResolvedValue({ protocol_version: 1, devices: [foundHost], fields: {} })
    vi.spyOn(nativeDeviceApi, 'pairWithDiscoveredDevice').mockImplementation(async (hostId, pairingCode) => {
      sshConnected = true
      const saved = currentStatus.device_config!
      const paired = {
        host_id: hostId, display_name: foundHost.display_name, addresses: foundHost.addresses,
        address: foundHost.addresses[0], port: foundHost.ssh_port, username: 'tester',
        host_key_sha256: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        credential_ref: 'VoiceTerminal/device/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      }
      currentStatus = makeConfig('remote', false)
      currentStatus.device_config = { ...saved, mode: 'remote', desired_role: 'remote_client', paired_host: paired }
      return {
        state: 'paired', device_id: saved.device_id, host_id: hostId, display_name: foundHost.display_name,
        ssh_port: 22, address: foundHost.addresses[0], connected: true, connection_error: null, fields: {},
      }
    })
    vi.spyOn(nativeDeviceApi, 'openPairingWindow').mockResolvedValue({ pairing_code: '000042', expires_in_seconds: 120, fields: {} })
    vi.spyOn(nativeDeviceApi, 'closePairingWindow').mockResolvedValue(undefined)
    vi.spyOn(nativeDeviceApi, 'pairingWindowStatus').mockResolvedValue({ open: true, expires_in_ms: 120000, fields: {} })
    vi.spyOn(nativeDeviceApi, 'openSshServerStatus').mockImplementation(async () => makeSshStatus(sshReady))
    vi.spyOn(nativeDeviceApi, 'enableOpenSshServer').mockResolvedValue({ job_id: 'system-feature-openssh-server', fields: {} })
    vi.spyOn(nativeDeviceApi, 'resourceJobs').mockResolvedValue([])
    vi.spyOn(nativeDeviceApi, 'sshHostStatus').mockImplementation(async () => ({ state: sshConnected ? 'connected' : 'disconnected', connected: sshConnected, fields: {} }))
    vi.spyOn(nativeDeviceApi, 'connectSshHost').mockImplementation(async () => {
      sshConnected = true
      return { state: 'ready', device_id: 'device-a', address: '192.168.1.20', port: 22,
        host_key_sha256: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', gateway_port: 18765, fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'disconnectSshHost').mockImplementation(async () => {
      sshConnected = false
      return { state: 'disconnected', disconnected: true, fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockImplementation(async action => {
      if (action === 'start') taskRunning = true
      if (action === 'stop') taskRunning = false
      return taskRunning
        ? { state: 'running', device_id: 'device-a', connection_generation: 1, executor_generation: 1, stopped: action === 'stop', fields: {} }
        : { state: 'stopped', stopped: action === 'stop', fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'remoteMediaChannel').mockImplementation(async action => {
      if (action === 'start') mediaRunning = true
      if (action === 'stop') mediaRunning = false
      if (action === 'start') interactionGeneration += 1
      return mediaRunning
        ? { state: 'running', device_id: 'device-a', stream_id: 'stream-a', connection_generation: 1,
          executor_generation: 1, interaction_generation: interactionGeneration, stopped: false, capture_active: true, fields: {} }
        : { state: 'stopped', interaction_generation: interactionGeneration, stopped: action === 'stop', capture_active: false, fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'remoteDeviceSnapshot').mockImplementation(async () => ({
      protocol_version: 1, ready: true,
      active_device_id: mediaRunning ? 'device-a' : 'local',
      interaction_generation: interactionGeneration,
      devices: [{
        device_id: 'device-a', name: '客户端 A', connected: sshConnected,
        connection_generation: 1, executor_generation: 1,
        capabilities: ['audio_stream'], error: null, interaction_active: mediaRunning,
      }],
      capability_labels: { audio_stream: '语音流' },
      fields: { active_device_id: '当前语音占用设备编号', interaction_generation: '当前语音占用代次',
        devices: '服务端已登记设备' },
    }))
    vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue({
      request_id: 'a'.repeat(32), target_host_id: foundHost.host_id, session_generation: 4,
      session_kind: 'management', operation: 'management.status', status: 200, body: { core_state: 'ready' }, fields: {},
    })
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = undefined
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('零组件远程客户端可直接搜索，并在搜索前保存远程模式', async () => {
    currentStatus = { ...makeConfig('remote', false), local_core_available: false,
      runtime: { ...makeConfig('remote', false).runtime, config_state: 'missing', device_id: null }, device_config: null }
    wrapper = mount(DeviceSettings)
    await flushPromises()

    const search = wrapper.findAll('button').find(button => button.text().trim() === '搜索设备')
    expect(search).toBeDefined()
    await search!.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'remote', desired_role: 'remote_client', paired_host: null,
    }), false)
    expect(nativeDeviceApi.searchDevices).toHaveBeenCalledWith(3000)
    expect(wrapper.text()).toContain('服务端 A')
  })

  it('模式选远程后自动保存；配对码保持前导零并自动建立连接通道', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.get('select').setValue('remote')
    await flushPromises()

    const search = wrapper.findAll('button').find(button => button.text().trim() === '搜索设备')
    await search!.trigger('click')
    await flushPromises()
    const codeInput = wrapper.find('input[placeholder="输入服务端显示的配对码"]')
    await codeInput.setValue('000042')
    const connect = wrapper.findAll('button').find(button => button.text().trim() === '连接')
    await connect!.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.pairWithDiscoveredDevice).toHaveBeenCalledWith('server-a', '000042')
    expect(nativeDeviceApi.remoteTaskChannel).toHaveBeenCalledWith('start')
    expect(nativeDeviceApi.remoteMediaChannel).toHaveBeenCalledWith('start')
    expect(wrapper.text()).toContain('服务端 A')
    expect(wrapper.find('input[placeholder*="192.168"]').exists()).toBe(false)
    expect(wrapper.find('input[placeholder*="SHA256"]').exists()).toBe(false)
  })

  it('远程连接开关即时保存并按需重建通道，不暴露手工通道控制', async () => {
    currentStatus = makeConfig('remote', false)
    currentStatus.device_config!.paired_host = {
      host_id: 'server-a', display_name: '服务端 A', addresses: foundHost.addresses,
      address: foundHost.addresses[0], port: foundHost.ssh_port, username: 'tester',
      host_key_sha256: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      credential_ref: 'VoiceTerminal/device/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    }
    sshConnected = true
    taskRunning = true
    mediaRunning = true
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockClear()
    vi.mocked(nativeDeviceApi.remoteTaskChannel).mockClear()
    vi.mocked(nativeDeviceApi.remoteMediaChannel).mockClear()

    const audioCompressionToggle = wrapper.get('input[aria-label="压缩远程音频"]')
    await audioCompressionToggle.setValue(true)
    await flushPromises()

    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'remote', audio_compression: true, audio_mode: 'opus_experimental',
    }))
    expect(nativeDeviceApi.remoteMediaChannel).toHaveBeenNthCalledWith(1, 'stop')
    expect(nativeDeviceApi.remoteMediaChannel).toHaveBeenNthCalledWith(2, 'start')
    expect(nativeDeviceApi.remoteTaskChannel).not.toHaveBeenCalledWith('stop')

    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockClear()
    vi.mocked(nativeDeviceApi.remoteTaskChannel).mockClear()
    vi.mocked(nativeDeviceApi.remoteMediaChannel).mockClear()
    const onlineTtsToggle = wrapper.get('input[aria-label="在线语音在本机合成并播放"]')
    await onlineTtsToggle.setValue(false)
    await flushPromises()

    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'remote', client_online_tts_local: false, audio_compression: true, audio_mode: 'opus_experimental',
    }))
    expect(vi.mocked(nativeDeviceApi.remoteMediaChannel).mock.calls.map(([action]) => action)).toEqual(['stop', 'start'])
    expect(vi.mocked(nativeDeviceApi.remoteTaskChannel).mock.calls.map(([action]) => action)).toEqual(['stop', 'start'])
    expect(wrapper.findAll('button').map(button => button.text())).not.toContain('启动远端桌面通道')
    expect(wrapper.findAll('button').map(button => button.text())).not.toContain('停止远端语音设备')
  })

  it('本机广播开关保存本地运行意图，获取配对码后自动复制', async () => {
    currentStatus = makeConfig('local', false)
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.openSshServerStatus).mockClear()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockClear()
    const checkbox = wrapper.get('input[aria-label="广播并接受配对"]')
    await checkbox.setValue(true)
    await flushPromises()

    const getCode = wrapper.findAll('button').find(button => button.text().trim() === '获取配对码')
    expect(getCode).toBeDefined()
    await getCode!.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'local', broadcast_enabled: true, desired_role: 'shared_host',
    }), false)
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(nativeDeviceApi.openSshServerStatus).toHaveBeenCalled()
    expect(vi.mocked(nativeDeviceApi.openSshServerStatus).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(nativeDeviceApi.saveDeviceConfig).mock.invocationCallOrder[0]!)
    expect(nativeDeviceApi.openPairingWindow).toHaveBeenCalledOnce()
    expect(clipboardWrite).toHaveBeenCalledWith('000042')
    expect(wrapper.get('.pairing-code-box output').text()).toBe('000042')
  })

  it('广播开启前 SSH 未就绪时获准后等待系统任务完成再保存', async () => {
    vi.useFakeTimers()
    sshReady = false
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockClear()
    vi.mocked(nativeDeviceApi.openSshServerStatus).mockReset()
      .mockResolvedValueOnce(makeSshStatus(false))
      .mockResolvedValueOnce(makeSshStatus(true))
      .mockResolvedValueOnce(makeSshStatus(true))
      .mockResolvedValueOnce(makeSshStatus(true))
    vi.mocked(nativeDeviceApi.resourceJobs).mockReset()
      .mockResolvedValueOnce([makeLocalSshJob('running')])
      .mockResolvedValueOnce([makeLocalSshJob('complete')])
      .mockResolvedValueOnce([makeLocalSshJob('complete')])

    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()

    expect(nativeDeviceApi.enableOpenSshServer).toHaveBeenCalledOnce()
    expect(nativeDeviceApi.enableOpenSshServer).toHaveBeenCalledWith(true)
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(wrapper.get('input[aria-label="广播并接受配对"]').element).toHaveProperty('checked', true)
    await vi.advanceTimersByTimeAsync(1200)
    await flushPromises()

    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'local', broadcast_enabled: true, desired_role: 'shared_host',
    }), false)
    expect(wrapper.text()).toContain('版本：9.5.0.0')
  })

  it('SSH 准备超过五分钟后继续显示任务，晚到完成时清除等待告警并恢复检查按钮', async () => {
    vi.useFakeTimers()
    sshReady = false
    let jobState = 'running'
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.resourceJobs).mockImplementation(async () => [makeLocalSshJob(jobState)])

    await wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    await flushPromises()

    expect(wrapper.text()).toContain('Windows SSH 准备任务仍在进行')
    expect(wrapper.text()).toContain('任务状态：进行中')
    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!
    expect(check.element.disabled).toBe(true)

    jobState = 'complete'
    sshReady = true
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()

    expect(wrapper.text()).toContain('微软 OpenSSH Server 已就绪')
    expect(wrapper.text()).not.toContain('准备任务仍在进行')
    expect(wrapper.text()).not.toContain('等待 Windows SSH 服务准备超时')
    expect(check.element.disabled).toBe(false)
  })

  it('SSH 准备超时后晚到失败时显示真实原因并开放重试', async () => {
    vi.useFakeTimers()
    sshReady = false
    let jobState = 'running'
    let jobError: string | null = null
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.resourceJobs).mockImplementation(async () => [
      { ...makeLocalSshJob(jobState), error: jobError },
    ])

    await wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    await flushPromises()
    expect(wrapper.text()).toContain('Windows SSH 准备任务仍在进行')

    jobState = 'failed'
    jobError = 'Windows 管理员授权后安装失败。'
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('Windows 管理员授权后安装失败')
    expect(wrapper.text()).toContain('任务状态：失败')
    expect(wrapper.text()).not.toContain('准备任务仍在进行')
    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!
    expect(check.element.disabled).toBe(false)
  })

  it('广播开启前 SSH 未就绪且用户取消时还原开关并且不保存', async () => {
    sshReady = false
    vi.mocked(window.confirm).mockReturnValue(false)
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockClear()
    vi.mocked(nativeDeviceApi.enableOpenSshServer).mockClear()
    vi.mocked(nativeDeviceApi.resourceJobs).mockClear()

    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()

    expect(window.confirm).toHaveBeenCalledOnce()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(nativeDeviceApi.resourceJobs).not.toHaveBeenCalled()
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(wrapper.get('input[aria-label="广播并接受配对"]').element).toHaveProperty('checked', false)
    expect(wrapper.get('[role="alert"]').text()).toContain('你取消了 SSH 服务准备')
  })

  it('SSH 已就绪时按钮只检查状态，不重新安装或重启', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')
    await check!.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.openSshServerStatus).toHaveBeenCalled()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('微软 OpenSSH Server 已就绪')
  })

  it('SSH 未就绪且用户取消时不启动安装任务，并允许再次检查', async () => {
    sshReady = false
    vi.mocked(window.confirm).mockReturnValue(false)
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')
    await check!.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('你取消了 SSH 服务准备')
    expect(check!.element.disabled).toBe(false)
  })

  it('其他入站规则默认收起，只显示有限条数和短摘要', async () => {
    sshReady = false
    const snapshot = makeSshStatus(false)
    snapshot.existing_ssh_inbound_rules = Array.from({ length: 8 }, (_, index) =>
      `possible-ssh-rule-${index} ${'RemoteAddress=Any '.repeat(30)}`)
    vi.mocked(nativeDeviceApi.openSshServerStatus).mockResolvedValue(snapshot)
    wrapper = mount(DeviceSettings)
    await flushPromises()

    const details = wrapper.get('details.ssh-rule-details')
    expect((details.element as HTMLDetailsElement).open).toBe(false)
    expect(details.get('summary').text()).toContain('检测到 8 条')
    expect(details.findAll('li')).toHaveLength(5)
    expect(details.text()).toContain('还有 3 条规则未展开显示')
    expect(details.findAll('li').every(row => Array.from(row.text()).length <= 180)).toBe(true)
  })

  it('远程服务端 SSH 就绪时只读显示服务端状态，不请求安装或检查客户端', async () => {
    prepareRemoteHost()
    const snapshot = makeRemoteSshSnapshot(true)
    const request = vi.mocked(nativeDeviceApi.remoteManagementRequest).mockImplementation(async operation => {
      if (operation === 'management.status') return makeManagementResponse(operation, { core_state: 'ready' })
      if (operation === 'system.ssh.status') return makeManagementResponse(operation, snapshot as unknown as Record<string, unknown>)
      throw new Error(`Unexpected remote operation: ${operation}`)
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    request.mockClear()

    await wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()

    expect(request).toHaveBeenCalledWith('system.ssh.status', {})
    expect(request).not.toHaveBeenCalledWith('system.ssh.enable', expect.anything())
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('服务端微软 OpenSSH Server 已就绪')
    expect(wrapper.text()).toContain('9.5.0.0')
    expect(wrapper.text()).not.toContain('MACHINE\\tester')
    expect(wrapper.text()).not.toContain('authorized_keys')
    expect(wrapper.find('input[placeholder*="192.168"]').exists()).toBe(false)
    expect(wrapper.find('input[placeholder*="SHA256"]').exists()).toBe(false)
  })

  it('远程服务端未就绪时必须明确确认，取消不发送启动操作', async () => {
    prepareRemoteHost()
    const snapshot = makeRemoteSshSnapshot(false)
    const request = vi.mocked(nativeDeviceApi.remoteManagementRequest).mockImplementation(async operation => {
      if (operation === 'management.status') return makeManagementResponse(operation, { core_state: 'ready' })
      if (operation === 'system.ssh.status') return makeManagementResponse(operation, snapshot as unknown as Record<string, unknown>)
      throw new Error(`Unexpected remote operation: ${operation}`)
    })
    vi.mocked(window.confirm).mockReturnValue(false)
    wrapper = mount(DeviceSettings)
    await flushPromises()

    await wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()
    const prepare = wrapper.findAll('button').find(button => button.text().trim() === '请求服务端安装或启动')
    expect(prepare).toBeDefined()
    await prepare!.trigger('click')
    await flushPromises()

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('服务端'))
    expect(request.mock.calls.filter(([operation]) => operation === 'system.ssh.enable')).toHaveLength(0)
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('服务端微软 OpenSSH Server 尚未就绪')
  })

  it('服务端 UAC 等待与失败显示服务端任务状态，失败后可重试并只按 ready 快照标记就绪', async () => {
    vi.useFakeTimers()
    prepareRemoteHost()
    const notReady = makeRemoteSshSnapshot(false)
    const awaitingUac = makeRemoteSshSnapshot(false, makeRemoteSshJob('running', '等待 Windows 管理员授权'))
    const failed = makeRemoteSshSnapshot(false, makeRemoteSshJob('failed', '管理员取消安装', 'Windows 管理员授权已取消。'))
    const retrying = makeRemoteSshSnapshot(false, makeRemoteSshJob('running', '正在复核服务端状态'))
    const ready = makeRemoteSshSnapshot(true, makeRemoteSshJob('complete', 'OpenSSH Server 已就绪'))
    let statusReads = 0
    let enableCalls = 0
    const request = vi.mocked(nativeDeviceApi.remoteManagementRequest).mockImplementation(async operation => {
      if (operation === 'management.status') return makeManagementResponse(operation, { core_state: 'ready' })
      if (operation === 'system.ssh.status') {
        statusReads += 1
        const snapshot = statusReads === 1 ? notReady : statusReads === 2 ? failed : ready
        return makeManagementResponse(operation, snapshot as unknown as Record<string, unknown>)
      }
      if (operation === 'system.ssh.enable') {
        enableCalls += 1
        const snapshot = enableCalls === 1 ? awaitingUac : retrying
        return makeManagementResponse(operation, snapshot as unknown as Record<string, unknown>, 202)
      }
      throw new Error(`Unexpected remote operation: ${operation}`)
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    request.mockClear()

    await wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().trim() === '请求服务端安装或启动')!.trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledWith('system.ssh.enable', { confirmed: true })
    expect(wrapper.text()).toContain('服务端正在等待 Windows UAC（用户账户控制）授权')
    expect(wrapper.text()).not.toContain('服务端微软 OpenSSH Server 已就绪')

    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('Windows 管理员授权已取消')
    const retry = wrapper.findAll('button').find(button => button.text().trim() === '重试服务端 SSH 准备')
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await flushPromises()
    expect(request).toHaveBeenCalledWith('system.ssh.enable', { confirmed: true })

    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.text()).toContain('服务端微软 OpenSSH Server 已就绪')
    expect(wrapper.text()).toContain('OpenSSH Server 已就绪')
    expect(enableCalls).toBe(2)
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    vi.useRealTimers()
  })
})

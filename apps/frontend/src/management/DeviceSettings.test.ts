// @vitest-environment jsdom
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nativeDeviceApi, type DeviceConfigStatus, type DiscoveredDevice, type DeviceConfig, type ManagedResourceJob, type OpenSshServerStatus, type RemoteManagementOperation, type RemoteManagementResponse, type RemoteSshSystemFeatureSnapshot, type RemoteSshSystemFeatureJob } from '../product/native-device-api'
import DeviceSettings from './DeviceSettings.vue'
import { recordDeviceSettingsError } from '../product/device-settings-operation'
import { productApi } from '../product/api'

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
  let mountTarget: HTMLDivElement | undefined // mountTarget（需要真实焦点行为时的挂载节点）。
  let currentStatus: DeviceConfigStatus
  let sshReady: boolean
  let sshConnected: boolean
  let taskRunning: boolean
  let mediaRunning: boolean
  let interactionGeneration: number
  const clipboardWrite = vi.fn()

  /** respondToConfirmation（通过正式项目弹窗确认或取消，不模拟浏览器原生确认框）。 */
  async function respondToConfirmation(accepted = true) {
    await flushPromises()
    const dialog = wrapper!.get('[role="dialog"]')
    expect(dialog.attributes('aria-modal')).toBe('true')
    const action = dialog.findAll('button').find(button => accepted ? button.text().startsWith('确认') : button.text() === '取消')
    expect(action).toBeDefined()
    await action!.trigger('click')
    await flushPromises()
    expect(window.confirm).not.toHaveBeenCalled()
  }

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
    vi.spyOn(nativeDeviceApi, 'versionCheck').mockResolvedValue({host_id:'server-a',local_version:'1.0.0',remote_version:'1.0.0',comparison:'same',can_sync:false,reason:null,fields:{}})
    recordDeviceSettingsError('')
    currentStatus = makeConfig('local', false)
    sshReady = true
    sshConnected = false
    taskRunning = false
    mediaRunning = false
    interactionGeneration = 0
    clipboardWrite.mockReset().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: clipboardWrite } })
    vi.spyOn(window, 'confirm').mockImplementation(() => { throw new Error('设备操作必须使用项目弹窗') })
    vi.spyOn(nativeDeviceApi, 'deviceConfigStatus').mockImplementation(async () => currentStatus)
    vi.spyOn(nativeDeviceApi, 'inboundStatus').mockImplementation(async () => { throw new Error('测试未提供局域网状态') })
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
    vi.spyOn(nativeDeviceApi, 'sshHostStatus').mockImplementation(async () => {
      currentStatus.device_runtime!.remote_client_connected = sshConnected
      return { state: sshConnected ? 'connected' : 'disconnected', connected: sshConnected, fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'connectSshHost').mockImplementation(async () => {
      sshConnected = true
      return { state: 'ready', device_id: 'device-a', address: '192.168.1.20', port: 22,
        host_key_sha256: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', gateway_port: 18765, fields: {} }
    })
    vi.spyOn(nativeDeviceApi, 'disconnectSshHost').mockImplementation(async () => {
      sshConnected = false
      if (currentStatus.device_runtime) currentStatus.device_runtime.remote_client_connected = false
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
    mountTarget?.remove()
    mountTarget = undefined
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('服务端中文名称独立保存，保留设备编号和广播状态且不检查系统', async () => {
    currentStatus = makeConfig('local', true)
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    expect(wrapper.get('.server-name-display').text()).toContain('Voice Terminal')
    expect(wrapper.find('#device-server-name').exists()).toBe(false)
    await wrapper.findAll('button').find(button => button.text() === '重命名')!.trigger('click')
    await wrapper.get('#device-server-name').setValue('  书房语音服务端  ')
    await wrapper.findAll('button').find(button => button.text() === '保存名称')!.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      device_id: 'device-a', server_name: '书房语音服务端', mode: 'local', broadcast_enabled: true,
    }))
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(wrapper.get('.server-name-display').text()).toContain('书房语音服务端')
    expect(wrapper.emitted('notice')?.at(-1)).toEqual(['服务端名称已保存。'])
    expect(wrapper.text()).not.toContain('服务端名称已保存。')
    expect(wrapper.text()).not.toContain('目标模式：')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(wrapper.get('.broadcast-actions input').attributes('role')).toBe('switch')
    expect(wrapper.get('.broadcast-actions button').text()).toBe('获取配对码')
    expect(wrapper.get('.inbound-clients-card .device-card-heading button').text()).toBe('刷新')
  })

  it('过长服务端名称拒绝保存，未保存草稿不会被轻量补查覆盖', async () => {
    vi.useFakeTimers()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '重命名')!.trigger('click')
    await wrapper.get('#device-server-name').setValue('中'.repeat(33))
    await vi.advanceTimersByTimeAsync(1500)
    await wrapper.findAll('button').find(button => button.text() === '保存名称')!.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain('最多 32 个字符')
    expect(wrapper.get<HTMLInputElement>('#device-server-name').element.value).toBe('中'.repeat(33))
  })

  it('名称默认只读，取消重命名不保存，也不改变基础随机名称', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const original = wrapper.get('.server-name-display').text()
    await wrapper.findAll('button').find(button => button.text() === '重命名')!.trigger('click')
    const dialog = wrapper.get('[role="dialog"]')
    expect(dialog.findAll('button').find(button => button.text() === '保存名称')!.attributes('disabled')).toBeDefined()
    await dialog.get('input').setValue('临时名称')
    await dialog.findAll('button').find(button => button.text() === '取消')!.trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(wrapper.get('.server-name-display').text()).toBe(original)
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
  })

  it('同名服务端只在选择列表中编号，配对仍提交真实设备编号', async () => {
    currentStatus = makeConfig('remote', false)
    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, host_id: 'server-b', display_name: '书房服务端' },
      { ...foundHost, host_id: 'server-a', display_name: '书房服务端' },
    ] })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '搜索设备')!.trigger('click')
    await flushPromises()
    const select = wrapper.get('.remote-pairing-card select')
    expect(select.findAll('option').map(option => option.text())).toEqual(['书房服务端 1 · 可连接', '书房服务端 2 · 可连接'])
    await select.setValue('server-b')
    await wrapper.get('input[placeholder="输入服务端显示的配对码"]').setValue('000042')
    await wrapper.get('.remote-pairing-card').findAll('button').find(button => button.text() === '连接')!.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.pairWithDiscoveredDevice).toHaveBeenCalledWith('server-b', '000042')
    expect(currentStatus.device_config?.device_id).toBe('device-a')
  })

  it('同名入站客户端以稳定后缀排序，原始设备身份与名称保持不变', async () => {
    currentStatus = makeConfig('local', true)
    const devices = ['client-b', 'client-a'].map(device_id => ({ device_id, name: '客厅设备', connected: true,
      connection_generation: 1, executor_generation: 1, capabilities: [], error: null, interaction_active: false }))
    vi.spyOn(productApi, 'deviceSnapshot').mockResolvedValue({ devices, active_device_id: null,
      interaction_generation: 0, capability_labels: {}, fields: {} })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.findAll('.inbound-clients-card .remote-device-title strong').map(row => row.text())).toEqual(['客厅设备 1', '客厅设备 2'])
    expect(devices.map(device => [device.device_id, device.name])).toEqual([['client-b', '客厅设备'], ['client-a', '客厅设备']])
  })

  it('断开后的已配对服务端自动发现为可连接，未配对结果仍只在配对区手动搜索', async () => {
    prepareRemoteHost()
    sshConnected = false
    const unrelated = { ...foundHost, host_id: 'unpaired-host', display_name: '未配对服务端' }
    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, devices: [foundHost, unrelated], fields: {} })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(nativeDeviceApi.searchDevices).toHaveBeenCalledWith(1000)
    expect(wrapper.get('.server-connection-row').text()).toContain('可连接')
    expect(wrapper.find('.remote-pairing-card select').exists()).toBe(false)
    expect(wrapper.findAll('.server-connection-row')).toHaveLength(1)
    expect(wrapper.get('.server-connection-row').text()).not.toContain('未配对服务端')
    expect(nativeDeviceApi.connectSshHost).not.toHaveBeenCalled()
    expect(nativeDeviceApi.pairWithDiscoveredDevice).not.toHaveBeenCalled()
    expect(wrapper.get('.remote-channel-status .device-card-heading button').text()).toBe('刷新')
    expect(wrapper.text()).not.toContain('服务端管理连接：')
  })

  it.each([true, false])('服务端重命名后自动更新客户端名称，连接状态为 %s 时不被旧配对配置覆盖', async connected => {
    vi.useFakeTimers()
    prepareRemoteHost()
    sshConnected = connected
    const savedConfig = structuredClone(currentStatus.device_config)
    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, display_name: '书房服务端' },
    ] })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const name = () => wrapper!.get('.remote-channel-status .remote-device-title strong').text()
    expect(name()).toBe('书房服务端')

    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, display_name: '超级服务器' },
    ] })
    await vi.advanceTimersByTimeAsync(10000)
    expect(name()).toBe('超级服务器')
    await vi.advanceTimersByTimeAsync(1500)
    expect(name()).toBe('超级服务器')
    expect(wrapper!.get('.server-connection-row .connection-state').text()).toBe(connected ? '已连接' : '可连接')

    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, display_name: 'Voice Terminal rver-a' },
    ] })
    await vi.advanceTimersByTimeAsync(10000)
    expect(name()).toBe('Voice Terminal rver-a')
    expect(currentStatus.device_config).toEqual(savedConfig)
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(nativeDeviceApi.connectSshHost).not.toHaveBeenCalled()
    expect(nativeDeviceApi.pairWithDiscoveredDevice).not.toHaveBeenCalled()
    expect(nativeDeviceApi.remoteTaskChannel).not.toHaveBeenCalledWith('start')
    expect(nativeDeviceApi.remoteMediaChannel).not.toHaveBeenCalledWith('start')
  })

  it('手动搜索及刷新同步已配对名称，已有搜索选择不阻止后续自动更新', async () => {
    vi.useFakeTimers()
    prepareRemoteHost()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const name = () => wrapper!.get('.remote-channel-status .remote-device-title strong').text()
    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, display_name: '搜索后的服务端' },
    ] })
    await wrapper.get('.remote-pairing-card header button').trigger('click')
    await flushPromises()
    expect(name()).toBe('搜索后的服务端')
    expect(wrapper.get<HTMLSelectElement>('.remote-pairing-card select').element.value).toBe(foundHost.host_id)

    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, display_name: '再次重命名' },
    ] })
    await vi.advanceTimersByTimeAsync(10000)
    expect(name()).toBe('再次重命名')
    expect(wrapper.get('.remote-pairing-card select option').text()).toContain('再次重命名')

    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, display_name: '刷新后的服务端' },
    ] })
    await wrapper.get('.remote-channel-status header button').trigger('click')
    await flushPromises()
    expect(name()).toBe('刷新后的服务端')
  })

  it.each([false, true])('发现失败时保留名称，目标切换后的迟到结果失效（切回原目标：%s）', async returnToOriginal => {
    vi.useFakeTimers()
    prepareRemoteHost()
    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, display_name: '最新服务端名称' },
    ] })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const name = () => wrapper!.get('.remote-channel-status .remote-device-title strong').text()
    expect(name()).toBe('最新服务端名称')
    vi.mocked(nativeDeviceApi.searchDevices).mockRejectedValue(new Error('多播暂不可用'))
    await vi.advanceTimersByTimeAsync(10000)
    expect(name()).toBe('最新服务端名称')
    vi.mocked(nativeDeviceApi.searchDevices).mockResolvedValue({ protocol_version: 1, fields: {}, devices: [
      { ...foundHost, host_id: 'another-host', display_name: '其他服务端' },
    ] })
    await vi.advanceTimersByTimeAsync(10000)
    expect(name()).toBe('最新服务端名称')

    let finishDiscovery!: (result: Awaited<ReturnType<typeof nativeDeviceApi.searchDevices>>) => void
    vi.mocked(nativeDeviceApi.searchDevices).mockImplementationOnce(() => new Promise(resolve => { finishDiscovery = resolve }))
    await vi.advanceTimersByTimeAsync(10000)
    const originalStatus = structuredClone(currentStatus)
    currentStatus = structuredClone(currentStatus)
    currentStatus.device_config!.paired_host!.host_id = 'new-paired-host'
    currentStatus.device_config!.paired_host!.display_name = '新配对服务端'
    await vi.advanceTimersByTimeAsync(1500)
    expect(name()).toBe('新配对服务端')
    if (returnToOriginal) {
      currentStatus = originalStatus
      await vi.advanceTimersByTimeAsync(1500)
    }
    finishDiscovery({ protocol_version: 1, fields: {}, devices: [{ ...foundHost, display_name: '迟到的旧服务端名称' }] })
    await flushPromises()
    expect(name()).toBe(returnToOriginal ? foundHost.display_name : '新配对服务端')
  })

  it('客户端重命名后服务端刷新入站名称，并保留同一连接与登记', async () => {
    vi.useFakeTimers()
    currentStatus = makeConfig('local', true)
    const recordToken = 'f'.repeat(64) // recordToken（同一客户端的可信登记代次）。
    vi.mocked(nativeDeviceApi.inboundStatus).mockResolvedValue({ host_id: 'device-a', discovery_available: true, fields: {}, devices: [
      { device_id: 'client-a', display_name: '原客户端名称', online: true, connected: true, record_token: recordToken },
    ] })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.get('.inbound-clients-card .remote-device-title strong').text()).toBe('原客户端名称')
    vi.mocked(nativeDeviceApi.inboundStatus).mockResolvedValue({ host_id: 'device-a', discovery_available: true, fields: {}, devices: [
      { device_id: 'client-a', display_name: '客厅终端', online: true, connected: true, record_token: recordToken },
    ] })
    await vi.advanceTimersByTimeAsync(10000)
    expect(wrapper.get('.inbound-clients-card .remote-device-title strong').text()).toBe('客厅终端')
    expect(wrapper.get('.inbound-clients-card .connection-state').text()).toBe('已连接')
    expect(wrapper.find('[aria-label="断开 客厅终端"]').exists()).toBe(true)
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
  })

  it('入站客户端按实际连接、局域网发现和持久登记显示三态，不再显示语音设备标记', async () => {
    vi.useFakeTimers()
    currentStatus = makeConfig('local', true)
    const device = (id: string, connected: boolean) => ({ device_id: id, name: id, connected,
      connection_generation: 1, executor_generation: 1, capabilities: [], error: null, interaction_active: true })
    vi.spyOn(productApi, 'deviceSnapshot').mockResolvedValue({ devices: [device('linked', true), device('online', false), device('unregistered', false)],
      active_device_id: 'linked', interaction_generation: 1, capability_labels: {}, fields: {} })
    vi.mocked(nativeDeviceApi.inboundStatus).mockResolvedValue({ host_id: 'device-a', discovery_available: true, fields: {}, devices: [
      { device_id: 'linked', display_name: '甲设备', online: false, connected: false, record_token: 'a'.repeat(64) },
      { device_id: 'online', display_name: '乙设备', online: true, connected: false, record_token: 'b'.repeat(64) },
      { device_id: 'persisted', display_name: '丙设备', online: false, connected: false, record_token: 'c'.repeat(64) },
    ] })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const states = () => new Map(wrapper!.findAll('.inbound-clients-card .remote-device-row').map(row => [row.get('strong').text(), row.get('.connection-state').text()]))
    expect(states()).toEqual(new Map([['甲设备', '已连接'], ['乙设备', '在线'], ['丙设备', '离线']]))
    expect(wrapper.find('.interaction-state').exists()).toBe(false)
    expect(wrapper.get('.inbound-clients-card').text()).not.toContain('当前语音设备')
    expect(wrapper.get('.inbound-clients-card').text()).not.toContain('unregistered')
    vi.mocked(nativeDeviceApi.inboundStatus).mockRejectedValue(new Error('临时多播错误'))
    await vi.advanceTimersByTimeAsync(10000)
    expect(states().get('乙设备')).toBe('在线')
    await vi.advanceTimersByTimeAsync(20000)
    expect(states().get('乙设备')).toBe('待检查')
    expect(states().get('甲设备')).toBe('已连接')
    vi.mocked(nativeDeviceApi.inboundStatus).mockResolvedValue({ host_id: 'device-a', discovery_available: true, fields: {}, devices: [
      { device_id: 'online', display_name: '乙设备', online: false, connected: false, record_token: 'b'.repeat(64) },
    ] })
    await vi.advanceTimersByTimeAsync(10000)
    expect(states()).toEqual(new Map([['乙设备', '离线']]))
    expect(nativeDeviceApi.connectSshHost).not.toHaveBeenCalled()
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
  })

  it('连接中断后自动更新可连接状态，不触发系统检查或前台进度', async () => {
    vi.useFakeTimers()
    prepareRemoteHost()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.get('.server-connection-row .connection-state').text()).toBe('已连接')
    sshConnected = false
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(wrapper.get('.server-connection-row .connection-state').text()).toBe('可连接')
    expect(wrapper.find('.remove-host-button').exists()).toBe(true)
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.connectSshHost).not.toHaveBeenCalled()
  })

  it('移除仅在断开时出现，取消保留资料，确认后必须重新配对', async () => {
    prepareRemoteHost()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.find('.remove-host-button').exists()).toBe(false)
    await wrapper.get('.server-connection-actions button').trigger('click')
    await flushPromises()
    expect(currentStatus.device_config?.paired_host?.host_id).toBe(foundHost.host_id)
    const remove = vi.spyOn(nativeDeviceApi, 'removePairedHost').mockImplementation(async expectedHostId => {
      expect(expectedHostId).toBe(foundHost.host_id)
      currentStatus.device_config!.paired_host = null
      currentStatus.device_config!.reconnect_suspended_reason = null
    })
    await wrapper.get('.remove-host-button').trigger('click')
    await respondToConfirmation(false)
    expect(remove).not.toHaveBeenCalled()
    expect(wrapper.find('.server-connection-row').exists()).toBe(true)
    await wrapper.get('.remove-host-button').trigger('click')
    await respondToConfirmation()
    expect(remove).toHaveBeenCalledWith(foundHost.host_id, true)
    expect(wrapper.find('.server-connection-row').exists()).toBe(false)
    expect(wrapper.get('.remote-channel-status').text()).toContain('等待配对')
    await wrapper.findAll('button').find(button => button.text() === '搜索设备')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('input[placeholder="输入服务端显示的配对码"]').exists()).toBe(true)
    expect(nativeDeviceApi.pairWithDiscoveredDevice).not.toHaveBeenCalled()
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

  it.each([true, false])('零组件客户端能保存本地管理模式，广播保持关闭（已有配置：%s）', async existing => {
    currentStatus = { ...makeConfig('remote', false), local_core_available: false }
    if (!existing) {
      currentStatus.device_config = null
      currentStatus.runtime.config_state = 'missing'
    }
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const local = wrapper.findAll('.mode-options button').find(button => button.text() === '本地运行')!
    expect(local.attributes('disabled')).toBeUndefined()
    await local.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === '保存运行模式')!.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'local', desired_role: 'standalone', broadcast_enabled: false,
    }), false)
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
  })

  it('远程配对合并搜索、配对码、SSH 和偏好设置，并提供客户端重命名', async () => {
    currentStatus = makeConfig('remote', false)
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const card = wrapper.get('.remote-pairing-card')
    expect(card.get('header h3').text()).toBe('远程配对')
    await card.get('header button').trigger('click')
    await flushPromises()
    expect(card.find('select[aria-label="选择服务端"]').exists()).toBe(true)
    expect(card.get('.pairing-submit-row input').attributes('aria-label')).toBe('六位配对码')
    expect(card.get('.pairing-submit-row button').text()).toBe('连接')
    expect(card.find('.remote-ssh-feature-card').exists()).toBe(true)
    expect(card.findAll('[role="switch"]')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('本设备连接')
    await wrapper.get('.client-name-card button').trigger('click')
    expect(wrapper.get('[role="dialog"]').text()).toContain('重命名客户端')
    await wrapper.get('#device-server-name').setValue('客厅终端')
    await wrapper.findAll('button').find(button => button.text() === '保存名称')!.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({server_name: '客厅终端', mode: 'remote'}))
    expect(wrapper.get('.client-name-card').text()).toContain('客厅终端')
  })

  it('入站管理连接已建立时可断开，移除仅断开后确认，并固定登记代次', async () => {
    const token = 'd'.repeat(64)
    const presence = { host_id: 'device-a', discovery_available: true, fields: {}, devices: [
      { device_id: 'client-a', display_name: '客厅终端', online: true, connected: true, record_token: token },
    ] }
    vi.mocked(nativeDeviceApi.inboundStatus).mockResolvedValue(presence)
    const control = vi.spyOn(nativeDeviceApi, 'controlInboundDevice').mockImplementation(async (_host, _device, _token, action) => {
      if (action === 'disconnect') presence.devices[0]!.connected = false
      else presence.devices = []
      return {device_id: 'client-a', action, completed: true, fields: {}}
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.get('.inbound-clients-card .connection-state').text()).toBe('已连接')
    expect(wrapper.find('[aria-label="移除 客厅终端"]').exists()).toBe(false)
    await wrapper.get('[aria-label="断开 客厅终端"]').trigger('click')
    await flushPromises()
    expect(control).toHaveBeenCalledWith('device-a', 'client-a', token, 'disconnect')
    expect(wrapper.get('.inbound-clients-card .connection-state').text()).toBe('在线')
    await wrapper.get('[aria-label="移除 客厅终端"]').trigger('click')
    await respondToConfirmation(false)
    expect(control).toHaveBeenCalledTimes(1)
    await wrapper.get('[aria-label="移除 客厅终端"]').trigger('click')
    await respondToConfirmation()
    expect(control).toHaveBeenLastCalledWith('device-a', 'client-a', token, 'remove')
    expect(wrapper.get('.inbound-clients-card').text()).toContain('暂无已登记客户端')
  })

  it('主机身份异常时连接入口转入重新配对，保留旧信任和暂停状态', async () => {
    prepareRemoteHost(); sshConnected = false
    currentStatus.device_config!.reconnect_suspended_reason = 'host_key_changed'
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.get('.server-connection-row .connection-state').text()).toBe('需重新配对')
    const rePair = wrapper.findAll('.server-connection-row button').find(button => button.text() === '重新配对')!
    await rePair.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.searchDevices).toHaveBeenCalled()
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(nativeDeviceApi.connectSshHost).not.toHaveBeenCalled()
    expect(currentStatus.device_config?.paired_host?.host_id).toBe(foundHost.host_id)
    expect(currentStatus.device_config?.reconnect_suspended_reason).toBe('host_key_changed')
  })

  it('系统公钥清理失败保留待清理条目，提供重试而不恢复客户端连接', async () => {
    vi.mocked(nativeDeviceApi.inboundStatus).mockResolvedValue({host_id:'device-a', discovery_available:true, fields:{}, devices:[
      {device_id:'cleanup-client', display_name:'待清理终端', online:false, connected:false, removal_pending:true, record_token:'e'.repeat(64)},
    ]})
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const row = wrapper.get('.inbound-clients-card .server-connection-row')
    expect(row.get('.connection-state').text()).toBe('待清理')
    expect(row.get('button').text()).toBe('重试移除')
    expect(row.text()).not.toContain('断开')
    expect(nativeDeviceApi.connectSshHost).not.toHaveBeenCalled()
  })

  it('未配对远程角色不报告本机核心缺失，任务和语音明确等待配对', async () => {
    currentStatus = makeConfig('remote', false)
    currentStatus.device_runtime!.actual_mode = null
    currentStatus.device_runtime!.switch_phase = 'waiting_for_remote'
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.text()).not.toContain('目标模式：')
    expect(wrapper.text()).not.toContain('本机语音核心尚未就绪')
    expect(wrapper.get('.remote-channel-status').text()).toContain('等待配对')
    expect(wrapper.find('.server-connection-row').exists()).toBe(false)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('SSH 已认证但管理失败时仍能断开并持久暂停重连，保留原配对身份', async () => {
    prepareRemoteHost()
    vi.mocked(nativeDeviceApi.remoteManagementRequest).mockRejectedValue(new Error('管理通道回执不可用'))
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.get('.server-connection-row').text()).toContain('可连接')
    expect(wrapper.find('.remove-host-button').exists()).toBe(false)
    const disconnect = wrapper.findAll('button').find(button => button.text().trim() === '断开')
    expect(disconnect).toBeDefined()
    await disconnect!.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.disconnectSshHost).toHaveBeenCalledOnce()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({ reconnect_suspended_reason: 'user_disconnected' }))
    expect(currentStatus.device_config?.paired_host?.host_id).toBe(foundHost.host_id)
    expect(wrapper.findAll('button').find(button => button.text().trim() === '断开')).toBeUndefined()
  })

  it('任务通道已就绪时仍单独显示桌面能力不可用及原因', async () => {
    prepareRemoteHost()
    vi.mocked(nativeDeviceApi.remoteTaskChannel).mockResolvedValue({
      state: 'running', device_id: 'device-a', connection_generation: 1, executor_generation: 1,
      desktop_control: false, desktop_control_error: '当前没有交互用户会话。', fields: {},
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const channelStatus = wrapper.get('.remote-channel-status').text()
    expect(channelStatus).toContain('任务正常')
    expect(channelStatus).toContain('桌面不可用')
    expect(channelStatus).toContain('当前没有交互用户会话。')
  })

  it('运行模式经右侧保存后生效；配对码保持前导零并自动建立连接通道', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.get('[aria-label="运行模式"] button:last-child').trigger('click')
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    await wrapper.get('.device-mode-card .save-button').trigger('click')
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

  it('配对登记失败后清除授权进度，保留真实错误并允许重试', async () => {
    let rejectPairing!: (error: Error) => void
    vi.mocked(nativeDeviceApi.pairWithDiscoveredDevice).mockImplementation(() => new Promise((_, reject) => {
      rejectPairing = reject
    }))
    currentStatus = makeConfig('remote', false)
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().trim() === '搜索设备')!.trigger('click')
    await flushPromises()
    await wrapper.get('input[placeholder="输入服务端显示的配对码"]').setValue('000042')
    const connect = wrapper.findAll('button').find(button => button.text().trim() === '连接')!
    await connect.trigger('click')
    expect(wrapper.emitted('notice')?.at(-1)?.[0]).toContain('正在验证配对并登记受限公钥')
    expect(wrapper.get('.managed-resource-progress').text()).toContain('正在验证配对')
    rejectPairing(new Error('服务端未能安全登记本次设备；请检查服务端配对状态后重试。'))
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('服务端未能安全登记本次设备')
    expect(wrapper.emitted('notice')?.at(-1)).toEqual([''])
    expect(wrapper.find('.managed-resource-progress').exists()).toBe(false)
    expect(connect.attributes('disabled')).toBeUndefined()
    expect(nativeDeviceApi.remoteTaskChannel).not.toHaveBeenCalledWith('start')
    expect(nativeDeviceApi.remoteMediaChannel).not.toHaveBeenCalledWith('start')
  })

  it('配对成功但桥接失败时清除登记进度并显示连接错误', async () => {
    vi.mocked(nativeDeviceApi.pairWithDiscoveredDevice).mockResolvedValue({
      state: 'paired', device_id: 'device-a', host_id: foundHost.host_id,
      display_name: foundHost.display_name, ssh_port: 22, address: foundHost.addresses[0],
      connected: false, connection_error: 'SSH 桥接身份参数格式无效。', fields: {},
    })
    currentStatus = makeConfig('remote', false)
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().trim() === '搜索设备')!.trigger('click')
    await flushPromises()
    await wrapper.get('input[placeholder="输入服务端显示的配对码"]').setValue('000042')
    await wrapper.findAll('button').find(button => button.text().trim() === '连接')!.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('配对和服务端身份保存成功，但 SSH 桥接连接失败')
    expect(wrapper.get('[role="alert"]').text()).toContain('SSH 桥接身份参数格式无效')
    expect(wrapper.text()).not.toContain('正在验证配对并登记受限公钥')
    expect(nativeDeviceApi.remoteTaskChannel).not.toHaveBeenCalledWith('start')
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
    mountTarget = document.createElement('div')
    document.body.append(mountTarget)
    wrapper = mount(DeviceSettings, { attachTo: mountTarget })
    await flushPromises()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockClear()
    const checkbox = wrapper.get('input[aria-label="广播并接受配对"]')
    await checkbox.setValue(true)
    await flushPromises()

    const getCode = wrapper.findAll('button').find(button => button.text().trim() === '获取配对码')
    expect(getCode).toBeDefined()
    getCode!.element.focus()
    await getCode!.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'local', broadcast_enabled: true, desired_role: 'shared_host',
    }), false)
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(nativeDeviceApi.openSshServerStatus).toHaveBeenCalledTimes(1)
    expect(vi.mocked(nativeDeviceApi.openSshServerStatus).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(nativeDeviceApi.saveDeviceConfig).mock.invocationCallOrder[0]!)
    expect(nativeDeviceApi.openPairingWindow).toHaveBeenCalledOnce()
    expect(clipboardWrite).toHaveBeenCalledWith('000042')
    expect(wrapper.get('.pairing-code-box output').text()).toBe('000042')
    expect(wrapper.get('[role="dialog"]').text()).toContain('本机配对码')
    expect(wrapper.get('.pairing-code-box output').attributes('aria-label')).toBe('本机六位配对码')
    const copy = wrapper.get('[role="dialog"]').findAll('button').find(button => button.text() === '复制配对码')!
    await copy.trigger('click')
    await flushPromises()
    await copy.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('notice')?.filter(([message]) => message === '六位配对码已复制到剪贴板。')).toHaveLength(2)
    await wrapper.get('[role="dialog"]').findAll('button').find(button => button.text() === '关闭配对窗口')!.trigger('click')
    await flushPromises()
    expect(nativeDeviceApi.closePairingWindow).toHaveBeenCalledOnce()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(getCode!.element)
  })

  it('历史 SSH 失败不覆盖运行状态，进入页面只读取任务而不检查系统', async () => {
    vi.mocked(nativeDeviceApi.resourceJobs).mockResolvedValue([
      { ...makeLocalSshJob('failed'), error: '旧版本的状态查询失败' },
    ])
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.resourceJobs).toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('旧版本的状态查询失败')
    expect(wrapper.text()).toContain('广播已关闭')
  })

  it('自动复制挂起时配对码弹窗仍可关闭，迟到回执不覆盖关闭结果', async () => {
    currentStatus = makeConfig('local', true)
    let finishCopy!: () => void
    clipboardWrite.mockImplementationOnce(() => new Promise<void>(resolve => { finishCopy = resolve }))
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '获取配对码')!.trigger('click')
    await flushPromises()
    const close = wrapper.get('[role="dialog"]').findAll('button').find(button => button.text() === '关闭配对窗口')!
    expect(close.element.disabled).toBe(false)
    expect(wrapper.get('.pairing-code-box output').text()).toBe('000042')
    await close.trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(nativeDeviceApi.closePairingWindow).toHaveBeenCalledOnce()
    finishCopy()
    await flushPromises()
    expect(wrapper.emitted('notice')?.at(-1)).toEqual(['六位码和本机配对监听器已关闭。'])
    expect(wrapper.emitted('notice')?.some(([message]) => String(message).includes('六位配对码已复制'))).toBe(false)
  })

  it('进入和重建页面不检查 SSH，轻量补查保留未保存的运行模式', async () => {
    vi.useFakeTimers()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.get('[aria-label="运行模式"] button:last-child').trigger('click')
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.get('[aria-label="运行模式"] button:last-child').attributes('aria-pressed')).toBe('true')
    expect(currentStatus.device_config?.mode).toBe('local')
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    wrapper.unmount()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(wrapper.get('.ssh-status-row [role="status"]').text()).toBe('未检查')
  })

  it('切换页面后接续未完成保存进度，不重复保存或授权，完成后恢复可用', async () => {
    vi.useFakeTimers()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    let finishSave!: () => void
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockImplementationOnce(async config => {
      await new Promise<void>(resolve => { finishSave = resolve })
      currentStatus = makeConfig('local', true)
      currentStatus.device_config = config
      return currentStatus
    })
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()
    wrapper.unmount()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.text()).not.toContain('切换页面不会取消任务')
    expect(wrapper.emitted('notice')?.some(([message]) => String(message).includes('切换页面不会取消任务'))).toBe(true)
    expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
    expect(wrapper.get('input[aria-label="广播并接受配对"]').attributes('disabled')).toBeDefined()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledOnce()
    finishSave()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.get('.broadcast-status').text()).toContain('广播正常')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledOnce()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
  })

  it('页面重建从宿主恢复授权阶段与失败终态，广播未保存时也继续补查', async () => {
    vi.useFakeTimers()
    currentStatus.device_runtime!.operation = { active: true, revision: 2, phase: 'preparing_access',
      state_revision: 4, message: '等待 Windows 管理员授权', target_mode: 'local', target_broadcast: true, error: null }
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.get('.broadcast-status').text()).toBe('配置中')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
    currentStatus = structuredClone(currentStatus)
    currentStatus.device_runtime!.operation = { ...currentStatus.device_runtime!.operation!, active: false,
      state_revision: 3, phase: 'failed', error: '过期任务错误' }
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.get('.broadcast-status').text()).toBe('配置中')
    expect(wrapper.text()).not.toContain('过期任务错误')
    currentStatus.device_runtime!.operation = { ...currentStatus.device_runtime!.operation!,
      state_revision: 5, error: 'Windows 管理员授权已取消。' }
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.text()).toContain('Windows 管理员授权已取消。')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined()
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
  })

  it('广播保存期间立即显示准备状态，晚到的发布结果只通过轻量状态补查更新', async () => {
    vi.useFakeTimers()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    let finishSave!: () => void
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockImplementationOnce(async config => {
      await new Promise<void>(resolve => { finishSave = resolve })
      currentStatus = makeConfig('local', true)
      currentStatus.device_config = config
      currentStatus.device_runtime!.broadcast_actual = false
      currentStatus.device_runtime!.device_gateway.ready = false
      return currentStatus
    })
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()
    expect(wrapper.get('.broadcast-status').text()).toContain('配置中')
    finishSave()
    await flushPromises()
    expect(wrapper.get('.broadcast-status').text()).toContain('网关准备中')
    vi.mocked(nativeDeviceApi.openSshServerStatus).mockClear()
    currentStatus.device_runtime!.broadcast_actual = true
    currentStatus.device_runtime!.device_gateway.ready = true
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.get('.broadcast-status').text()).toContain('广播正常')
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
  })

  it('公告发布失败后重读已保存的开关并显示实际故障', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockImplementationOnce(async config => {
      currentStatus = makeConfig('local', true)
      currentStatus.device_config = config
      currentStatus.device_runtime!.broadcast_actual = false
      currentStatus.device_runtime!.error = '局域网公告发布失败'
      throw new Error('本地运行模式已保存，但局域网广播尚未实际发布')
    })
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()
    expect(wrapper.get('input[aria-label="广播并接受配对"]').element).toHaveProperty('checked', true)
    expect(wrapper.get('.broadcast-status').text()).toContain('广播异常')
    expect(wrapper.text()).toContain('局域网公告发布失败')
  })

  it('广播开启在 SSH 缺失时先确认官方下载，准备就绪后才保存和开启网络访问', async () => {
    sshReady = false
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockClear()
    vi.mocked(nativeDeviceApi.enableOpenSshServer).mockImplementationOnce(async () => {
      sshReady = true
      return { job_id: 'system-feature-openssh-server', fields: {} }
    })
    vi.mocked(nativeDeviceApi.resourceJobs).mockResolvedValue([makeLocalSshJob('complete')])
    let finishSave!: () => void
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockImplementationOnce(async config => {
      await new Promise<void>(resolve => { finishSave = resolve })
      currentStatus = makeConfig('local', true)
      currentStatus.device_config = config
      return currentStatus
    })
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()
    expect(wrapper.get('[role="dialog"]').text()).toContain('Windows Update（系统更新）下载')
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('切换页面不会取消任务')
    expect(wrapper.emitted('notice')?.some(([message]) => String(message).includes('切换页面不会取消任务'))).toBe(true)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
    await respondToConfirmation()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledOnce()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledWith(expect.objectContaining({ mode: 'local', broadcast_enabled: true }), false)
    expect(nativeDeviceApi.enableOpenSshServer).toHaveBeenCalledExactlyOnceWith(true)
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(vi.mocked(nativeDeviceApi.enableOpenSshServer).mock.invocationCallOrder[0]!)
      .toBeLessThan(vi.mocked(nativeDeviceApi.saveDeviceConfig).mock.invocationCallOrder[0]!)
    expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
    finishSave()
    await flushPromises()
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('input[aria-label="广播并接受配对"]').element).toHaveProperty('checked', true)
  })

  it('广播开启重新检查已缓存的 SSH 状态，取消下载时不安装、不保存且恢复关闭状态', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()
    sshReady = false
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()
    expect(nativeDeviceApi.openSshServerStatus).toHaveBeenCalledWith(true)
    await respondToConfirmation(false)
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(wrapper.get('input[aria-label="广播并接受配对"]').element).toHaveProperty('checked', false)
    expect(wrapper.text()).toContain('你取消了 SSH 服务准备')
  })

  it('广播开启的官方下载任务失败时不保存配置，保留实际错误和重试入口', async () => {
    sshReady = false
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.resourceJobs).mockResolvedValue([
      { ...makeLocalSshJob('failed'), error: 'Windows Update（系统更新）源不可用' },
    ])
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await respondToConfirmation()
    expect(nativeDeviceApi.enableOpenSshServer).toHaveBeenCalledWith(true)
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Windows Update（系统更新）源不可用')
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined()
  })

  it('SSH 准备超过五分钟后继续显示任务，晚到完成时清除等待告警并恢复检查按钮', async () => {
    vi.useFakeTimers()
    sshReady = false
    let jobState = 'running'
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.resourceJobs).mockImplementation(async () => [makeLocalSshJob(jobState)])

    await wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!.trigger('click')
    await respondToConfirmation()
    await vi.advanceTimersByTimeAsync(5 * 60_000 + 1200)
    await flushPromises()

    expect(wrapper.text()).toContain('Windows SSH 准备任务仍在进行')
    expect(wrapper.text()).toContain('任务状态：进行中')
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuetext')).toContain('任务状态：进行中')
    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!
    expect(check.element.disabled).toBe(true)

    jobState = 'complete'
    sshReady = true
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()

    expect(wrapper.emitted('notice')?.at(-1)).toEqual(['SSH（安全外壳协议）服务正常。'])
    expect(wrapper.text()).not.toContain('准备任务仍在进行')
    expect(wrapper.text()).not.toContain('等待 Windows SSH 服务准备超时')
    expect(check.element.disabled).toBe(false)
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
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
    await respondToConfirmation()
    await vi.advanceTimersByTimeAsync(5 * 60_000 + 1200)
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

  it('广播维修的系统授权取消后恢复实际配置，不另开安装任务或重复保存', async () => {
    sshReady = true
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockClear()
    vi.mocked(nativeDeviceApi.saveDeviceConfig).mockRejectedValueOnce(new Error('已取消 Windows 管理员授权；系统维护未执行。'))
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledOnce()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(window.confirm).not.toHaveBeenCalled()
    expect(wrapper.get('input[aria-label="广播并接受配对"]').element).toHaveProperty('checked', false)
    expect(wrapper.get('[role="alert"]').text()).toContain('已取消 Windows 管理员授权')
  })

  it('官方下载超过五分钟并切换页面后，任务完成继续开启原请求的广播，不重复下载或残留失败提示', async () => {
    vi.useFakeTimers()
    sshReady = false
    let jobState = 'running'
    wrapper = mount(DeviceSettings)
    await flushPromises()
    vi.mocked(nativeDeviceApi.resourceJobs).mockImplementation(async () => [makeLocalSshJob(jobState)])
    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await respondToConfirmation()
    await vi.advanceTimersByTimeAsync(5 * 60_000 + 1200)
    await flushPromises()
    expect(wrapper.text()).toContain('Windows SSH 准备任务仍在进行')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    wrapper.unmount()
    wrapper = mount(DeviceSettings)
    await flushPromises()
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
    jobState = 'complete'
    sshReady = true
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(nativeDeviceApi.enableOpenSshServer).toHaveBeenCalledExactlyOnceWith(true)
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ broadcast_enabled: true }), false)
    expect(wrapper.get('.broadcast-status').text()).toContain('广播正常')
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.get('fieldset').attributes('disabled')).toBeUndefined()
  })

  it('SSH 已就绪时按钮只检查状态，不重新安装或重启', async () => {
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')
    await check!.trigger('click')
    await flushPromises()

    expect(nativeDeviceApi.openSshServerStatus).toHaveBeenCalled()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(wrapper.emitted('notice')?.at(-1)).toEqual(['SSH（安全外壳协议）服务正常。'])
  })

  it('SSH 未就绪且用户取消时不启动安装任务，并允许再次检查', async () => {
    sshReady = false
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')
    await check!.trigger('click')
    await respondToConfirmation(false)

    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('你取消了 SSH 服务准备')
    expect(check!.element.disabled).toBe(false)
  })

  it('项目确认框可用 Escape 取消，关闭页面也不会提交尚未确认的下载', async () => {
    sshReady = false
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const check = () => wrapper!.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!
    await check().trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="dialog"]').text()).toContain('准备本机 SSH 服务')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(check().element.disabled).toBe(false)

    await wrapper.get('input[aria-label="广播并接受配对"]').setValue(true)
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
    wrapper.unmount()
    wrapper = undefined
    await flushPromises()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    expect(window.confirm).not.toHaveBeenCalled()
  })

  it('损坏配置通过项目弹窗确认归档，取消保留原件，确认才提交修复', async () => {
    currentStatus.runtime.config_state = 'invalid'
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const repair = () => wrapper!.findAll('button').find(button => button.text() === '归档原件并修复')!
    await repair().trigger('click')
    await respondToConfirmation(false)
    expect(nativeDeviceApi.saveDeviceConfig).not.toHaveBeenCalled()
    await repair().trigger('click')
    await respondToConfirmation()
    expect(nativeDeviceApi.saveDeviceConfig).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ mode: 'local' }), true)
  })

  it('搜索、配对和通道准备共用一个进度条，阶段变化时不显示配对码或虚假百分比', async () => {
    currentStatus = makeConfig('remote')
    let finishSearch!: () => void
    let finishPairing!: () => void
    let finishChannels!: () => void
    const pair = vi.mocked(nativeDeviceApi.pairWithDiscoveredDevice).getMockImplementation()!
    const tasks = vi.mocked(nativeDeviceApi.remoteTaskChannel).getMockImplementation()!
    vi.mocked(nativeDeviceApi.searchDevices).mockImplementationOnce(async () => {
      await new Promise<void>(resolve => { finishSearch = resolve })
      return { protocol_version: 1, devices: [foundHost], fields: {} }
    })
    vi.mocked(nativeDeviceApi.pairWithDiscoveredDevice).mockImplementationOnce(async (...args) => {
      await new Promise<void>(resolve => { finishPairing = resolve })
      return pair(...args)
    })
    vi.mocked(nativeDeviceApi.remoteTaskChannel).mockImplementation(async action => {
      if (action === 'start') await new Promise<void>(resolve => { finishChannels = resolve })
      return tasks(action)
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const expectProgress = (phase: string) => {
      expect(wrapper!.findAll('[role="progressbar"]')).toHaveLength(1)
      const progress = wrapper!.get('[role="progressbar"]')
      expect(progress.attributes('aria-label')).toBe('设备连接进度')
      expect(progress.attributes('aria-valuetext')).toContain(phase)
      expect(progress.attributes('aria-valuetext')).not.toContain('000042')
      expect(progress.attributes('aria-valuenow')).toBeUndefined()
    }
    await wrapper.findAll('button').find(button => button.text() === '搜索设备')!.trigger('click')
    await flushPromises()
    expectProgress('正在搜索')
    finishSearch()
    await flushPromises()
    await wrapper.get('input[autocomplete="one-time-code"]').setValue('000042')
    await wrapper.findAll('button').find(button => button.text() === '连接')!.trigger('click')
    await flushPromises()
    expectProgress('正在验证配对')
    finishPairing()
    await flushPromises()
    expectProgress('正在准备任务与语音通道')
    finishChannels()
    await flushPromises()
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(nativeDeviceApi.pairWithDiscoveredDevice).toHaveBeenCalledWith(foundHost.host_id, '000042')
  })

  it('重新连接服务端时显示统一进度，连接失败后保留错误并恢复重试', async () => {
    prepareRemoteHost()
    sshConnected = false
    let failConnection!: (failure: Error) => void
    vi.mocked(nativeDeviceApi.connectSshHost).mockImplementationOnce(() => new Promise((_, reject) => { failConnection = reject }))
    wrapper = mount(DeviceSettings)
    await flushPromises()
    const connect = () => wrapper!.findAll('button').find(button => button.text() === '连接')!
    await connect().trigger('click')
    await flushPromises()
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuetext')).toContain('正在连接配对服务端')
    expect(connect().element.disabled).toBe(true)
    failConnection(new Error('服务端连接超时'))
    await flushPromises()
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain('服务端连接超时')
    expect(connect().element.disabled).toBe(false)
  })

  it('其他入站规则默认收起，只显示有限条数和短摘要', async () => {
    sshReady = false
    const snapshot = makeSshStatus(false)
    snapshot.existing_ssh_inbound_rules = Array.from({ length: 8 }, (_, index) =>
      `possible-ssh-rule-${index} ${'RemoteAddress=Any '.repeat(30)}`)
    vi.mocked(nativeDeviceApi.openSshServerStatus).mockResolvedValue(snapshot)
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()

    const details = wrapper.get('details.ssh-rule-details')
    expect((details.element as HTMLDetailsElement).open).toBe(false)
    expect(details.get('summary').text()).toBe('其他入站规则：8 条')
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
    expect(wrapper.text()).toContain('正常')
    expect(wrapper.get('.remote-ssh-feature-card .ssh-status-row [role="status"]').text()).toBe('正常')
    expect(snapshot.status.source_version).toBe('9.5.0.0')
    expect(wrapper.text()).not.toContain('MACHINE\\tester')
    expect(wrapper.text()).not.toContain('authorized_keys')
    expect(wrapper.find('input[placeholder*="192.168"]').exists()).toBe(false)
    expect(wrapper.find('input[placeholder*="SHA256"]').exists()).toBe(false)
  })

  it.each([
    { state: 'failed', label: '失败', createdAt: undefined },
    { state: 'failed', label: '失败', createdAt: 1794928935566 },
    { state: 'cancelled', label: '已取消', createdAt: undefined },
    { state: 'complete', label: '已完成', createdAt: undefined },
  ] as const)('服务端已就绪时，$state 任务仅作为可展开的历史记录（创建时间：$createdAt）', async ({ state, label, createdAt }) => {
    prepareRemoteHost()
    const historicalError = state === 'failed' ? '旧版本的状态查询失败' : null
    const job = { ...makeRemoteSshJob(state, '此前的准备阶段', historicalError), created_at: createdAt }
    const snapshot = makeRemoteSshSnapshot(true, job)
    snapshot.status.capability_state = 'Unknown'
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

    const card = wrapper.get('.remote-ssh-feature-card')
    expect(card.get('[data-state="ready"]').text()).toContain('正常')
    expect(card.find('.device-message.error').exists()).toBe(false)
    expect(card.find('[role="alert"]').exists()).toBe(false)
    expect(card.findAll('button').map(button => button.text().trim())).toEqual(['检查 SSH 服务'])
    const history = card.get('.remote-ssh-job-history')
    expect((history.element as HTMLDetailsElement).open).toBe(false)
    expect(history.get('summary').text()).toBe(`历史准备任务：${label}（当前服务已就绪）`)
    expect(history.text()).toContain('当时阶段：此前的准备阶段')
    if (historicalError) expect(history.text()).toContain(`历史错误：${historicalError}`)
    if (createdAt === undefined) {
      expect(history.find('time').exists()).toBe(false)
    } else {
      expect(history.get('time').attributes('datetime')).toBe('2026-11-17T15:22:15.566Z')
    }
    await history.get('summary').trigger('click')
    expect((history.element as HTMLDetailsElement).open).toBe(true)
    expect(snapshot.job?.state).toBe(state)
    expect(snapshot.job?.error).toBe(historicalError)
    expect(request.mock.calls.map(([operation]) => operation)).toEqual(['system.ssh.status'])
    expect(window.confirm).not.toHaveBeenCalled()
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
  })

  it('服务端当前检查恢复就绪后清除失败提示，再次未就绪时仍显示原失败与重试入口', async () => {
    prepareRemoteHost()
    const job = makeRemoteSshJob('failed', '资源处理失败', '旧版本的状态查询失败')
    let ready = false
    const request = vi.mocked(nativeDeviceApi.remoteManagementRequest).mockImplementation(async operation => {
      if (operation === 'management.status') return makeManagementResponse(operation, { core_state: 'ready' })
      if (operation === 'system.ssh.status') {
        return makeManagementResponse(operation, makeRemoteSshSnapshot(ready, job) as unknown as Record<string, unknown>)
      }
      throw new Error(`Unexpected remote operation: ${operation}`)
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    request.mockClear()
    const check = wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!

    for (const currentReady of [false, true, false]) {
      ready = currentReady
      await check.trigger('click')
      await flushPromises()
      const card = wrapper.get('.remote-ssh-feature-card')
      expect(card.find('.remote-ssh-job-history').exists()).toBe(currentReady)
      expect(card.find('[role="alert"]').exists()).toBe(!currentReady)
      expect(card.findAll('button').some(button => button.text() === '重试服务端 SSH 准备')).toBe(!currentReady)
      if (!currentReady) {
        expect(card.get('[role="alert"]').text()).toBe('旧版本的状态查询失败')
        expect(card.text()).toContain('服务端任务：失败')
      }
    }
    expect(job.state).toBe('failed')
    expect(request.mock.calls.map(([operation]) => operation)).toEqual(Array(3).fill('system.ssh.status'))
  })

  it('服务端已就绪时仍跟踪进行中的准备任务，任务结束后转入历史并停止补查', async () => {
    vi.useFakeTimers()
    prepareRemoteHost()
    let statusReads = 0
    vi.mocked(nativeDeviceApi.remoteManagementRequest).mockImplementation(async operation => {
      if (operation === 'management.status') return makeManagementResponse(operation, { core_state: 'ready' })
      if (operation === 'system.ssh.status') {
        statusReads += 1
        const job = statusReads === 1
          ? makeRemoteSshJob('running', '正在复核服务端状态')
          : makeRemoteSshJob('failed', '资源处理失败', '此前的准备任务失败')
        return makeManagementResponse(operation, makeRemoteSshSnapshot(true, job) as unknown as Record<string, unknown>)
      }
      throw new Error(`Unexpected remote operation: ${operation}`)
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()

    await wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('服务端任务：进行中')
    expect(wrapper.find('.remote-ssh-job-history').exists()).toBe(false)

    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(statusReads).toBe(2)
    expect(wrapper.get('.remote-ssh-job-history summary').text()).toContain('失败（当前服务已就绪）')
    expect(wrapper.find('.remote-ssh-feature-card .device-message.error').exists()).toBe(false)

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(statusReads).toBe(2)
  })

  it('远程服务端未就绪时必须明确确认，取消不发送启动操作', async () => {
    prepareRemoteHost()
    const snapshot = makeRemoteSshSnapshot(false)
    const request = vi.mocked(nativeDeviceApi.remoteManagementRequest).mockImplementation(async operation => {
      if (operation === 'management.status') return makeManagementResponse(operation, { core_state: 'ready' })
      if (operation === 'system.ssh.status') return makeManagementResponse(operation, snapshot as unknown as Record<string, unknown>)
      throw new Error(`Unexpected remote operation: ${operation}`)
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()

    await wrapper.findAll('button').find(button => button.text().trim() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()
    const prepare = wrapper.findAll('button').find(button => button.text().trim() === '请求服务端安装或启动')
    expect(prepare).toBeDefined()
    await prepare!.trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="dialog"]').text()).toContain('服务端 A')
    await respondToConfirmation(false)
    expect(request.mock.calls.filter(([operation]) => operation === 'system.ssh.enable')).toHaveLength(0)
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    expect(wrapper.get('.remote-ssh-feature-card .ssh-status-row [role="status"]').text()).toBe('程序异常')
  })

  it('服务端确认等待期间更换配对目标，旧确认不能授权新服务端', async () => {
    vi.useFakeTimers()
    prepareRemoteHost()
    const request = vi.mocked(nativeDeviceApi.remoteManagementRequest).mockImplementation(async operation => {
      return makeManagementResponse(operation, operation === 'management.status' ? { core_state: 'ready' }
        : makeRemoteSshSnapshot(false) as unknown as Record<string, unknown>)
    })
    wrapper = mount(DeviceSettings)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '检查 SSH 服务')!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '请求服务端安装或启动')!.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="dialog"]').text()).toContain('服务端 A')
    currentStatus = structuredClone(currentStatus)
    currentStatus.device_config!.paired_host!.host_id = 'server-b'
    currentStatus.device_config!.paired_host!.display_name = '服务端 B'
    await vi.advanceTimersByTimeAsync(1500)
    await respondToConfirmation()
    expect(request.mock.calls.filter(([operation]) => operation === 'system.ssh.enable')).toHaveLength(0)
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
    await respondToConfirmation()
    expect(request).toHaveBeenCalledWith('system.ssh.enable', { confirmed: true })
    expect(wrapper.text()).toContain('服务端正在等待 Windows UAC（用户账户控制）授权')
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuetext')).toContain('等待 Windows 管理员授权')
    expect(wrapper.text()).not.toContain('正常')

    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('Windows 管理员授权已取消')
    const retry = wrapper.findAll('button').find(button => button.text().trim() === '重试服务端 SSH 准备')
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await respondToConfirmation()
    expect(request).toHaveBeenCalledWith('system.ssh.enable', { confirmed: true })

    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.text()).toContain('正常')
    expect(wrapper.text()).toContain('OpenSSH Server 已就绪')
    expect(enableCalls).toBe(2)
    expect(nativeDeviceApi.openSshServerStatus).not.toHaveBeenCalled()
    expect(nativeDeviceApi.enableOpenSshServer).not.toHaveBeenCalled()
    vi.useRealTimers()
  })
})

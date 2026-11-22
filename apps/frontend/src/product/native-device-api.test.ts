// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nativeDeviceApi } from './native-device-api'

afterEach(() => {
  vi.restoreAllMocks()
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__')
})

describe('远程管理会话生命周期', () => {
  it('移除配对使用固定服务端编号，迟到管理状态不能复活已移除的会话', async () => {
    let complete!: (value: unknown) => void
    const pending = new Promise(resolve => { complete = resolve })
    const invoke = vi.fn(async (command: string) => command === 'device_config_status'
      ? { runtime: { mode: 'remote' }, device_config: { mode: 'remote', paired_host: { host_id: 'server-remove' } } }
      : command === 'device_ssh_management_request' ? pending : undefined)
    window.__TAURI_INTERNALS__ = { invoke: invoke as never }
    const result = nativeDeviceApi.remoteManagementRequest('management.status', {})
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    await nativeDeviceApi.removePairedHost('server-remove', true)
    expect(invoke).toHaveBeenCalledWith('device_pairing_remove', { expectedHostId: 'server-remove', confirmed: true })
    complete({ status: 200, fields: {} })
    await expect(result).rejects.toThrow('已丢弃旧管理状态')
  })

  it('断开后的迟到管理状态不能复活旧会话', async () => {
    let complete!: (value: unknown) => void
    const pending = new Promise(resolve => { complete = resolve })
    const invoke = vi.fn(async (command: string) => command === 'device_config_status'
      ? { runtime: { mode: 'remote' }, device_config: { mode: 'remote', paired_host: { host_id: 'server-a' } } }
      : command === 'device_ssh_management_request' ? pending : { state: 'disconnected', disconnected: true, fields: {} })
    window.__TAURI_INTERNALS__ = { invoke: invoke as never }
    const result = nativeDeviceApi.remoteManagementRequest('management.status', {})
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    await nativeDeviceApi.disconnectSshHost()
    complete({ status: 200, fields: {} })
    await expect(result).rejects.toThrow('已丢弃旧管理状态')
  })
})

describe('nativeDeviceApi.enableOpenSshServer（本机 SSH 准备确认）', () => {
  it('把用户明确确认值发送给原生系统功能命令', async () => {
    const response = { job_id: 'system-feature-openssh-server', fields: { job_id: '固定 SSH 任务编号' } }
    const invoke = vi.fn().mockResolvedValue(response)
    window.__TAURI_INTERNALS__ = { invoke }

    await expect(nativeDeviceApi.enableOpenSshServer(true)).resolves.toEqual(response)
    expect(invoke).toHaveBeenCalledOnce()
    expect(invoke).toHaveBeenCalledWith('resource_system_feature_enable', { confirmed: true })
  })
})

describe('nativeDeviceApi.controlInboundDevice（入站连接控制）', () => {
  it('使用正式命令携带客户端身份、登记代次和确认值', async () => {
    const invoke = vi.fn().mockResolvedValue({device_id:'client-a', action:'disconnect', completed:true, fields:{}})
    window.__TAURI_INTERNALS__ = {invoke}
    await nativeDeviceApi.controlInboundDevice('host-a', 'client-a', 'a'.repeat(64), 'disconnect')
    expect(invoke).toHaveBeenCalledWith('device_inbound_control', {expectedHostId:'host-a', deviceId:'client-a',
      expectedRecordToken:'a'.repeat(64), action:'disconnect', confirmed:true})
  })
})

describe('nativeDeviceApi.remoteTtsAssetImport（远程 TTS 资产导入）', () => {
  it.each(['asset', 'source'] as const)('用 %s 用途调用固定原生命令并返回资产回执', async purpose => {
    const response = { asset: { asset_id: `remote-${purpose}`, duration_ms: 5230 } }
    const invoke = vi.fn().mockResolvedValue(response)
    window.__TAURI_INTERNALS__ = { invoke }

    await expect(nativeDeviceApi.remoteTtsAssetImport('aniflive', purpose)).resolves.toEqual(response)
    expect(invoke).toHaveBeenCalledOnce()
    expect(invoke).toHaveBeenCalledWith('device_ssh_tts_asset_import', {
      model_id: 'aniflive', purpose,
    })
  })

  it('source_and_asset 一次调用回传可裁剪原件和参考资产', async () => {
    const response = {
      asset: { asset_id: 'reference-asset', duration_ms: 5230 },
      source: { asset_id: 'original-source', duration_ms: 8230 },
    }
    const invoke = vi.fn().mockResolvedValue(response)
    window.__TAURI_INTERNALS__ = { invoke }

    await expect(nativeDeviceApi.remoteTtsAssetImport('aniflive', 'source_and_asset')).resolves.toEqual(response)
    expect(invoke).toHaveBeenCalledOnce()
    expect(invoke).toHaveBeenCalledWith('device_ssh_tts_asset_import', {
      model_id: 'aniflive', purpose: 'source_and_asset',
    })
  })

  it('保留原生文件选择器取消回执，不伪装成资产结果', async () => {
    const response = { cancelled: true as const, fields: { cancelled: '用户取消文件选择' } }
    const invoke = vi.fn().mockResolvedValue(response)
    window.__TAURI_INTERNALS__ = { invoke }

    await expect(nativeDeviceApi.remoteTtsAssetImport('aniflive', 'asset')).resolves.toEqual(response)
    expect(invoke).toHaveBeenCalledOnce()
  })

  it('缺少桌面原生调用器时不回退为浏览器 HTTP 上传', async () => {
    await expect(nativeDeviceApi.remoteTtsAssetImport('aniflive', 'asset'))
      .rejects.toThrow('此操作需要桌面窗口。')
  })
})

describe('nativeDeviceApi.remoteVoiceForwarder（远程语音回环转发）', () => {
  it.each(['start', 'status'] as const)('%s 只接受本机 loopback WebSocket 地址', async action => {
    const response = { state: 'running', url: 'ws://127.0.0.1:43127/random_token_123/ws', fields: { url: '临时回环地址' } }
    const invoke = vi.fn().mockResolvedValue(response)
    window.__TAURI_INTERNALS__ = { invoke }

    await expect(nativeDeviceApi.remoteVoiceForwarder(action)).resolves.toEqual(response)
    expect(invoke).toHaveBeenCalledWith('device_ssh_voice_forwarder', { action })
  })

  it('拒绝转发到非回环地址并校验停止回执', async () => {
    window.__TAURI_INTERNALS__ = { invoke: vi.fn().mockResolvedValue({
      state: 'running', url: 'ws://server.example/ws', fields: {},
    }) }
    await expect(nativeDeviceApi.remoteVoiceForwarder('start')).rejects.toThrow('本机回环 WebSocket 地址')

    window.__TAURI_INTERNALS__ = { invoke: vi.fn().mockResolvedValue({ state: 'stopped', fields: {} }) }
    await expect(nativeDeviceApi.remoteVoiceForwarder('stop')).resolves.toEqual({ state: 'stopped', fields: {} })
  })
})

describe('nativeDeviceApi.remoteDshConsole（远程原生网页）', () => {
  it('打开只返回本机来源和状态，不需要网页处理登录地址', async () => {
    const response = { state:'running', opened:true, origin:'http://127.0.0.1:43210', fields:{ state:'网页入口状态' } }
    const invoke = vi.fn().mockResolvedValue(response)
    window.__TAURI_INTERNALS__ = { invoke }
    await expect(nativeDeviceApi.remoteDshConsole('open')).resolves.toEqual(response)
    expect(invoke).toHaveBeenCalledWith('device_ssh_dsh_console', { action:'open' })
  })

  it.each(['http://server:43210', 'http://127.0.0.1:43210/?token=value', 'http://user@127.0.0.1:43210'])('拒绝不是原生回环来源的回执 %s', async origin => {
    window.__TAURI_INTERNALS__ = { invoke:vi.fn().mockResolvedValue({state:'running',opened:true,origin,fields:{}}) }
    await expect(nativeDeviceApi.remoteDshConsole('open')).rejects.toThrow()
  })

  it('打开操作不能把尚未打开浏览器的状态当成成功', async () => {
    window.__TAURI_INTERNALS__ = { invoke:vi.fn().mockResolvedValue({state:'running',opened:false,origin:'http://127.0.0.1:43210',fields:{}}) }
    await expect(nativeDeviceApi.remoteDshConsole('open')).rejects.toThrow('尚未完成')
  })
})

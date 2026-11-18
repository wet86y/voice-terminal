// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiBaseUrl, productApi } from './api'

afterEach(() => {
  Reflect.deleteProperty(window, '__VOICE_APPLICATION__')
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__')
  vi.unstubAllGlobals()
})

function expectRemoteRequest(args: Record<string, unknown> | undefined, operation: string, argumentsValue: Record<string, unknown>) {
  expect(args).toEqual({ operation, arguments: argumentsValue, request_id: expect.stringMatching(/^[0-9a-f]{32}$/) })
}

describe('Product API（产品接口）地址', () => {
  it('拒绝缺失的 Host 地址，不回退到本机默认端口', () => {
    expect(() => apiBaseUrl()).toThrow('应用宿主尚未提供语音服务接口地址')
  })

  it('配置缺失时业务请求在触碰网络之前失败', async () => {
    const fetchRequest = vi.fn()
    vi.stubGlobal('fetch', fetchRequest)
    await expect(productApi.snapshot()).rejects.toThrow('应用宿主尚未提供语音服务接口地址')
    expect(fetchRequest).not.toHaveBeenCalled()
  })

  it('只采用 Host 配置并移除末尾斜杠', () => {
    window.__VOICE_APPLICATION__ = {
      managed: true,
      origin: 'http://127.0.0.1:42123',
      voice_api_url: 'http://127.0.0.1:43876/',
      voice_ws_url: 'ws://127.0.0.1:43876/ws',
    }
    expect(apiBaseUrl()).toBe('http://127.0.0.1:43876')
  })

  it('远程配对模式将静态设置保存映射到固定服务端操作，不访问客户端本机 HTTP', async () => {
    const remoteResponse = (operation: string, body: Record<string, unknown>, taskGeneration: number, requestId: string) => ({
      request_id: requestId, target_host_id: 'server-a',
      session_kind: operation === 'management.status' ? 'management' : 'task',
      session_generation: operation === 'management.status' ? 5 : taskGeneration,
      ...(operation === 'management.status' ? {} : { connection_generation: taskGeneration }),
      operation, status: 200, body, fields: {},
    })
    const responseFor = (args: Record<string, unknown> | undefined, operation: string, body: Record<string, unknown>, taskGeneration = 12) =>
      remoteResponse(operation, body, taskGeneration, String(args?.request_id ?? ''))
    const invoke = vi.fn(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'device_config_status') {
        return { runtime: { mode: 'remote' }, device_config: { mode: 'remote', paired_host: { host_id: 'server-a' } }, fields: {} }
      }
      if (command === 'device_ssh_management_request') {
        if (args?.operation === 'management.status') {
          expectRemoteRequest(args, 'management.status', {})
          return responseFor(args, 'management.status', { core_state: 'ready' }, 5)
        }
        if (args?.operation === 'settings.silence.save') {
          expectRemoteRequest(args, 'settings.silence.save', { value: { seconds: 31 } })
          return responseFor(args, 'settings.silence.save', { settings: { silence: { seconds: 31 } }, message: '已保存' })
        }
        if (args?.operation === 'wake_task.update') {
          expectRemoteRequest(args, 'wake_task.update', { task_id: 'task-1', value: { text: '服务端任务' } })
          return responseFor(args, 'wake_task.update', { task_id: 'task-1', text: '服务端任务' })
        }
        if (args?.operation === 'wake_task.create') {
          expectRemoteRequest(args, 'wake_task.create', { value: {
            text: '远程定时任务', at: '2026-11-18T09:30:00+08:00',
          } })
          return responseFor(args, 'wake_task.create', { task_id: 'created-task', text: '远程定时任务', status: 'pending' })
        }
        if (args?.operation === 'product.compute_status.refresh') {
          expectRemoteRequest(args, 'product.compute_status.refresh', {})
          return responseFor(args, 'product.compute_status.refresh', { models: {} })
        }
        if (args?.operation === 'tts.voices.read') {
          expectRemoteRequest(args, 'tts.voices.read', { model_id: 'aniflive' })
          return responseFor(args, 'tts.voices.read', { voices: [] })
        }
        if (args?.operation === 'tts.voices.update') {
          expectRemoteRequest(args, 'tts.voices.update', {
            model_id: 'aniflive', voice_id: 'voice-1', value: { name: '测试音色', expected_revision: 2 },
          })
          return responseFor(args, 'tts.voices.update', { voice: { id: 'voice-1', name: '测试音色' } })
        }
        if (args?.operation === 'hotwords.import') {
          expectRemoteRequest(args, 'hotwords.import', { value: { text: 'hello' } })
          return responseFor(args, 'hotwords.import', { entries: [] })
        }
        if (args?.operation === 'speaker.enrollment.capture_stop') {
          expectRemoteRequest(args, 'speaker.enrollment.capture_stop', {
            enrollment_id: 'enrollment-1', value: { capture_id: 'capture-1', cancel: true },
          })
          return responseFor(args, 'speaker.enrollment.capture_stop', { stopped: true })
        }
        if (args?.operation === 'settings.persona.save') {
          return responseFor(args, 'settings.persona.save', { message: '旧代次回执' }, 13)
        }
        if (args?.operation === 'settings.speaker.save') {
          return { ...responseFor(args, 'settings.speaker.save', { message: '编号不匹配' }), request_id: 'f'.repeat(32) }
        }
        throw new Error(`Unexpected management operation: ${String(args?.operation)}`)
      }
      throw new Error(`Unexpected native command: ${command}`)
    })
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
    const localFetch = vi.fn()
    vi.stubGlobal('fetch', localFetch)
    const result = await productApi.saveSilence({ seconds: 31 })
    expect(result).toMatchObject({ settings: { silence: { seconds: 31 } } })
    await expect(productApi.updateWakeTask('task-1', { text: '服务端任务' }))
      .resolves.toMatchObject({ task_id: 'task-1', text: '服务端任务' })
    await expect(productApi.createWakeTask({ text: '远程定时任务', at: '2026-11-18T09:30:00+08:00' }))
      .resolves.toMatchObject({ task_id: 'created-task', text: '远程定时任务', status: 'pending' })
    await expect(productApi.computeStatus(true)).resolves.toMatchObject({ models: {} })
    await expect(productApi.ttsVoices('aniflive')).resolves.toEqual({ voices: [] })
    await expect(productApi.updateTtsVoice('aniflive', 'voice-1', { name: '测试音色' }, 2))
      .resolves.toMatchObject({ voice: { id: 'voice-1', name: '测试音色' } })
    const upload = new File(['audio'], 'sample.wav', { type: 'audio/wav' })
    await expect(productApi.uploadTtsAsset('aniflive', upload)).rejects.toMatchObject({
      status: 501, code: 'REMOTE_OPERATION_UNAVAILABLE',
    })
    await expect(productApi.importHotwords('hello')).resolves.toMatchObject({ entries: [] })
    await expect(productApi.stopSpeakerCapture('enrollment-1', 'capture-1', true))
      .resolves.toMatchObject({ stopped: true })
    await expect(productApi.savePersona({ name: '跨代草稿' }))
      .rejects.toThrow('服务端连接代次已切换')
    await expect(productApi.saveSpeaker({ enabled: true }))
      .rejects.toThrow('服务端连接代次已切换')
    await expect(productApi.scanAnifLiveImport('local', 'C:/client/models'))
      .rejects.toMatchObject({ status: 501, code: 'REMOTE_FILE_TRANSFER_REQUIRED' })
    expect(invoke.mock.calls.filter(([command]) => command === 'device_ssh_management_request')
      .map(([, args]) => args?.operation)).toEqual([
        'management.status', 'settings.silence.save', 'wake_task.update', 'wake_task.create',
        'product.compute_status.refresh', 'tts.voices.read', 'tts.voices.update', 'hotwords.import',
        'speaker.enrollment.capture_stop', 'settings.persona.save', 'settings.speaker.save',
      ])
    expect(localFetch).not.toHaveBeenCalled()
  })

  it('远程产品快照绑定已配对服务端并通过原生代次校验', async () => {
    window.__VOICE_APPLICATION__ = {
      managed: true, remote_mode: true, origin: 'http://127.0.0.1:42123',
      voice_api_url: 'http://127.0.0.1:43876', voice_ws_url: 'ws://127.0.0.1:43876/ws',
    }
    const operations: string[] = []
    const invoke = vi.fn(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'device_config_status') {
        return { runtime: { mode: 'remote' }, device_config: { mode: 'remote', paired_host: { host_id: 'snapshot-server' } }, fields: {} }
      }
      if (command === 'device_ssh_management_request') {
        const operation = String(args?.operation)
        operations.push(operation)
        const task = operation !== 'management.status'
        return {
          request_id: String(args?.request_id ?? ''), target_host_id: 'snapshot-server',
          session_kind: task ? 'task' : 'management', session_generation: task ? 12 : 5,
          ...(task ? { connection_generation: 12 } : {}), operation, status: 200,
          body: operation === 'management.status' ? { core_state: 'ready' } : { runtime: { can_start: true } }, fields: {},
        }
      }
      throw new Error(`Unexpected native command: ${command}`)
    })
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
    const localFetch = vi.fn()
    vi.stubGlobal('fetch', localFetch)

    await expect(productApi.snapshot()).resolves.toMatchObject({ runtime: { can_start: true } })
    expect(operations).toContain('product.snapshot')
    expect(localFetch).not.toHaveBeenCalled()
  })

  it('无法读取远程目标配置时失败关闭，不回退到客户端本机 HTTP', async () => {
    window.__VOICE_APPLICATION__ = {
      managed: true, remote_mode: true, origin: 'http://127.0.0.1:42123',
      voice_api_url: 'http://127.0.0.1:43876', voice_ws_url: 'ws://127.0.0.1:43876/ws',
    }
    const invoke = vi.fn(async () => { throw new Error('device config unavailable') })
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
    const localFetch = vi.fn()
    vi.stubGlobal('fetch', localFetch)

    await expect(productApi.snapshot()).rejects.toMatchObject({ code: 'REMOTE_TARGET_UNAVAILABLE' })
    expect(invoke).toHaveBeenCalledWith('device_config_status', undefined)
    expect(localFetch).not.toHaveBeenCalled()
  })

  it('服务端核心尚未就绪时在客户端边界拒绝业务写入', async () => {
    window.__VOICE_APPLICATION__ = {
      managed: true, remote_mode: true, business_ready: false,
      origin: 'http://127.0.0.1:42123',
      voice_api_url: 'http://127.0.0.1:43876', voice_ws_url: 'ws://127.0.0.1:43876/ws',
    }
    const invoke = vi.fn()
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
    const localFetch = vi.fn()
    vi.stubGlobal('fetch', localFetch)

    await expect(productApi.saveSilence({ seconds: 31 })).rejects.toMatchObject({
      status: 503, code: 'REMOTE_BUSINESS_NOT_READY',
    })
    expect(invoke).not.toHaveBeenCalled()
    expect(localFetch).not.toHaveBeenCalled()
  })

  it('远程模式下尚未登记固定操作的 DSH 控制台启动会失败关闭', async () => {
    window.__VOICE_APPLICATION__ = {
      managed: true, remote_mode: true, origin: 'http://127.0.0.1:42123',
      voice_api_url: 'http://127.0.0.1:43876', voice_ws_url: 'ws://127.0.0.1:43876/ws',
    }
    const invoke = vi.fn(async (command: string) => {
      if (command === 'device_config_status') {
        return { runtime: { mode: 'remote' }, device_config: { mode: 'remote', paired_host: { host_id: 'server-a' } }, fields: {} }
      }
      throw new Error(`Unexpected native command: ${command}`)
    })
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
    const localFetch = vi.fn()
    vi.stubGlobal('fetch', localFetch)

    await expect(productApi.launchDsh()).rejects.toMatchObject({
      status: 501, code: 'REMOTE_OPERATION_UNAVAILABLE',
    })
    expect(localFetch).not.toHaveBeenCalled()
    expect(invoke.mock.calls.map(([command]) => command)).toEqual(['device_config_status'])
  })

  it('远程 DSH 权限答复映射到固定操作，并校验参数与回执主机代次', async () => {
    window.__VOICE_APPLICATION__ = {
      managed: true, remote_mode: true, origin: 'http://127.0.0.1:42123',
      voice_api_url: 'http://127.0.0.1:43876', voice_ws_url: 'ws://127.0.0.1:43876/ws',
    }
    let wrongTarget = false
    const invoke = vi.fn(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'device_config_status') {
        return { runtime: { mode: 'remote' }, device_config: { mode: 'remote', paired_host: { host_id: 'dsh-server' } }, fields: {} }
      }
      if (command === 'device_ssh_management_request') {
        if (args?.operation === 'management.status') {
          return {
            request_id: String(args.request_id), target_host_id: 'dsh-server', session_kind: 'management',
            session_generation: 7, operation: 'management.status', status: 200, body: { core_state: 'ready' }, fields: {},
          }
        }
        if (args?.operation === 'dsh.approval.decide') {
          expect(args).toEqual({
            operation: 'dsh.approval.decide',
            arguments: {
              approval_id: 'approval-1',
              decision: expect.stringMatching(/^(allowed-once|allowed-until-silence|rejected)$/),
              run_seq: 3,
            },
            request_id: expect.stringMatching(/^[0-9a-f]{32}$/),
          })
          return {
            request_id: String(args.request_id), target_host_id: wrongTarget ? 'another-server' : 'dsh-server',
            session_kind: 'task', session_generation: 12, connection_generation: 12,
            operation: 'dsh.approval.decide', status: 200, body: { result: 'accepted' }, fields: {},
          }
        }
      }
      throw new Error(`Unexpected native command: ${command}`)
    })
    Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } })
    const localFetch = vi.fn()
    vi.stubGlobal('fetch', localFetch)

    await expect(productApi.decideDshApproval({
      approval_id: 'approval-1', decision: 'allowed-until-silence', run_seq: 3,
    })).resolves.toEqual({ result: 'accepted' })
    const operationCalls = invoke.mock.calls.filter(([command, args]) => command === 'device_ssh_management_request'
      && (args as Record<string, unknown> | undefined)?.operation === 'dsh.approval.decide').length
    await expect(productApi.decideDshApproval({
      approval_id: 'approval-1', decision: 'rejected', run_seq: -1,
    })).rejects.toMatchObject({ status: 400 })
    expect(invoke.mock.calls.filter(([command, args]) => command === 'device_ssh_management_request'
      && (args as Record<string, unknown> | undefined)?.operation === 'dsh.approval.decide')).toHaveLength(operationCalls)

    wrongTarget = true
    await expect(productApi.decideDshApproval({
      approval_id: 'approval-1', decision: 'rejected', run_seq: 3,
    })).rejects.toThrow('服务端连接代次已切换')
    expect(localFetch).not.toHaveBeenCalled()
  })

  it('远程模式不生成客户端音频 HTTP 地址', () => {
    window.__VOICE_APPLICATION__ = {
      managed: true, remote_mode: true, origin: 'http://127.0.0.1:42123',
      voice_api_url: 'http://127.0.0.1:43876', voice_ws_url: 'ws://127.0.0.1:43876/ws',
    }

    expect(() => productApi.ttsAssetAudioUrl('aniflive', 'asset-1')).toThrow('远程产品请求不能使用客户端语音服务地址')
  })
})

describe('主机配对接口回执', () => {
  it('静态页面的 HTML 响应不能当作配对 JSON，也不向用户回显页面内容', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<!doctype html><title>管理页</title>', {
      status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' },
    })))
    await expect(productApi.devicePairingStatus()).rejects.toThrow('主机配对接口尚未就绪')
  })

  it('配对操作收到损坏 JSON 时返回可理解的错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response('{broken', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })))
    await expect(productApi.openDevicePairing(120)).rejects.toThrow('主机配对接口返回了无效数据')
    await expect(productApi.closeDevicePairing()).rejects.toThrow('主机配对接口返回了无效数据')
  })
})

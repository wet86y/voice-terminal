// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { useApplicationHost, type ApplicationSnapshot, type DesktopRecovery } from './application-host'
import * as desktop from './desktop-port'
import { productApi } from './api'
import { nativeDeviceApi, type DeviceConfigStatus, type RemoteManagementOperation, type RemoteManagementResponse, type RemoteVoiceForwarderResult } from './native-device-api'
import { DEFAULT_REGISTRY, DEFAULT_SETTINGS } from './normalization'
import { useProductAdapter } from './useProductAdapter'

class FakeEventSource {
  /** instances（事件流实例列表）：用于断言不会重复建立状态连接。 */
  static instances: FakeEventSource[] = []
  /** onopen（事件流已连接回调）。 */
  onopen: ((event: Event) => void) | null = null
  /** onmessage（事件流消息回调）。 */
  onmessage: ((event: MessageEvent) => void) | null = null
  /** onerror（事件流错误回调）。 */
  onerror: ((event: Event) => void) | null = null
  /** close（关闭事件流）：Vitest（前端测试框架）监视器。 */
  close = vi.fn()
  /** url（事件流地址）。 */
  readonly url: string

  constructor(url: string) { this.url = url; FakeEventSource.instances.push(this) }
  open() { this.onopen?.(new Event('open')) }
}

class FakeWebSocket {
  /** instances（套接字实例列表）：用于断言不会继续建立业务连接。 */
  static instances: FakeWebSocket[] = []
  /** readyState（套接字状态）。 */
  readyState = 0
  /** onopen（套接字连接成功回调）。 */
  onopen: ((event: Event) => void) | null = null
  /** onmessage（套接字消息回调）。 */
  onmessage: ((event: MessageEvent) => void) | null = null
  /** onerror（套接字错误回调）。 */
  onerror: ((event: Event) => void) | null = null
  /** onclose（套接字关闭回调）。 */
  onclose: ((event: CloseEvent) => void) | null = null
  /** send（发送消息）：Vitest（前端测试框架）监视器。 */
  sent: string[] = []
  send = vi.fn((data: string) => { this.sent.push(data) })
  /** close（关闭套接字）：Vitest（前端测试框架）监视器。 */
  close = vi.fn(() => { this.readyState = 3; this.onclose?.(new CloseEvent('close')) })
  /** url（业务 WebSocket（网页套接字）地址）。 */
  readonly url: string

  constructor(url: string) { this.url = url; FakeWebSocket.instances.push(this) }
  open() { this.readyState = 1; this.onopen?.(new Event('open')) }
  push(type: string, seq: number, payload: Record<string, unknown> = {}, ids: Record<string, string> = {}) {
    this.onmessage?.({ data: JSON.stringify({ type, seq, timestamp: '2026-11-17T00:00:00.000Z', conversation_id: 'conversation_a', ...ids, payload }) } as MessageEvent)
  }
}

const readySnapshot: ApplicationSnapshot = {
  instance_id: 'session-test', revision: 1, state: 'ready', phase: '运行正常',
  started_at: 1, startup_complete: true, core_available: true, stopping: false,
  voice_api_url: 'http://127.0.0.1:43876', voice_ws_url: 'ws://127.0.0.1:43876/ws',
  capabilities: {}, sampled_at: 1, total_memory_rss_bytes: 1, completed_components: 1, total_components: 1,
  components: [{ id: 'voice', name: '语音服务', state: 'ready', phase: '运行正常', error: '', pid: 123,
    created_at: 1, uptime_seconds: 1, memory_rss_bytes: 1, memory_private_commit_bytes: 1,
    thread_count: 1, cpu_percent: 0, sampled_at: 1, shared_with: null,
    retry_allowed: false, retrying: false, external: false }],
}

const productSnapshot = {
  settings: structuredClone(DEFAULT_SETTINGS), models: structuredClone(DEFAULT_REGISTRY),
} as never

function remoteManagementResponse(
  operation: RemoteManagementOperation,
  body: Record<string, unknown>,
  managementGeneration = 5,
  hostId = 'server-a',
): RemoteManagementResponse {
  const managerStatus = operation === 'management.status'
  return {
    request_id: managerStatus ? 'a'.repeat(32) : 'b'.repeat(32),
    target_host_id: hostId,
    session_kind: managerStatus ? 'management' : 'task',
    session_generation: managerStatus ? managementGeneration : 12,
    ...(managerStatus ? {} : { connection_generation: 12 }),
    operation, status: 200, body, fields: {},
  }
}

function mockNativeInvoke(recovery: () => DesktopRecovery) {
  return vi.fn(async (command: string) => command === 'device_config_status'
    ? { runtime: { mode: 'local' }, device_config: null, fields: {} }
    : { runtime_recovery: recovery() })
}

let app: ReturnType<typeof createApp> | undefined
let host: ReturnType<typeof useApplicationHost>
let adapter: ReturnType<typeof useProductAdapter>

beforeEach(() => {
  vi.useFakeTimers()
  host = useApplicationHost()
  host.stop()
  host.snapshot.value = null
  host.error.value = ''
  host.sessionInvalid.value = false
  FakeEventSource.instances = []
  FakeWebSocket.instances = []
  window.__VOICE_APPLICATION__ = {
    managed: true, origin: 'http://127.0.0.1:42123',
    voice_api_url: '', voice_ws_url: '', business_ready: false,
  }
  vi.stubGlobal('EventSource', FakeEventSource)
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.spyOn(productApi, 'snapshot').mockResolvedValue(productSnapshot)
  vi.spyOn(productApi, 'speakerStatus').mockResolvedValue({})
  vi.spyOn(productApi, 'speakerProfile').mockResolvedValue({})
  vi.spyOn(productApi, 'computeStatus').mockResolvedValue({} as never)
  vi.spyOn(productApi, 'wakeTasks').mockResolvedValue({ tasks: [] } as never)

  app = createApp({
    setup() {
      host = useApplicationHost()
      adapter = useProductAdapter()
      return () => h('div')
    },
  })
  app.mount(document.createElement('div'))
})

afterEach(() => {
  host.stop()
  app?.unmount()
  app = undefined
  Reflect.deleteProperty(window, '__VOICE_APPLICATION__')
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__')
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Host 会话失效处理', () => {
  it('组件维护保留页面，丢弃旧鉴权失败，在新宿主加载期间重接并持续显示热切换', async () => {
    let deliver!: (event: { payload: DesktopRecovery }) => void
    const operation = { id: 'native-update', revision: 1, target: 'resources', label: '本地组件',
      state: 'running', phase: 'switching', detail: '正在启用组件' } as const
    window.__TAURI_INTERNALS__ = { invoke: mockNativeInvoke(() => ({ revision: 0, suspended: false, operation: null })) as unknown as NonNullable<Window['__TAURI_INTERNALS__']>['invoke'] }
    vi.spyOn(desktop, 'desktopPort').mockReturnValue({ available: true,
      listen: vi.fn(async (_name, handler) => { deliver = handler; return vi.fn() }) } as unknown as desktop.DesktopPort)
    const response = (value: unknown, status = 200) => ({ status, ok: status === 200, json: async () => value }) as Response
    let releaseOld!: (value: Response) => void
    const oldRequest = new Promise<Response>(resolve => { releaseOld = resolve })
    const restoring = { ...readySnapshot, instance_id: 'replacement', revision: 1, startup_complete: false,
      core_available: false, state: 'starting', voice_api_url: '', voice_ws_url: '' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response(readySnapshot))
      .mockReturnValueOnce(oldRequest).mockResolvedValue(response(restoring)))
    await host.start()
    FakeWebSocket.instances[0]!.open()
    const pending = host.refresh()
    // remote-mode probe（远程模式检查）现在先核对原生设备配置，再发出本地快照请求。
    await flushPromises()
    deliver({ payload: { revision: 1, suspended: true, operation } })
    expect(host.businessReady.value).toBe(false)
    expect(host.overlay.value).toBe(false)
    expect(FakeEventSource.instances[0]!.close).toHaveBeenCalledOnce()
    releaseOld(response({}, 403))
    await pending
    expect(host.sessionInvalid.value).toBe(false)
    deliver({ payload: { revision: 2, suspended: false, operation: { ...operation, revision: 2, phase: 'loading' } } })
    await flushPromises()
    expect(host.snapshot.value?.instance_id, host.error.value).toBe('replacement')
    expect(host.overlay.value).toBe(false)
    expect(host.runtimeOperations.value[0]?.state).toBe('running')
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(window.__VOICE_APPLICATION__?.origin).toBe('http://127.0.0.1:42123')
    // 迟到的旧恢复消息不能使新宿主再次暂停。
    deliver({ payload: { revision: 1, suspended: true, operation } })
    expect(host.recovery.value?.suspended).toBe(false)
    FakeEventSource.instances[1]!.onmessage?.({ data: JSON.stringify({ ...readySnapshot, instance_id: 'replacement', revision: 2 }) } as MessageEvent)
    expect(host.businessReady.value).toBe(true)
    deliver({ payload: { revision: 3, suspended: false, operation: { ...operation, revision: 3, state: 'succeeded', phase: 'ready' } } })
    expect(host.runtimeOperations.value[0]?.state).toBe('succeeded')
    expect(host.overlay.value).toBe(false)
  })

  it('原生补查恢复丢失的维护事件，恢复失败保留页面和原生重试入口', async () => {
    let nativeState: DesktopRecovery = { revision: 0, suspended: false, operation: null }
    const invoke = mockNativeInvoke(() => nativeState)
    window.__TAURI_INTERNALS__ = { invoke: invoke as unknown as NonNullable<Window['__TAURI_INTERNALS__']>['invoke'] }
    vi.spyOn(desktop, 'desktopPort').mockReturnValue({ available: true } as desktop.DesktopPort)
    const fetchMock = vi.fn().mockResolvedValue({ status: 200, ok: true, json: async () => readySnapshot })
    vi.stubGlobal('fetch', fetchMock)
    await host.start()
    nativeState = { revision: 1, suspended: true, operation: { id: 'failed-recovery', revision: 1, target: 'resources',
      label: '本地组件', state: 'failed', phase: 'failed', detail: '恢复失败' } }
    await vi.advanceTimersByTimeAsync(2000)
    const requests = fetchMock.mock.calls.length
    expect(host.recovery.value?.suspended).toBe(true)
    expect(host.overlay.value).toBe(false)
    expect(host.businessReady.value).toBe(false)
    await vi.advanceTimersByTimeAsync(6000)
    expect(fetchMock).toHaveBeenCalledTimes(requests)
    await host.recover()
    expect(invoke).toHaveBeenCalledWith('desktop_start_service')
  })

  it('整轮停机与恢复事件均丢失时，从已停止快照重新接入且不闪回启动画面', async () => {
    let nativeState: DesktopRecovery = { revision: 0, suspended: false, operation: null }
    window.__TAURI_INTERNALS__ = { invoke: mockNativeInvoke(() => nativeState) as unknown as NonNullable<Window['__TAURI_INTERNALS__']>['invoke'] }
    vi.spyOn(desktop, 'desktopPort').mockReturnValue({ available: true } as desktop.DesktopPort)
    const response = (value: unknown) => ({ status: 200, ok: true, json: async () => value }) as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response(readySnapshot))
      .mockResolvedValue(response({ ...readySnapshot, instance_id: 'restored-after-gap' })))
    await host.start()
    FakeEventSource.instances[0]!.onmessage?.({ data: JSON.stringify({ ...readySnapshot, revision: 2, state: 'stopped', stopping: true }) } as MessageEvent)
    expect(host.overlay.value).toBe(false)
    nativeState = { revision: 3, suspended: false, operation: { id: 'missed-update', revision: 3, target: 'resources',
      label: '本地组件', state: 'succeeded', phase: 'ready', detail: '已恢复' } }
    await vi.advanceTimersByTimeAsync(2000)
    expect(host.snapshot.value?.instance_id).toBe('restored-after-gap')
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(host.businessReady.value).toBe(true)
    expect(host.overlay.value).toBe(false)
  })

  it('重试确认重新读取状态，不复用操作之前已发出的失败快照请求', async () => {
    const failed = { ...readySnapshot, components: [...readySnapshot.components,
      { ...readySnapshot.components[0]!, id: 'speaker', state: 'failed', error: '旧故障', retry_allowed: true }],
    }
    let resolveOld!: (value: Response) => void
    const old = new Promise<Response>(resolve => { resolveOld = resolve })
    const retrying = { ...failed, revision: 2, components: [failed.components[0]!,
      { ...failed.components[1]!, retrying: true, phase: '等待恢复操作' }],
    }
    const response = (value: unknown) => ({ status: 200, ok: true, json: async () => value }) as Response
    const fetchMock = vi.fn().mockResolvedValueOnce(response(failed)).mockReturnValueOnce(old)
      .mockResolvedValueOnce(response({ ok: true })).mockResolvedValueOnce(response(retrying))
    vi.stubGlobal('fetch', fetchMock)
    await host.start(); await flushPromises()
    const beforeRetry = host.refresh()
    const requested = host.retry('speaker')
    await flushPromises()
    resolveOld(response(failed))
    await Promise.all([beforeRetry, requested]); await flushPromises()
    expect(fetchMock).toHaveBeenCalledTimes(4)
    expect(host.snapshot.value?.components.find(row => row.id === 'speaker')?.retrying).toBe(true)
    expect(host.snapshot.value?.revision).toBe(2)
  })
  it('ready 后遇到 403 终止 Host 重试并断开已有语音连接；5xx 保持可恢复', async () => {
    const responses = [
      { status: 200, ok: true, json: async () => readySnapshot },
      { status: 503, ok: false, json: async () => ({}) },
      { status: 403, ok: false, json: async () => ({}) },
    ]
    const fetchMock = vi.fn(async () => responses.shift() as Response)
    vi.stubGlobal('fetch', fetchMock)

    await host.start()
    await flushPromises()
    await nextTick()
    expect(host.businessReady.value).toBe(true)
    expect(window.__VOICE_APPLICATION__?.business_ready).toBe(true)
    expect(adapter.voice.connection.value).toBe('connecting')
    const stream = FakeEventSource.instances[0]
    const socket = FakeWebSocket.instances[0]
    expect(stream).toBeDefined()
    expect(socket).toBeDefined()
    stream.open()
    socket.open()
    await flushPromises()
    expect(adapter.voice.connection.value).toBe('connected')
    expect(productApi.snapshot).toHaveBeenCalled()
    const snapshotRequestCount = vi.mocked(productApi.snapshot).mock.calls.length

    stream.onerror?.(new Event('error'))
    await flushPromises()
    expect(host.sessionInvalid.value).toBe(false)
    expect(host.snapshot.value).toEqual(readySnapshot)
    expect(host.error.value).toContain('HTTP 503')
    expect(stream.close).toHaveBeenCalledOnce()
    expect(socket.close).not.toHaveBeenCalled()
    expect(window.__VOICE_APPLICATION__?.business_ready).toBe(true)

    await host.refresh()
    await flushPromises()
    expect(host.sessionInvalid.value).toBe(true)
    expect(host.snapshot.value).toBeNull()
    expect(host.businessReady.value).toBe(false)
    expect(host.error.value).toBe('会话已失效，请退出后重新启动。')
    expect(stream.close).toHaveBeenCalledOnce()
    expect(socket.close).toHaveBeenCalledOnce()
    expect(adapter.voice.connection.value).toBe('offline')
    expect(window.__VOICE_APPLICATION__?.origin).toBe('')
    expect(window.__VOICE_APPLICATION__?.voice_api_url).toBe('')
    expect(window.__VOICE_APPLICATION__?.voice_ws_url).toBe('')
    expect(window.__VOICE_APPLICATION__?.business_ready).toBe(false)

    const invoke = mockNativeInvoke(() => ({ revision: 0, suspended: false, operation: null }))
    window.__TAURI_INTERNALS__ = { invoke: invoke as unknown as NonNullable<Window['__TAURI_INTERNALS__']>['invoke'] }
    await host.exit()
    expect(invoke).toHaveBeenCalledOnce()
    expect(invoke).toHaveBeenCalledWith('exit_application')
    await expect(productApi.saveWake({ enabled: false, keywords: [], sensitivity: 0 })).rejects.toThrow('尚未允许业务请求')
    adapter.retry()
    adapter.voice.connect()
    await host.start()
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(productApi.snapshot).toHaveBeenCalledTimes(snapshotRequestCount)
  })
})

describe('远程 Host 管理代次', () => {
  const remoteDeviceStatus = {
    runtime: { config_state: 'valid', device_id: 'client-a', mode: 'remote', desired_role: 'remote_client', audio_mode: 'pcm', error: null },
    device_config: { mode: 'remote', device_id: 'client-a', paired_host: { host_id: 'server-a', display_name: '服务端 A' } },
  } as DeviceConfigStatus
  const remoteProductStatus = {
    runtime: { can_start: true, state: 'ready', services: { voice: 'READY' } },
    settings: structuredClone(DEFAULT_SETTINGS), models: structuredClone(DEFAULT_REGISTRY),
  }

  beforeEach(() => {
    window.__TAURI_INTERNALS__ = { invoke: vi.fn().mockResolvedValue({ runtime_recovery: { revision: 0, suspended: false, operation: null } }) }
    vi.spyOn(nativeDeviceApi, 'deviceConfigStatus').mockResolvedValue(remoteDeviceStatus)
    vi.spyOn(nativeDeviceApi, 'sshHostStatus').mockResolvedValue({ state: 'connected', connected: true, fields: {} })
    vi.spyOn(nativeDeviceApi, 'remoteTaskChannel').mockResolvedValue({ state: 'running', device_id: 'client-a',
      connection_generation: 12, executor_generation: 1, fields: {} })
  })

  it('只接受当前配对主机和同一管理代次的产品状态', async () => {
    const url = 'ws://127.0.0.1:43127/random_token_123/ws'
    const forwarder = vi.spyOn(nativeDeviceApi, 'remoteVoiceForwarder').mockImplementation(async (action): Promise<RemoteVoiceForwarderResult> => action === 'stop'
      ? { state: 'stopped', fields: {} }
      : { state: 'running', url, fields: { url: '本机回环地址' } })
    vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockImplementation(async operation => operation === 'management.status'
      ? remoteManagementResponse(operation, { core_state: 'ready' })
      : remoteManagementResponse(operation, remoteProductStatus))

    await host.refresh()

    expect(host.snapshot.value?.host_id).toBe('server-a')
    expect(host.snapshot.value?.endpoint_revision).toBe(12)
    expect(host.snapshot.value?.remote_business_ready).toBe(true)
    expect(host.snapshot.value?.voice_api_ready).toBe(true)
    expect(host.snapshot.value?.voice_ws_url).toBe(url)
    expect(forwarder).toHaveBeenCalledWith('start')
  })

  it('管理通道可用但语音核心仍启动时不开放业务写入', async () => {
    const forwarder = vi.spyOn(nativeDeviceApi, 'remoteVoiceForwarder').mockResolvedValue({
      state: 'stopped', fields: {},
    })
    const management = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue(
      remoteManagementResponse('management.status', { core_state: 'starting' }),
    )

    await host.refresh()

    expect(host.snapshot.value?.management_connected).toBe(true)
    expect(host.snapshot.value?.remote_business_ready).toBe(false)
    expect(host.snapshot.value?.voice_api_ready).toBe(false)
    expect(window.__VOICE_APPLICATION__?.business_ready).toBe(false)
    expect(management).toHaveBeenCalledOnce()
    expect(forwarder).not.toHaveBeenCalledWith('start')
    expect(host.corePhase.value).toBe('starting')
    expect(host.runtimeFailure.value).toBe(false)
    expect(host.snapshot.value?.unavailable_capabilities?.[0].state).toBe('STARTING')
  })

  it('服务端核心已就绪但任务通道仍建立时等待，不提前发产品请求或显示错误', async () => {
    vi.mocked(nativeDeviceApi.remoteTaskChannel).mockResolvedValue({ state: 'stopped', fields: {} })
    const manager = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue(
      remoteManagementResponse('management.status', { core_state: 'ready' }))
    const forwarder = vi.spyOn(nativeDeviceApi, 'remoteVoiceForwarder')
    await host.refresh()
    expect(host.snapshot.value?.remote_error).toBeNull()
    expect(host.corePhase.value).toBe('starting')
    expect(host.runtimeFailure.value).toBe(false)
    expect(manager).toHaveBeenCalledOnce()
    expect(forwarder).not.toHaveBeenCalledWith('start')
  })

  it('未配对和已配对但断线时只显示等待，不伪造组件失败', async () => {
    const status = vi.mocked(nativeDeviceApi.deviceConfigStatus)
    status.mockResolvedValueOnce({ ...remoteDeviceStatus, device_config: { ...remoteDeviceStatus.device_config!, paired_host: null } })
    vi.mocked(nativeDeviceApi.sshHostStatus).mockResolvedValue({ state: 'disconnected', connected: false, fields: {} })
    const manager = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest')
    await host.refresh()
    expect(host.snapshot.value?.remote_connection_state).toBe('unpaired')
    expect(host.snapshot.value?.host_name).toBe('尚未配对服务端')
    expect(host.snapshot.value?.unavailable_capabilities).toEqual([])
    expect(host.runtimeFailure.value).toBe(false)
    await host.refresh()
    expect(host.snapshot.value?.remote_connection_state).toBe('connecting')
    expect(host.runtimeFailure.value).toBe(false)
    expect(manager).not.toHaveBeenCalled()
  })

  it.each(['LISTENING', 'THINKING', 'SPEAKING'])('服务端 %s 期间不能再次开始监听不代表组件故障', async state => {
    vi.spyOn(nativeDeviceApi, 'remoteVoiceForwarder').mockResolvedValue({ state: 'running', url: 'ws://127.0.0.1:43127/random_token_123/ws', fields: {} })
    vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockImplementation(async operation => operation === 'management.status'
      ? remoteManagementResponse(operation, { core_state: 'ready' })
      : remoteManagementResponse(operation, { ...remoteProductStatus, runtime: { state, can_start: false,
        services: { audio: 'RUNNING', asr: 'STREAMING', llm: 'CONNECTED', tts: 'READY', kws: 'DISABLED' } } }))
    await host.refresh()
    expect(host.snapshot.value?.core_available).toBe(true)
    expect(host.runtimeFailure.value).toBe(false)
    expect(host.corePhase.value).toBe('ready')
  })

  it('管理和语音通道状态独立，实时产品投影与语音命令走远程 WebSocket', async () => {
    const url = 'ws://127.0.0.1:43127/random_token_123/ws'
    vi.spyOn(nativeDeviceApi, 'remoteVoiceForwarder').mockImplementation(async (action): Promise<RemoteVoiceForwarderResult> => action === 'stop'
      ? { state: 'stopped', fields: {} }
      : { state: 'running', url, fields: { url: '本机回环地址' } })
    const apiEntry = { seq: 1, kind: 'user_turn', conversation_id: 'api-episode', turn_id: 'api-turn',
      speech_id: '', text: '较旧的管理快照正文', status: 'completed', delivery: {}, termination: 'completed', at: '' }
    const liveSnapshot = {
      ...remoteProductStatus,
      conversation: { current: { turn_id: 'api-turn', partial_text: '旧的管理快照草稿', final_text: '', assistant_text: '' },
        entries: [apiEntry], recent: [] },
    }
    vi.spyOn(productApi, 'snapshot').mockResolvedValue(liveSnapshot as never)
    vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockImplementation(async operation => operation === 'management.status'
      ? remoteManagementResponse(operation, { core_state: 'ready' })
      : remoteManagementResponse(operation, liveSnapshot))

    await host.refresh()
    await flushPromises()
    const socket = FakeWebSocket.instances.at(-1)!
    expect(socket.url).toBe(url)
    socket.open()
    await flushPromises()

    expect(adapter.managementConnected.value).toBe(true)
    expect(adapter.voiceConnected.value).toBe(true)
    socket.push('runtime.snapshot', 1, {
      state: 'IDLE', listening: false, can_start: true,
      current: { turn_id: 'live-turn', partial_text: '实时语音草稿', final_text: '', assistant_text: '' },
    }, { turn_id: 'live-turn' })
    socket.push('conversation.snapshot', 2, { entries: [{ ...apiEntry, seq: 2, conversation_id: 'live-episode',
      turn_id: 'live-turn', text: '实时对话记录' }], latest_seq: 2 })
    socket.push('agent.state', 3, { conversation_id: 'live-episode', agent_busy: true, context_token_estimate: 12 })
    socket.push('speech.queued', 4, { speech_id: 'live-speech', conversation_id: 'live-episode', turn_id: 'live-turn',
      output_generation: 1, text: '' }, { turn_id: 'live-turn', speech_id: 'live-speech' })
    socket.push('speech.delta', 5, { speech_id: 'live-speech', conversation_id: 'live-episode', turn_id: 'live-turn',
      output_generation: 1, text_delta: '实时播报正文' }, { turn_id: 'live-turn', speech_id: 'live-speech' })

    expect(adapter.current.value.partial_text).toBe('实时语音草稿')
    expect(adapter.conversationRows.value.map(row => row.text)).toContain('实时对话记录')
    expect(adapter.voice.agentEpisode.value?.conversation_id).toBe('live-episode')
    expect(adapter.pendingSpeechRows.value[0]?.text).toBe('实时播报正文')

    expect(adapter.setListening(true)).toBe(true)
    socket.push('runtime.snapshot', 6, { state: 'LISTENING', listening: true, can_start: false,
      current: { turn_id: 'live-turn' } }, { turn_id: 'live-turn' })
    expect(adapter.setListening(false)).toBe(true)
    expect(adapter.setReasoningMode('deep')).toBe(true)
    socket.push('runtime.snapshot', 7, { state: 'IDLE', listening: false, can_start: true,
      session_operation: null, current: { turn_id: 'live-turn' } }, { turn_id: 'live-turn' })
    expect(adapter.resetSession()).toBe(true)
    const resetRequest = JSON.parse(socket.sent.filter(row => JSON.parse(row).command === 'session.reset').at(-1)!)
    socket.push('runtime.snapshot', 8, { session_operation: 'reset', can_start: false })
    socket.push('session.operation', 9, { request_id: resetRequest.request_id, operation: 'reset', status: 'completed' })
    socket.push('runtime.snapshot', 10, { session_operation: null, can_start: true, conversation: { entries: [], latest_seq: 0 } })
    expect(adapter.clearSession()).toBe(true)
    expect(socket.sent.map(row => JSON.parse(row).command)).toEqual([
      'subscribe', 'session.start', 'session.stop', 'session.reasoning_mode', 'session.reset', 'session.clear',
    ])
    expect(vi.mocked(nativeDeviceApi.remoteManagementRequest).mock.calls.some(([operation]) => operation === 'voice.command')).toBe(false)

    adapter.wakeForm.keywords = ['断线期间保留设置草稿']
    socket.close()
    expect(adapter.connected.value).toBe(true)
    expect(adapter.managementConnected.value).toBe(true)
    expect(adapter.voiceConnected.value).toBe(false)
    expect(adapter.voiceReady.value).toBe(false)
    expect(adapter.wakeForm.keywords).toEqual(['断线期间保留设置草稿'])
    expect(adapter.setListening(true)).toBe(false)
  })

  it('拒绝错误目标的管理状态，不请求产品快照', async () => {
    const request = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue(
      remoteManagementResponse('management.status', { core_state: 'ready' }, 5, 'server-b'),
    )

    await host.refresh()

    expect(request).toHaveBeenCalledOnce()
    expect(host.snapshot.value?.management_connected).toBe(false)
    expect(host.snapshot.value?.endpoint_revision).toBeUndefined()
    expect(host.snapshot.value?.remote_error).toContain('目标主机或连接代次无效')
  })

  it('拒绝错误目标的产品状态并关闭该代次的业务可用标记', async () => {
    const request = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockImplementation(async operation => operation === 'management.status'
      ? remoteManagementResponse(operation, { core_state: 'ready' }, 5)
      : remoteManagementResponse(operation, remoteProductStatus, 5, 'server-b'))

    await host.refresh()

    expect(request).toHaveBeenCalledTimes(2)
    expect(host.snapshot.value?.management_connected).toBe(true)
    expect(host.snapshot.value?.endpoint_revision).toBe(12)
    expect(host.snapshot.value?.remote_business_ready).toBe(false)
    expect(host.snapshot.value?.voice_api_ready).toBe(false)
    expect(host.snapshot.value?.remote_error).toContain('目标或通道代次')
  })
})

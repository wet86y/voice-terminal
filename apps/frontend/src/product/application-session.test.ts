// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { useApplicationHost, type ApplicationSnapshot } from './application-host'
import { productApi } from './api'
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
  send = vi.fn()
  /** close（关闭套接字）：Vitest（前端测试框架）监视器。 */
  close = vi.fn(() => { this.readyState = 3; this.onclose?.(new CloseEvent('close')) })
  /** url（业务 WebSocket（网页套接字）地址）。 */
  readonly url: string

  constructor(url: string) { this.url = url; FakeWebSocket.instances.push(this) }
  open() { this.readyState = 1; this.onopen?.(new Event('open')) }
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
  vi.spyOn(productApi, 'ambientContext').mockResolvedValue({})
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

    const invoke = vi.fn().mockResolvedValue(undefined)
    window.__TAURI_INTERNALS__ = { invoke }
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

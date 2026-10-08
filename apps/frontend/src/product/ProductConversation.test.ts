// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { h, nextTick, ref } from 'vue'
import Workbench from '../interface/Workbench.vue'
import { useProductControls } from './useProductControls'
import { productApi } from './api'
import { DEFAULT_REGISTRY, DEFAULT_SETTINGS } from './normalization'
import type { VoiceSocket } from '../composables/useVoiceTerminal'

const application = { // application（模拟宿主）：仅提供页面依赖，不启动本机业务。
  managed: ref(false), // managed（是否受宿主管理）。
  snapshot: ref(null), // snapshot（宿主状态快照）。
  businessReady: ref(false), // businessReady（是否允许自动连接）。
  overlay: ref(false), // overlay（启动遮罩）。
  error: ref(''), // error（宿主错误）。
  now: ref(0), // now（宿主显示时间）。
  retryingAll: ref(false), // retryingAll（批量重试状态）。
  connected: ref(false), // connected（宿主连接状态）。
  inspecting: ref(false), // inspecting（诊断页面状态）。
  start: vi.fn(), stop: vi.fn(), inspect: vi.fn(), refresh: vi.fn(), // 生命周期操作均不执行真实业务。
  exit: vi.fn(), retry: vi.fn(), retryFailed: vi.fn(), // 退出和重试操作均不执行真实业务。
}
vi.mock('./application-host', () => ({
  useApplicationHost: () => application,
  applicationStateLabel: (state: string) => state,
}))

class FakeSocket implements VoiceSocket {
  readyState = 0 // readyState（模拟连接状态）。
  sent: string[] = [] // sent（已发送的命令）。
  sequence = 0 // sequence（事件递增序号）。
  onopen: (() => void) | null = null // onopen（连接建立回调）。
  onmessage: ((event: { data: unknown }) => void) | null = null // onmessage（模拟事件回调）。
  onerror: (() => void) | null = null // onerror（连接错误回调）。
  onclose: (() => void) | null = null // onclose（连接关闭回调）。
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3; this.onclose?.() }
  open() { this.readyState = 1; this.onopen?.() }
  async push(type: string, payload: Record<string, unknown> = {}, turnId = 'input-a') {
    this.onmessage?.({ data: JSON.stringify({
      type, // type（事件类型）。
      seq: ++this.sequence, // seq（协议顺序号）。
      timestamp: '2026-10-08T10:00:00Z', // timestamp（模拟事件时间）。
      conversation_id: payload.conversation_id || 'episode-a', // conversation_id（所属会话）。
      turn_id: turnId, // turn_id（输入轮次）。
      speech_id: payload.speech_id, // speech_id（播报事务）。
      payload, // payload（原有协议的事件数据）。
    }) })
    await nextTick()
  }
}

const mounted: VueWrapper[] = [] // mounted（待释放的页面组件）。
let socket: FakeSocket // socket（当前模拟连接）。
let now = 0, nextFrame = 0 // now（动画时刻）；nextFrame（动画帧标识）。
let frames = new Map<number, FrameRequestCallback>() // frames（待执行的动画帧）。

beforeEach(() => {
  now = nextFrame = 0
  frames = new Map()
  vi.stubGlobal('WebSocket', class extends FakeSocket { constructor() { super(); socket = this } })
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frames.set(++nextFrame, callback); return nextFrame })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  window.__VOICE_APPLICATION__ = {
    managed: true, // managed（桌面管理标记）。
    origin: 'http://127.0.0.1:42123', // origin（模拟应用来源）。
    voice_api_url: 'http://127.0.0.1:43876', // voice_api_url（模拟业务接口）。
    voice_ws_url: 'ws://test.invalid/ws', // voice_ws_url（模拟事件连接）。
  }
  vi.spyOn(productApi, 'snapshot').mockResolvedValue({ settings: structuredClone(DEFAULT_SETTINGS), models: structuredClone(DEFAULT_REGISTRY) } as never)
  vi.spyOn(productApi, 'speakerStatus').mockResolvedValue({})
  vi.spyOn(productApi, 'speakerProfile').mockResolvedValue({})
  vi.spyOn(productApi, 'ambientContext').mockResolvedValue({})
  vi.spyOn(productApi, 'computeStatus').mockResolvedValue({} as never)
  vi.spyOn(productApi, 'wakeTasks').mockResolvedValue({ tasks: [] } as never)
  vi.spyOn(productApi, 'stopSpeakerCapture').mockResolvedValue({ stopped: true })
})
afterEach(() => {
  mounted.splice(0).forEach(wrapper => wrapper.unmount())
  Reflect.deleteProperty(window, '__VOICE_APPLICATION__')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
async function renderConversation() {
  let controls!: ReturnType<typeof useProductControls> // controls（真实前端投影链路）。
  const wrapper = mount({ setup() {
    controls = useProductControls()
    return () => h(Workbench, { model: controls.workbenchModel.value })
  } })
  mounted.push(wrapper)
  controls.voice.connect(); socket.open()
  await flushPromises()
  await socket.push('session.state', { state: 'LISTENING', listening: true })
  return { controls, wrapper }
}
async function advance(milliseconds: number) {
  while (milliseconds > 0) {
    const elapsed = Math.min(16, milliseconds)
    now += elapsed; milliseconds -= elapsed
    const callbacks = [...frames.values()]; frames.clear()
    callbacks.forEach(callback => callback(now))
    await nextTick()
  }
}
const visible = (message: ReturnType<VueWrapper['get']>) => message.get('.message-text-visible').text()
function userEntry(text: string, seq = 1, turnId = 'input-a') {
  return {
    seq, // seq（服务端会话顺序）。
    kind: 'user_turn', // kind（用户条目）。
    conversation_id: 'episode-a', // conversation_id（正式归属的会话）。
    turn_id: turnId, // turn_id（用户轮次标识）。
    text, // text（用户正文）。
    status: 'completed', // status（识别完成）。
  }
}
function speech(text: string, speechId = 'speech-a', turnId = 'input-a') {
  return {
    conversation_id: 'episode-a', // conversation_id（播报所属会话）。
    speech_id: speechId, // speech_id（独立播报标识）。
    turn_id: turnId, // turn_id（对应输入轮次）。
    text, // text（播报正文）。
  }
}

describe('会话气泡跨流式与结算阶段的连续显示', () => {
  it('识别中、最终稿与正式记录复用同一节点和逐字进度', async () => {
    const { wrapper } = await renderConversation()
    await socket.push('input.updated', { text: '请帮我检查这段代码' })
    await advance(80)
    const message = wrapper.get('.user-message')
    const element = message.element
    const prefix = visible(message)
    expect(prefix.length).toBeGreaterThan(0)
    await socket.push('input.finalized', { text: '请帮我检查这段代码。', source: 'paraformer' })
    expect(wrapper.get('.user-message').element).toBe(element)
    expect(visible(wrapper.get('.user-message'))).toBe(prefix)
    await socket.push('conversation.append', userEntry('请帮我检查这段代码。'))
    expect(wrapper.get('.user-message').element).toBe(element)
    expect(visible(wrapper.get('.user-message'))).toBe(prefix)
    await advance(150)
    expect(visible(wrapper.get('.user-message'))).toBe('请帮我检查这段代码。')
    expect(wrapper.findAll('.user-message')).toHaveLength(1)
  })
  it('播放队列、物理播放结束、播报结算与正式记录持续复用同一助手节点', async () => {
    const { wrapper } = await renderConversation()
    await socket.push('agent.state', { conversation_id: 'episode-a', agent_busy: true })
    await socket.push('speech.queued', speech(''))
    await socket.push('speech.delta', { ...speech(''), text_delta: '我会先检查消息更新路径' })
    await socket.push('speech.started', speech('我会先检查消息更新路径'))
    await advance(80)
    const element = wrapper.get('.assistant-message').element
    const prefix = visible(wrapper.get('.assistant-message'))
    await socket.push('speech.progress', { ...speech('我会先检查消息更新路径'), trigger: 'playback_finished', status: 'FINISHED' })
    expect(wrapper.get('.assistant-message').element).toBe(element)
    expect(visible(wrapper.get('.assistant-message'))).toBe(prefix)
    await socket.push('speech.completed', { ...speech('我会先检查消息更新路径'), status: 'completed' })
    expect(wrapper.get('.assistant-message').element).toBe(element)
    await socket.push('conversation.append', { ...speech('我会先检查消息更新路径'), seq: 2, kind: 'assistant_speech', status: 'completed' })
    expect(wrapper.get('.assistant-message').element).toBe(element)
    expect(visible(wrapper.get('.assistant-message'))).toBe(prefix)
    await advance(150)
    expect(visible(wrapper.get('.assistant-message'))).toBe('我会先检查消息更新路径')
    expect(wrapper.findAll('.assistant-message')).toHaveLength(1)
  })
  it('正式记录先到时同样复用助手节点，并按服务端顺序移动既有节点', async () => {
    const { wrapper } = await renderConversation()
    await socket.push('input.updated', { text: '用户问题' })
    await socket.push('speech.delta', { ...speech(''), text_delta: '助手回复' })
    await advance(200)
    const user = wrapper.get('.user-message').element
    const assistant = wrapper.get('.assistant-message').element
    await socket.push('input.finalized', { text: '用户问题' })
    await socket.push('conversation.append', userEntry('用户问题'))
    await socket.push('conversation.append', { ...speech('助手回复'), seq: 2, kind: 'assistant_speech', status: 'completed' })
    await socket.push('speech.completed', { ...speech('助手回复'), status: 'completed' })
    expect(wrapper.findAll('.message').map(item => item.element)).toEqual([user, assistant])
    expect(visible(wrapper.get('.user-message'))).toBe('用户问题')
    expect(visible(wrapper.get('.assistant-message'))).toBe('助手回复')
  })
  it('同轮助手记录不会提前隐藏尚未入账的最终用户文本', async () => {
    const { wrapper } = await renderConversation()
    await socket.push('input.finalized', { text: '仍需显示的用户问题' })
    await socket.push('conversation.append', { ...speech('先到达的助手记录'), seq: 1, kind: 'assistant_speech', status: 'completed' })
    expect(wrapper.findAll('.user-message')).toHaveLength(1)
    expect(wrapper.get('.user-message .message-text-source').text()).toBe('仍需显示的用户问题')
  })
  it('同轮继续输入从识别稿切到最终稿时不退回旧记录，后续权威修订仍能接管正文', async () => {
    const { wrapper } = await renderConversation()
    await socket.push('input.finalized', { text: '第一句' })
    await socket.push('conversation.append', userEntry('第一句'))
    await advance(100)
    const element = wrapper.get('.user-message').element
    await socket.push('input.started')
    await socket.push('input.updated', { text: '第一句，以及继续输入的内容' })
    await advance(280)
    await socket.push('input.finalized', { text: '第一句，以及继续输入的内容。' })
    expect(wrapper.get('.user-message').element).toBe(element)
    expect(wrapper.get('.user-message .message-text-source').text()).toBe('第一句，以及继续输入的内容。')
    expect(visible(wrapper.get('.user-message'))).toBe('第一句，以及继续输入的内容')
    await socket.push('conversation.append', userEntry('第一句，以及继续输入的内容。'))
    await advance(32)
    expect(visible(wrapper.get('.user-message'))).toBe('第一句，以及继续输入的内容。')
    await socket.push('conversation.append', userEntry('正式修订的权威正文'))
    expect(wrapper.get('.user-message .message-text-source').text()).toBe('正式修订的权威正文')
  })
  it('下一轮输入有独立气泡，迟到结算不覆盖当前识别稿，重置清理全部条目', async () => {
    const { controls, wrapper } = await renderConversation()
    await socket.push('input.finalized', { text: '第一轮' })
    await socket.push('conversation.append', userEntry('第一轮'))
    await advance(100)
    const first = wrapper.get('.user-message').element
    await socket.push('input.started', {}, 'input-b')
    await socket.push('input.updated', { text: '第二轮正在识别' }, 'input-b')
    const second = wrapper.findAll('.user-message')[1].element
    expect(second).not.toBe(first)
    await socket.push('conversation.append', { ...userEntry('第一轮'), termination: 'completed' })
    expect(wrapper.findAll('.user-message').map(item => item.element)).toEqual([first, second])
    await socket.push('input.completed', { transcript: '第一轮' })
    expect(wrapper.findAll('.user-message')[1].get('.message-text-source').text()).toBe('第二轮正在识别')
    await socket.push('session.state', { state: 'IDLE', listening: false }, 'input-b')
    expect(controls.voice.resetSession()).toBe(true)
    expect(wrapper.findAll('.user-message')).toHaveLength(2)
    const request = JSON.parse(socket.sent.at(-1)!)
    await socket.push('runtime.snapshot', {
      state: 'IDLE', listening: false, can_start: true, session_operation: null,
      current: { turn_id: null },
      conversation: { entries: [], latest_seq: 0 },
    }, '')
    await socket.push('session.operation', { request_id: request.request_id, status: 'completed' }, '')
    await advance(100)
    expect(wrapper.findAll('.message')).toHaveLength(0)
    expect(frames.size).toBe(0)
  })
  it('替换事务保持独立身份，结束文字在正式记录到达前仍可见', async () => {
    const { wrapper } = await renderConversation()
    await socket.push('speech.started', speech('被替换的回复'))
    await advance(100)
    const first = wrapper.get('.assistant-message').element
    await socket.push('speech.progress', { ...speech('被替换的回复'), event: 'speech.replaced', old_speech_id: 'speech-a' })
    await socket.push('speech.queued', speech('新的回复', 'speech-b'))
    expect(wrapper.findAll('.assistant-message')).toHaveLength(2)
    expect(wrapper.findAll('.assistant-message')[0].element).toBe(first)
    expect(wrapper.findAll('.assistant-message')[1].element).not.toBe(first)
    expect(wrapper.findAll('.assistant-message')[0].get('.message-status').text()).toBe('已被新内容替换')
  })
  it('状态结算和后台协作变化不额外滚动正文，用户上翻时新增文字不拉回底部', async () => {
    const { controls, wrapper } = await renderConversation()
    await socket.push('conversation.append', userEntry('已完成的正文'))
    await advance(180)
    const scroll = wrapper.get('.conversation-scroll').element as HTMLElement
    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(scroll, 'clientHeight', { configurable: true, value: 200 })
    scroll.scrollTop = 800
    const setter = vi.spyOn(scroll, 'scrollTop', 'set')
    await socket.push('conversation.append', { ...userEntry('已完成的正文'), termination: 'completed' })
    controls.voice.shadowProduct.value = { ...controls.voice.shadowProduct.value, focus: '后台更新' }
    await nextTick(); await advance(32)
    expect(setter).not.toHaveBeenCalled()
    scroll.scrollTop = 100
    await wrapper.get('.conversation-scroll').trigger('scroll')
    setter.mockClear()
    await socket.push('input.started', {}, 'input-b')
    await socket.push('input.updated', { text: '新增的用户文字' }, 'input-b')
    await advance(280)
    expect(scroll.scrollTop).toBe(100)
    expect(setter).not.toHaveBeenCalled()
  })
  it('旧会话的迟到输出不恢复到当前临时列表', async () => {
    const { wrapper } = await renderConversation()
    await socket.push('agent.state', { conversation_id: 'episode-b', agent_busy: false })
    await socket.push('speech.completed', { ...speech('旧会话迟到的正文'), status: 'completed' })
    expect(wrapper.findAll('.assistant-message')).toHaveLength(0)
  })
  it('正式历史清空先于运行快照到达时，不从结束缓存恢复已经入账的助手文字', async () => {
    const { wrapper } = await renderConversation()
    await socket.push('input.finalized', { text: '已入账的用户问题' })
    await socket.push('conversation.append', userEntry('已入账的用户问题'))
    await socket.push('speech.delta', { ...speech(''), text_delta: '已入账的助手回复' })
    await socket.push('speech.started', speech('已入账的助手回复'))
    await socket.push('speech.completed', { ...speech('已入账的助手回复'), status: 'completed' })
    await socket.push('conversation.append', { ...speech('已入账的助手回复'), seq: 2, kind: 'assistant_speech', status: 'completed' })
    await advance(180)
    expect(wrapper.findAll('.assistant-message')).toHaveLength(1)
    await socket.push('conversation.snapshot', { entries: [], latest_seq: 0 })
    expect(wrapper.findAll('.message')).toHaveLength(0)
  })
  it('观察气泡内容高度，同一帧的多次尺寸变化只滚动一次，卸载后释放待执行帧', async () => {
    let resize!: ResizeObserverCallback // resize（模拟尺寸变化通知）。
    const observed: Element[] = [] // observed（实际被观察的容器）。
    const disconnect = vi.fn() // disconnect（尺寸观察释放检查）。
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) { resize = callback }
      observe(element: Element) { observed.push(element) }
      disconnect = disconnect
    })
    const { wrapper } = await renderConversation()
    const scroll = wrapper.get('.conversation-scroll').element as HTMLElement
    const stream = wrapper.get('.conversation-stream').element
    expect(observed).toContain(stream)
    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(scroll, 'clientHeight', { configurable: true, value: 200 })
    const setter = vi.spyOn(scroll, 'scrollTop', 'set')
    resize([{ target: stream } as ResizeObserverEntry], {} as ResizeObserver)
    resize([{ target: stream } as ResizeObserverEntry], {} as ResizeObserver)
    expect(frames.size).toBe(1)
    await advance(16)
    expect(scroll.scrollTop).toBe(800)
    expect(setter).toHaveBeenCalledTimes(1)
    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 1200 })
    resize([{ target: stream } as ResizeObserverEntry], {} as ResizeObserver)
    expect(frames.size).toBe(1)
    wrapper.unmount()
    mounted.splice(mounted.indexOf(wrapper), 1)
    expect(frames.size).toBe(0)
    expect(disconnect).toHaveBeenCalledTimes(1)
  })
})

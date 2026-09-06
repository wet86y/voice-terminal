import { describe, expect, it } from 'vitest'
import { useVoiceTerminal, type VoiceSocket } from './useVoiceTerminal'
import type { CommandMessage, LatencyValues, MessageEnvelope } from '../types'

class FakeSocket implements VoiceSocket {
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null

  send(data: string) {
    this.sent.push(data)
  }

  close() {
    this.readyState = 3
    this.onclose?.()
  }

  open() {
    this.readyState = 1
    this.onopen?.()
  }

  push(message: MessageEnvelope<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(message) })
  }

  fail() {
    this.onerror?.()
  }
}

function envelope<T>(
  type: string,
  seq: number,
  turnId: string | null,
  payload: T,
): MessageEnvelope<string, T> {
  return {
    type,
    seq,
    timestamp: `2026-09-04T08:30:${String(seq).padStart(2, '0')}.000Z`,
    turn_id: turnId,
    payload,
  }
}

function createLatency(overrides: Partial<LatencyValues> = {}): LatencyValues {
  return {
    wake_detection_ms: 120,
    asr_first_text_ms: 180,
    speech_end_confirmation_ms: 700,
    llm_first_token_ms: 320,
    llm_request_to_first_tts_text_ms: 410,
    tts_text_to_first_audio_ms: 210,
    first_audio_to_playback_ms: 60,
    speech_end_to_playback_ms: 1270,
    turn_total_ms: 5300,
    ...overrides,
  }
}

function openTerminal() {
  const socket = new FakeSocket()
  const terminal = useVoiceTerminal({
    webSocketUrl: 'ws://test.local/ws',
    webSocketFactory: () => socket,
  })
  terminal.connect()
  socket.open()
  return { socket, terminal }
}

describe('useVoiceTerminal', () => {
  it('恢复 v0.2 快照和历史，忽略旧 generation 的回答', () => {
    const { socket, terminal } = openTerminal()
    socket.push(envelope('runtime.snapshot', 1, null, {
      state: 'THINKING', listening: true, can_start: false,
      services: { analysis: 'READY' },
      current: { turn_id: 't1', v02: true, generation_id: 3, input_revision: 4,
        final_text: '最新校稿', assistant_text: '', prediction: { decision: 'READY', intent: '读取文档' } },
      recent_turns: [{ turn_id: 'old', transcript: '旧问题', response: '旧回答', completed_at: '', latency: {} }],
    }))
    expect(terminal.currentTurnId.value).toBe('t1')
    expect(terminal.recentTurns.value).toHaveLength(1)
    expect(terminal.current.prediction?.intent).toBe('读取文档')
    socket.push(envelope('assistant.delta', 2, 't1', { generation_id: 2, text_delta: '过时回答' }))
    expect(terminal.current.assistant_text).toBe('')
    socket.push(envelope('assistant.delta', 3, 't1', { generation_id: 3, text_delta: '当前回答' }))
    expect(terminal.current.assistant_text).toBe('当前回答')
  })

  it('同轮续说清理正文并接受新快照，旧工具状态不能覆盖', () => {
    const { socket, terminal } = openTerminal()
    socket.push(envelope('turn.started', 1, 't2', { generation_id: 0, workspace: { generation_id: 0 } }))
    socket.push(envelope('assistant.delta', 2, 't2', { generation_id: 1, text_delta: '撤销内容' }))
    socket.push(envelope('turn.resumed', 3, 't2', {
      generation_id: 2, workspace: { generation_id: 2, input_revision: 5, tools: [] },
    }))
    socket.push(envelope('agent.tool', 4, 't2', {
      generation_id: 1, workspace: { tools: [{ name: 'files_search', status: 'DONE' }] },
    }))
    expect(terminal.current.assistant_text).toBe('')
    expect(terminal.current.tools).toEqual([])
    expect(terminal.currentTurnId.value).toBe('t2')
  })

  it('自动订阅，并且只通过 session 命令控制后端监听', () => {
    const { socket, terminal } = openTerminal()

    expect(terminal.connection.value).toBe('connected')
    expect(JSON.parse(socket.sent[0]) as CommandMessage).toMatchObject({
      type: 'command',
      command: 'subscribe',
      payload: { client: 'voice-terminal-web', version: '0.1.0' },
    })

    socket.push(envelope('runtime.snapshot', 1, null, {
      state: 'IDLE',
      listening: false,
      can_start: true,
      services: { audio: 'READY', kws: 'RUNNING', asr: 'READY', llm: 'READY', tts: 'CONNECTED' },
      current: {
        partial_text: '',
        final_text: '',
        assistant_text: '',
        tts_text: '',
        tts_status: 'IDLE',
        audio_buffer_ms: 0,
      },
    }))

    expect(terminal.canStart.value).toBe(true)
    expect(terminal.toggleListening()).toBe(true)
    expect(JSON.parse(socket.sent[1]) as CommandMessage).toMatchObject({
      command: 'session.start',
      payload: {},
    })
    expect(terminal.listening.value).toBe(false)

    socket.push(envelope('session.state', 2, 'turn_000021', {
      state: 'LISTENING',
      listening: true,
      can_start: false,
    }))
    expect(terminal.listening.value).toBe(true)
    expect(terminal.state.value).toBe('LISTENING')
    expect(terminal.currentTurnId.value).toBe('turn_000021')

    expect(terminal.toggleListening()).toBe(true)
    expect(JSON.parse(socket.sent[2]) as CommandMessage).toMatchObject({ command: 'session.stop' })
  })

  it('只接受新序号和当前 turn 的文本与 TTS 事件', () => {
    const { socket, terminal } = openTerminal()

    socket.push(envelope('session.state', 1, 'turn_000001', {
      state: 'LISTENING',
      listening: true,
      can_start: false,
    }))
    socket.push(envelope('transcript.partial', 2, 'turn_000001', { text: '帮我看看' }))
    socket.push(envelope('transcript.partial', 1, 'turn_000001', { text: '旧消息' }))
    socket.push(envelope('transcript.final', 3, 'turn_000001', { text: '帮我看看天气' }))
    socket.push(envelope('assistant.delta', 4, 'turn_000001', { text_delta: '今天' }))
    socket.push(envelope('assistant.delta', 5, 'turn_000001', { text_delta: '晴朗。' }))
    socket.push(envelope('assistant.delta', 6, 'turn_999999', { text_delta: '旧回答' }))
    socket.push(envelope('tts.status', 7, 'turn_000001', {
      provider: 'minimax',
      status: 'STREAMING',
      waiting_text: '今天晴朗。',
      audio_buffer_ms: 64,
    }))
    socket.push(envelope('tts.progress', 8, 'turn_000001', {
      provider: 'minimax',
      status: 'WAITING_PLAYBACK',
      waiting_text: '下一句。',
      audio_buffer_ms: 1900,
      audio_returned_ms: 6120,
      audio_played_ms: 4220,
      audio_remaining_ms: 1900,
    }))

    expect(terminal.current.partial_text).toBe('')
    expect(terminal.current.final_text).toBe('帮我看看天气')
    expect(terminal.current.assistant_text).toBe('今天晴朗。')
    expect(terminal.current.tts_text).toBe('下一句。')
    expect(terminal.current.tts_status).toBe('WAITING_PLAYBACK')
    expect(terminal.current.audio_buffer_ms).toBe(1900)
    expect(terminal.current.tts_returned_ms).toBe(6120)
    expect(terminal.current.tts_played_ms).toBe(4220)
    expect(terminal.current.tts_remaining_ms).toBe(1900)
    expect(terminal.lastSequence.value).toBe(8)
  })

  it('展示后端实际发送给 LLM 的完整上下文', () => {
    const { socket, terminal } = openTerminal()
    const turnId = 'turn_context_000001'

    socket.push(envelope('session.state', 1, turnId, {
      state: 'THINKING',
      listening: true,
      can_start: false,
    }))
    socket.push(envelope('llm.context', 2, turnId, {
      message_count: 4,
      messages: [
        { role: 'system', content: '你是中文助手。' },
        { role: 'user', content: '第一句话' },
        { role: 'assistant', content: '第一句回答' },
        { role: 'user', content: '第二句话' },
      ],
    }))

    expect(terminal.llmContextTurnId.value).toBe(turnId)
    expect(terminal.llmContext.value).toEqual([
      { role: 'system', content: '你是中文助手。' },
      { role: 'user', content: '第一句话' },
      { role: 'assistant', content: '第一句回答' },
      { role: 'user', content: '第二句话' },
    ])
  })

  it('原样保存后端 latency，并把完成轮次限制为最近 10 轮', () => {
    const { socket, terminal } = openTerminal()
    const firstTurn = 'turn_000001'

    socket.push(envelope('session.state', 1, firstTurn, {
      state: 'THINKING',
      listening: false,
      can_start: false,
    }))
    socket.push(envelope('latency.snapshot', 2, firstTurn, {
      complete: false,
      values: { asr_first_text_ms: 181, speech_end_to_playback_ms: null },
    }))

    expect(terminal.currentLatency.asr_first_text_ms).toBe(181)
    expect(terminal.currentLatency.speech_end_to_playback_ms).toBe(null)
    expect(terminal.currentLatencyComplete.value).toBe(false)

    for (let index = 1; index <= 11; index += 1) {
      const turnId = `turn_${String(index).padStart(6, '0')}`
      socket.push(envelope('turn.completed', index + 2, turnId, {
        transcript: `问题 ${index}`,
        response: `回答 ${index}`,
        completed_at: `2026-09-04T08:31:${String(index).padStart(2, '0')}.000Z`,
        latency: createLatency({ turn_total_ms: index * 100 }),
      }))
    }

    expect(terminal.recentTurns.value).toHaveLength(10)
    expect(terminal.recentTurns.value[0].turn_id).toBe('turn_000011')
    expect(terminal.recentTurns.value.at(-1)?.turn_id).toBe('turn_000002')
    expect(terminal.currentLatency.turn_total_ms).toBe(100)
  })

  it('保存中断轮次的传达范围，并处理即时 turn.interrupted 状态', () => {
    const { socket, terminal } = openTerminal()
    const turnId = 'turn_interrupted_000001'
    const delivery = {
      status: 'partial', termination: 'interrupted',
      heard_text: '前半句。', uncertain_text: '正在说的词', unheard_text: '后半句。',
      played_ms: 912, precision: 'provider',
    }

    socket.push(envelope('session.state', 1, turnId, {
      state: 'SPEAKING', listening: true, can_start: false,
    }))
    socket.push(envelope('assistant.delta', 2, turnId, { text_delta: '前半句。正在说的词后半句。' }))
    socket.push(envelope('turn.interrupted', 3, turnId, {
      response: '前半句。正在说的词后半句。', delivery,
    }))
    expect(terminal.state.value).toBe('INTERRUPTING')
    expect(terminal.current.delivery).toEqual(delivery)

    socket.push(envelope('turn.completed', 4, turnId, {
      transcript: '查询资料', response: '前半句。正在说的词后半句。',
      completed_at: '2026-09-04T08:32:00.000Z', latency: createLatency(),
      status: 'interrupted', delivery,
    }))
    expect(terminal.recentTurns.value[0].status).toBe('interrupted')
    expect(terminal.recentTurns.value[0].delivery?.heard_text).toBe('前半句。')
  })

  it('后端错误和连接关闭时禁用开始按钮，不生成本地替代数据', () => {
    const { socket, terminal } = openTerminal()

    socket.push(envelope('runtime.snapshot', 1, null, {
      state: 'IDLE',
      listening: false,
      can_start: true,
      services: { audio: 'READY', kws: 'RUNNING', asr: 'READY', llm: 'READY', tts: 'CONNECTED' },
      current: {
        partial_text: '',
        final_text: '',
        assistant_text: '',
        tts_text: '',
        tts_status: '',
        audio_buffer_ms: null,
      },
    }))
    socket.push(envelope('error', 2, null, {
      code: 'ASR_WORKER_UNAVAILABLE',
      message: 'ASR Worker 当前不可用',
      recoverable: false,
    }))

    expect(terminal.connectionError.value).toBe('ASR Worker 当前不可用')
    expect(terminal.currentLatency.asr_first_text_ms).toBe(null)

    socket.fail()
    socket.close()
    expect(terminal.connection.value).toBe('offline')
    expect(terminal.canStart.value).toBe(false)
    expect(terminal.services.asr).toBe('OFFLINE')
  })

  it('重连时清理旧轮状态，并按空当前轮快照恢复', () => {
    const { socket, terminal } = openTerminal()

    socket.push(envelope('session.state', 1, 'old-turn', {
      state: 'THINKING', listening: true, can_start: false,
    }))
    socket.push(envelope('transcript.final', 2, 'old-turn', { text: '旧问题' }))
    socket.push(envelope('assistant.delta', 3, 'old-turn', { text_delta: '旧回答' }))
    expect(terminal.current.final_text).toBe('旧问题')
    expect(terminal.current.assistant_text).toBe('旧回答')

    socket.close()
    expect(terminal.currentTurnId.value).toBe(null)
    expect(terminal.current.final_text).toBe('')
    expect(terminal.current.assistant_text).toBe('')

    terminal.connect()
    socket.open()
    socket.push(envelope('runtime.snapshot', 1, null, {
      state: 'IDLE', listening: false, can_start: true,
      services: { audio: 'READY', kws: 'DISABLED', asr: 'READY', llm: 'READY', tts: 'READY' },
      current: { turn_id: null, partial_text: '', final_text: '', assistant_text: '', tts_text: '', tts_status: 'IDLE', audio_buffer_ms: 0 },
      recent_turns: [],
    }))

    expect(terminal.currentTurnId.value).toBe(null)
    expect(terminal.current.final_text).toBe('')
    expect(terminal.current.assistant_text).toBe('')
    expect(terminal.canStart.value).toBe(true)
  })

  it('拒绝非整数序号和非对象 payload 的协议消息', () => {
    const { socket, terminal } = openTerminal()

    socket.onmessage?.({ data: JSON.stringify({
      type: 'runtime.snapshot', seq: 1.5, timestamp: '2026-09-04T08:30:00.000Z',
      turn_id: null, payload: {},
    }) })
    expect(terminal.connectionError.value).toBe('收到无法解析的后端消息。')

    socket.onmessage?.({ data: JSON.stringify({
      type: 'runtime.snapshot', seq: 1, timestamp: '2026-09-04T08:30:00.000Z',
      turn_id: null, payload: [],
    }) })
    expect(terminal.connectionError.value).toBe('收到无法解析的后端消息。')
  })
})

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

    expect(terminal.current.partial_text).toBe('')
    expect(terminal.current.final_text).toBe('帮我看看天气')
    expect(terminal.current.assistant_text).toBe('今天晴朗。')
    expect(terminal.current.tts_text).toBe('今天晴朗。')
    expect(terminal.current.tts_status).toBe('STREAMING')
    expect(terminal.current.audio_buffer_ms).toBe(64)
    expect(terminal.lastSequence.value).toBe(7)
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
})

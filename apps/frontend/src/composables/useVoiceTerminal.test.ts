import { describe, expect, it, vi } from 'vitest'
import { useVoiceTerminal, type VoiceSocket } from './useVoiceTerminal'
import { voiceShadowReplayFixture } from '../ui-demo/voice-shadow-replay-data'

class FakeSocket implements VoiceSocket {
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3; this.onclose?.() }
  open() { this.readyState = 1; this.onopen?.() }
  push(type: string, seq: number, payload: Record<string, unknown> = {}, ids: Record<string, string> = {}) {
    this.onmessage?.({ data: JSON.stringify({ type, seq, timestamp: '2026-09-07T10:00:00.000Z',
      conversation_id: 'conversation_a', ...ids, payload }) })
  }
}

function openTerminal(now?: () => number) {
  const socket = new FakeSocket()
  const terminal = useVoiceTerminal({ webSocketUrl: 'ws://test.invalid/ws', webSocketFactory: () => socket, now })
  terminal.connect(); socket.open()
  return { socket, terminal }
}

describe('V0.6 session protocol', () => {
  it('重置立即锁定，保留旧记录，直到后端回执与完成快照均到达', () => {
    const { socket, terminal } = openTerminal()
    socket.push('runtime.snapshot', 1, { can_start: true, session_operation: null,
      conversation: { entries: [{ seq: 1, kind: 'user_turn', conversation_id: 'conversation_a',
        turn_id: 'input-a', speech_id: '', text: '需要保留的对话', status: '', delivery: {}, termination: '', at: '' }], latest_seq: 1 } })
    expect(terminal.resetSession()).toBe(true)
    const request = JSON.parse(socket.sent.at(-1)!)
    expect(request.command).toBe('session.reset')
    expect(terminal.sessionOperation.value).toBe('reset')
    expect(terminal.conversationEntries.value[0]?.text).toBe('需要保留的对话')
    expect(terminal.toggleListening()).toBe(false)
    expect(terminal.resetSession()).toBe(false)
    expect(terminal.clearSession()).toBe(false)
    expect(terminal.setReasoningMode('deep')).toBe(false)
    socket.push('runtime.snapshot', 2, { session_operation: null, can_start: true })
    expect(terminal.sessionOperation.value).toBe('reset')
    socket.push('runtime.snapshot', 3, { session_operation: 'reset', can_start: false })
    socket.push('session.operation', 4, { request_id: request.request_id, operation: 'reset', status: 'completed' })
    expect(terminal.sessionOperation.value).toBe('reset')
    socket.push('runtime.snapshot', 5, { session_operation: null, can_start: true,
      conversation: { entries: [], latest_seq: 0 } })
    expect(terminal.sessionOperation.value).toBeNull()
    expect(terminal.conversationEntries.value).toEqual([])
    expect(terminal.toggleListening()).toBe(true)
    terminal.dispose()
  })

  it('清除使用独立命令，错误回执只解除对应请求，失败后可重试', () => {
    const { socket, terminal } = openTerminal()
    expect(terminal.clearSession()).toBe(true)
    const request = JSON.parse(socket.sent.at(-1)!)
    expect(request.command).toBe('session.clear')
    socket.push('error', 1, { request_id: 'other-request', message: '其他操作错误' })
    expect(terminal.sessionOperation.value).toBe('clear')
    socket.push('error', 2, { request_id: request.request_id, code: 'SESSION_CLEAR_FAILED', message: '归档失败' })
    expect(terminal.sessionOperation.value).toBeNull()
    expect(terminal.connectionError.value).toBe('归档失败')
    expect(terminal.clearSession()).toBe(true)
    socket.close()
    expect(terminal.sessionOperation.value).toBeNull()
    terminal.dispose()
  })

  it('重连快照恢复操作锁定，发送失败不留下加载状态', () => {
    const { socket, terminal } = openTerminal()
    socket.push('runtime.snapshot', 1, { session_operation: 'reset', can_start: false })
    expect(terminal.sessionOperation.value).toBe('reset')
    expect(terminal.toggleListening()).toBe(false)
    expect(terminal.clearSession()).toBe(false)
    socket.push('runtime.snapshot', 2, { session_operation: null, can_start: true })
    vi.spyOn(socket, 'send').mockImplementationOnce(() => { throw new Error('连接中断') })
    expect(terminal.resetSession()).toBe(false)
    expect(terminal.sessionOperation.value).toBeNull()
    expect(terminal.connectionError.value).toBe('连接中断')
    terminal.dispose()
  })

  it('默认快速，仅接收快速和深度模式，并发送对应的模式命令', () => {
    const { socket, terminal } = openTerminal()
    expect(terminal.reasoningMode.value).toBe('fast')
    socket.push('runtime.snapshot', 1, { reasoning_mode: 'deep' })
    expect(terminal.reasoningMode.value).toBe('deep')
    socket.push('runtime.snapshot', 2, { reasoning_mode: 'balanced' })
    expect(terminal.reasoningMode.value).toBe('deep')
    terminal.setReasoningMode('fast')
    expect(terminal.reasoningMode.value).toBe('fast')
    expect(JSON.parse(socket.sent.at(-1)!)).toMatchObject({ command: 'session.reasoning_mode', payload: { mode: 'fast' } })
    terminal.setReasoningMode('deep')
    expect(JSON.parse(socket.sent.at(-1)!)).toMatchObject({ command: 'session.reasoning_mode', payload: { mode: 'deep' } })
    terminal.dispose()
  })

  it('实际播放可与输入结束判定并行，完成后立即清空音量', () => {
    const { socket, terminal } = openTerminal()
    socket.push('session.state', 1, { state: 'FINALIZING', listening: true, audio_generation: 1 })
    socket.push('speech.started', 2, { speech_id: 'speech-live', conversation_id: 'episode-live', text: '实际播放', status: 'playing' })
    socket.push('audio.levels', 3, { source: 'playback', generation: 1, levels: [0.2, 0.4, 0.3, 0.5, 0.6] })
    expect(terminal.audioLevels.value[4]).toBe(0.6)
    socket.push('speech.completed', 4, { speech_id: 'speech-live', conversation_id: 'episode-live', status: 'completed', text: '实际播放' })
    expect(terminal.audioLevels.value).toEqual([0, 0, 0, 0, 0])
    socket.push('audio.levels', 5, { source: 'playback', generation: 1, levels: [1, 1, 1, 1, 1] })
    expect(terminal.audioLevels.value).toEqual([0, 0, 0, 0, 0])
    terminal.dispose()
  })
  it('实际音量帧不改变消息缓存，过期、轮代切换、打断及断线立即安静', () => {
    vi.useFakeTimers()
    const { socket, terminal } = openTerminal()
    try {
      socket.push('session.state', 1, { state: 'LISTENING', listening: true, audio_generation: 5 })
      const entries = terminal.conversationEntries.value
      socket.push('audio.levels', 2, { source: 'input', levels: [0.2, 0.4, 0.6, 0.8, 1], generation: 5 })
      expect(terminal.audioLevels.value[4]).toBe(1)
      expect(terminal.conversationEntries.value).toBe(entries)
      vi.advanceTimersByTime(301)
      expect(terminal.audioLevels.value).toEqual([0, 0, 0, 0, 0])
      socket.push('audio.levels', 3, { source: 'input', levels: [1, 1, 1, 1, 1], generation: 4 })
      expect(terminal.audioLevels.value).toEqual([0, 0, 0, 0, 0])
      socket.push('session.state', 4, { state: 'SPEAKING', listening: true, audio_generation: 6 })
      socket.push('audio.levels', 5, { source: 'playback', levels: [1, 1, 1, 1, 1], generation: 6 })
      expect(terminal.audioLevels.value[0]).toBe(1)
      socket.push('session.state', 6, { state: 'INTERRUPTING', listening: true, audio_generation: 7 })
      expect(terminal.audioLevels.value).toEqual([0, 0, 0, 0, 0])
      socket.push('session.state', 7, { state: 'SPEAKING', listening: true, audio_generation: 8 })
      socket.push('audio.levels', 8, { source: 'playback', levels: [1, 1, 1, 1, 1], generation: 8 })
      terminal.disconnect()
      expect(terminal.audioLevels.value).toEqual([0, 0, 0, 0, 0])
    } finally { terminal.dispose(); vi.useRealTimers() }
  })
  it('没有 Host WebSocket 地址时拒绝本地默认回退', () => {
    const createSocket = vi.fn(() => new FakeSocket())
    const terminal = useVoiceTerminal({ webSocketFactory: createSocket })
    terminal.connect()
    expect(createSocket).not.toHaveBeenCalled()
    expect(terminal.connection.value).toBe('offline')
    expect(terminal.connectionError.value).toContain('应用宿主尚未提供')
    terminal.dispose()
  })

  it('读取被动静默与用户交互状态并在唤醒后更新', () => {
    const { socket, terminal } = openTerminal()
    socket.push('runtime.snapshot', 1, { state: 'IDLE', listening: true, can_start: false, services: {}, current: {},
      quiet_hours: { passive_quiet: true, user_interaction: false, within_quiet_hours: true } })
    expect(terminal.quietHours.passive_quiet).toBe(true)
    socket.push('runtime.snapshot', 2, { state: 'LISTENING', listening: true, can_start: false, services: {}, current: {},
      quiet_hours: { passive_quiet: false, user_interaction: true, within_quiet_hours: true } })
    expect(terminal.quietHours.user_interaction).toBe(true)
    expect(terminal.quietHours.passive_quiet).toBe(false)
  })
  it('subscribes and applies a server-owned session snapshot', () => {
    const { socket, terminal } = openTerminal()
    expect(JSON.parse(socket.sent[0]).command).toBe('subscribe')
    socket.push('runtime.snapshot', 1, { state: 'IDLE', listening: false, can_start: true,
      services: { audio: 'READY', kws: 'DISABLED', asr: 'READY', llm: 'READY', tts: 'READY' },
      current: {}, ledger: [] })
    expect(terminal.state.value).toBe('IDLE')
    expect(terminal.canStart.value).toBe(true)
  })

  it('accepts optional always-on wake ownership without requiring it from old backends', () => {
    const { socket, terminal } = openTerminal()
    socket.push('runtime.snapshot', 1, { state: 'IDLE', listening: true, can_start: false,
      wake_always_on: true, input_owner: 'kws',
      services: { audio: 'RUNNING', kws: 'RUNNING', asr: 'READY', llm: 'READY', tts: 'READY' },
      current: {} })
    expect(terminal.wakeAlwaysOn.value).toBe(true)
    expect(terminal.inputOwner.value).toBe('kws')

    socket.push('runtime.snapshot', 2, { state: 'LISTENING', listening: true, can_start: false,
      services: { audio: 'RUNNING', kws: 'READY', asr: 'RUNNING', llm: 'READY', tts: 'READY' },
      current: {} })
    expect(terminal.wakeAlwaysOn.value).toBe(true)
    expect(terminal.inputOwner.value).toBe('kws')
  })

  it('uses input identity for text and ignores an older sequence', () => {
    const { socket, terminal } = openTerminal()
    socket.push('input.started', 1, {}, { turn_id: 'input_a' })
    socket.push('input.updated', 2, { text: '第一段' }, { turn_id: 'input_a' })
    socket.push('input.updated', 1, { text: '过期文本' }, { turn_id: 'input_a' })
    socket.push('input.finalized', 3, { text: '最终输入' }, { turn_id: 'input_a' })
    expect(terminal.currentTurnId.value).toBe('input_a')
    expect(terminal.current.partial_text).toBe('')
    expect(terminal.current.final_text).toBe('最终输入')
    terminal.dispose()
  })

  it('measures perceptual latency locally until the first real playback event', () => {
    vi.useFakeTimers()
    try {
      let clock = 1000
      const { socket, terminal } = openTerminal(() => clock)
      socket.push('input.started', 1, {}, { turn_id: 'input_a' })
      socket.push('session.state', 2, { state: 'FINALIZING', listening: true, can_start: false }, { turn_id: 'input_a' })

      expect(terminal.perceptualLatencyRunning.value).toBe(true)
      expect(terminal.perceptualLatencyMs.value).toBe(0)

      clock = 1240
      vi.advanceTimersByTime(50)
      expect(terminal.perceptualLatencyMs.value).toBe(240)

      // A delayed server metric is diagnostic-only and must not replace the
      // browser stopwatch used by the product card.
      socket.push('metrics.snapshot', 3, {
        latency: { speech_end_to_playback_ms: 9999 },
      }, { turn_id: 'input_a' })
      expect(terminal.currentLatency.speech_end_to_playback_ms).toBe(9999)
      expect(terminal.perceptualLatencyMs.value).toBe(240)

      clock = 1813
      socket.push('speech.started', 4, {
        speech_id: 'speech_a', conversation_id: 'conversation_a', turn_id: 'input_a',
        text: '开始播放', status: 'playing',
      }, { turn_id: 'input_a', speech_id: 'speech_a' })
      expect(terminal.perceptualLatencyRunning.value).toBe(false)
      expect(terminal.perceptualLatencyMs.value).toBe(813)
      terminal.dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  it('freezes perceptual latency on a same-turn wait decision', () => {
    vi.useFakeTimers()
    try {
      let clock = 1000
      const { socket, terminal } = openTerminal(() => clock)
      socket.push('input.started', 1, {}, { turn_id: 'input_a' })
      socket.push('session.state', 2, { state: 'FINALIZING', listening: true, can_start: false }, { turn_id: 'input_a' })

      clock = 1640
      socket.push('agent.trace', 3, {
        trace_seq: 1, kind: 'SPEAK_STATE_CHANGED', conversation_id: 'episode_a', turn_id: 'input_a', state: 'wait',
      }, { turn_id: 'input_a' })

      expect(terminal.perceptualLatencyRunning.value).toBe(false)
      expect(terminal.perceptualLatencyMs.value).toBe(640)
      clock = 1900
      vi.advanceTimersByTime(50)
      expect(terminal.perceptualLatencyMs.value).toBe(640)
      terminal.dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  it('freezes perceptual latency on a same-turn stop-only cancel', () => {
    vi.useFakeTimers()
    try {
      let clock = 2000
      const { socket, terminal } = openTerminal(() => clock)
      socket.push('input.started', 1, {}, { turn_id: 'input_a' })
      socket.push('session.state', 2, { state: 'FINALIZING', listening: true, can_start: false }, { turn_id: 'input_a' })

      clock = 2475
      socket.push('agent.trace', 3, {
        trace_seq: 1, kind: 'SPEAK_CANCELLED', conversation_id: 'episode_a', turn_id: 'input_a',
      }, { turn_id: 'input_a' })

      expect(terminal.perceptualLatencyRunning.value).toBe(false)
      expect(terminal.perceptualLatencyMs.value).toBe(475)
      clock = 3000
      vi.advanceTimersByTime(50)
      expect(terminal.perceptualLatencyMs.value).toBe(475)
      terminal.dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  it('continues perceptual latency through cancel-with-replacement until its audio starts', () => {
    vi.useFakeTimers()
    try {
      let clock = 3000
      const { socket, terminal } = openTerminal(() => clock)
      socket.push('input.started', 1, {}, { turn_id: 'input_a' })
      socket.push('session.state', 2, { state: 'FINALIZING', listening: true, can_start: false }, { turn_id: 'input_a' })

      clock = 3500
      socket.push('agent.trace', 3, {
        trace_seq: 1, kind: 'SPEAK_STATE_CHANGED', conversation_id: 'episode_a', turn_id: 'input_a', state: 'cancel',
      }, { turn_id: 'input_a' })
      socket.push('agent.trace', 4, {
        trace_seq: 2, kind: 'SPEAK_CANCELLED_WITH_REPLACEMENT', conversation_id: 'episode_a', turn_id: 'input_a',
      }, { turn_id: 'input_a' })
      expect(terminal.perceptualLatencyRunning.value).toBe(true)

      clock = 3780
      socket.push('speech.started', 5, {
        speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a', text: '替换播报', status: 'playing',
      }, { turn_id: 'input_a', speech_id: 'speech_a' })

      expect(terminal.perceptualLatencyRunning.value).toBe(false)
      expect(terminal.perceptualLatencyMs.value).toBe(780)
      terminal.dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores late, mismatched, or turnless agent decisions for perceptual latency', () => {
    vi.useFakeTimers()
    try {
      let clock = 4000
      const { socket, terminal } = openTerminal(() => clock)
      socket.push('input.started', 1, {}, { turn_id: 'input_old' })
      socket.push('session.state', 2, { state: 'FINALIZING', listening: true, can_start: false }, { turn_id: 'input_old' })
      socket.push('input.started', 3, {}, { turn_id: 'input_current' })
      socket.push('session.state', 4, { state: 'FINALIZING', listening: true, can_start: false }, { turn_id: 'input_current' })

      clock = 4530
      socket.push('agent.trace', 5, {
        trace_seq: 1, kind: 'SPEAK_STATE_CHANGED', conversation_id: 'episode_a', turn_id: 'input_old', state: 'wait',
      }, { turn_id: 'input_old' })
      socket.push('agent.trace', 6, {
        trace_seq: 2, kind: 'SPEAK_CANCELLED', conversation_id: 'episode_a',
      })
      expect(terminal.perceptualLatencyRunning.value).toBe(true)

      clock = 4700
      socket.push('agent.trace', 7, {
        trace_seq: 3, kind: 'SPEAK_STATE_CHANGED', conversation_id: 'episode_a', turn_id: 'input_current', state: 'wait',
      }, { turn_id: 'input_current' })
      expect(terminal.perceptualLatencyRunning.value).toBe(false)
      expect(terminal.perceptualLatencyMs.value).toBe(700)
      terminal.dispose()
    } finally {
      vi.useRealTimers()
    }
  })

  it('尾巴修订直接替换权威文本，并拒绝旧版本、错误代数及已结束事务', () => {
    const { socket, terminal } = openTerminal()
    socket.push('input.started', 1, {}, { turn_id: 'input_a' })
    socket.push('agent.state', 2, { conversation_id: 'episode_a', request_id: 'decision_a' })
    const ids = { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a', output_generation: 3 }
    socket.push('speech.queued', 3, { ...ids, text: '' })
    socket.push('speech.delta', 4, { ...ids, text_delta: '已提交旧尾巴' })
    socket.push('speech.started', 5, { ...ids, text: '已提交旧尾巴' })
    socket.push('speech.tail_rewritten', 6, { ...ids, text: '已提交新尾巴', text_revision: 1 })
    expect(terminal.playbackQueue.value[0].text).toBe('已提交新尾巴')
    expect(terminal.playbackQueue.value[0].state).toBe('playing')
    expect(terminal.current.assistant_text).toBe('已提交新尾巴')
    socket.push('speech.tail_rewritten', 7, { ...ids, text: '过期尾巴', text_revision: 1 })
    socket.push('speech.tail_rewritten', 8, { ...ids, output_generation: 2, text: '错误代数', text_revision: 2 })
    socket.push('speech.tail_rewritten', 9, { ...ids, conversation_id: 'other', text: '其他会话', text_revision: 2 })
    socket.push('speech.started', 10, { ...ids, text: '已提交旧尾巴' })
    expect(terminal.playbackQueue.value[0].text).toBe('已提交新尾巴')
    socket.push('speech.completed', 11, { ...ids, text: '已提交新尾巴', status: 'completed' })
    socket.push('speech.tail_rewritten', 12, { ...ids, text: '迟到修订', text_revision: 2 })
    expect(terminal.playbackQueue.value).toEqual([])
    expect(terminal.lastPlayback.value?.text).toBe('已提交新尾巴')
    terminal.dispose()
  })

  it('renders only authorized speech deltas and complete delivery', () => {
    const { socket, terminal } = openTerminal()
    socket.push('agent.state', 1, { conversation_id: 'episode_a', request_id: 'decision_a', agent_busy: true })
    socket.push('speech.delta', 2, { speech_id: 'speech_a', conversation_id: 'episode_a', request_id: 'decision_a',
      text_delta: '已授权输出' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.completed', 3, { speech_id: 'speech_a', conversation_id: 'episode_a', request_id: 'decision_a',
      text: '已授权输出', status: 'completed', delivery: { status: 'full', heard_text: '已授权输出' } },
      { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.activeSpeech.value).toBeNull()
    expect(terminal.lastPlayback.value?.text).toBe('已授权输出')
    expect(terminal.lastPlayback.value?.result).toBe('finished')
    expect(terminal.speechOutputs.value[0].delivery?.heard_text).toBe('已授权输出')
  })

  it('keeps the server order when the wake acknowledgement precedes later turns', () => {
    // Regression: the rail used to re-derive the order, which pushed the wake
    // acknowledgement under every turn that arrived afterwards.
    const { socket, terminal } = openTerminal()
    socket.push('conversation.append', 1, {
      seq: 1, kind: 'user_turn', conversation_id: 'conversation_a', turn_id: 'input_1', text: '灵月是什么游戏？',
    })
    socket.push('conversation.append', 2, {
      seq: 2, kind: 'assistant_speech', conversation_id: 'conversation_a', turn_id: 'input_1',
      speech_id: 'speech_1', text: '它应该是……',
    })
    socket.push('conversation.append', 3, {
      seq: 3, kind: 'system_speech', conversation_id: 'conversation_a', turn_id: 'wake_1',
      speech_id: 'speech_wake', text: '我在，请说。', status: 'completed',
    })
    expect(terminal.latestConversationSeq.value).toBe(3)
    expect(terminal.conversationEntries.value.map(entry => entry.text)).toEqual([
      '灵月是什么游戏？', '它应该是……', '我在，请说。',
    ])

    socket.push('conversation.append', 4, {
      seq: 4, kind: 'user_turn', conversation_id: 'conversation_a', turn_id: 'input_2', text: '补充一点。',
    })

    expect(terminal.conversationEntries.value.map(entry => entry.text)).toEqual([
      '灵月是什么游戏？', '它应该是……', '我在，请说。', '补充一点。',
    ])
    terminal.dispose()
  })

  it('refreshes a re-published conversation line without moving it', () => {
    const { socket, terminal } = openTerminal()
    socket.push('conversation.append', 1, {
      seq: 1, kind: 'user_turn', conversation_id: 'conversation_a', turn_id: 'input_1', text: '进行中', status: 'pending',
    })
    socket.push('conversation.append', 2, {
      seq: 1, kind: 'user_turn', conversation_id: 'conversation_a', turn_id: 'input_1',
      text: '最终转写', status: 'empty_input',
    })

    expect(terminal.conversationEntries.value).toHaveLength(1)
    expect(terminal.conversationEntries.value[0]).toMatchObject({ text: '最终转写', status: 'empty_input' })
    terminal.dispose()
  })

  it('restores the whole rail from a subscription snapshot and replaces it on reset', () => {
    const { socket, terminal } = openTerminal()
    socket.push('conversation.append', 1, {
      seq: 1, kind: 'user_turn', conversation_id: 'conversation_a', turn_id: 'input_1', text: '旧会话',
    })

    // A reload subscribes again: one authoritative snapshot restores the rail,
    // including the assistant side that the output projections never carried.
    socket.push('runtime.snapshot', 2, {
      state: 'IDLE', listening: false, can_start: true,
      services: { audio: 'READY', kws: 'DISABLED', asr: 'READY', llm: 'READY', tts: 'READY' },
      current: {},
      recent_inputs: [],
      conversation: {
        latest_seq: 3,
        entries: [
          { seq: 1, kind: 'user_turn', conversation_id: 'conversation_a', turn_id: 'input_1', text: '灵月是什么游戏？' },
          { seq: 2, kind: 'system_speech', conversation_id: 'conversation_a', turn_id: 'wake_1',
            speech_id: 'speech_wake', text: '我在，请说。' },
          { seq: 3, kind: 'assistant_speech', conversation_id: 'conversation_a', turn_id: 'input_1',
            speech_id: 'speech_1', text: '它应该是……' },
        ],
      },
    })
    expect(terminal.latestConversationSeq.value).toBe(3)
    expect(terminal.conversationEntries.value.map(entry => entry.kind)).toEqual([
      'user_turn', 'system_speech', 'assistant_speech',
    ])

    // A snapshot that only re-states the head position never clears the rail.
    socket.push('runtime.snapshot', 3, {
      state: 'LISTENING', listening: true, can_start: false,
      services: { audio: 'RUNNING', kws: 'READY', asr: 'RUNNING', llm: 'READY', tts: 'READY' },
      current: {},
      conversation: { latest_seq: 5 },
    })
    expect(terminal.conversationEntries.value).toHaveLength(3)

    socket.push('conversation.snapshot', 4, { entries: [], latest_seq: 0 })
    expect(terminal.conversationEntries.value).toEqual([])
    terminal.dispose()
  })

  it('projects queued text, real playback start, and playback finish without waiting for settlement', () => {
    const { socket, terminal } = openTerminal()
    socket.push('agent.state', 1, { conversation_id: 'episode_a', request_id: 'decision_a', agent_busy: true })
    socket.push('speech.queued', 2, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      output_generation: 1, text: '', status: 'READY' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.delta', 3, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      output_generation: 1, text_delta: '实时内容' }, { turn_id: 'input_a', speech_id: 'speech_a' })

    expect(terminal.playbackQueue.value).toHaveLength(1)
    expect(terminal.activeSpeech.value).toMatchObject({ text: '实时内容', state: 'waiting' })
    expect(terminal.current.assistant_text).toBe('实时内容')

    socket.push('speech.started', 4, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      output_generation: 1, text: '实时内容', status: 'playing' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.activeSpeech.value?.state).toBe('playing')

    socket.push('speech.progress', 5, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      output_generation: 1, event: 'tts.progress', trigger: 'playback_finished', status: 'FINISHED' },
    { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.playbackQueue.value).toHaveLength(0)
    expect(terminal.lastPlayback.value).toMatchObject({ text: '实时内容', result: 'finished' })
    expect(terminal.speechOutputs.value).toMatchObject([{
      speech_id: 'speech_a', text: '实时内容', status: 'completed', turn_id: 'input_a',
    }])
  })

  it('keeps each completed speech transaction as its own conversation track', () => {
    const { socket, terminal } = openTerminal()
    socket.push('speech.queued', 1, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a', text: '' },
      { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.delta', 2, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a', text_delta: '第一段' },
      { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.progress', 3, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      event: 'tts.progress', trigger: 'playback_finished', status: 'FINISHED' },
    { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.queued', 4, { speech_id: 'speech_b', conversation_id: 'episode_a', turn_id: 'input_a', text: '' },
      { turn_id: 'input_a', speech_id: 'speech_b' })
    socket.push('speech.delta', 5, { speech_id: 'speech_b', conversation_id: 'episode_a', turn_id: 'input_a', text_delta: '第二段' },
      { turn_id: 'input_a', speech_id: 'speech_b' })
    socket.push('speech.completed', 6, { speech_id: 'speech_b', conversation_id: 'episode_a', turn_id: 'input_a',
      text: '第二段', status: 'completed' }, { turn_id: 'input_a', speech_id: 'speech_b' })

    expect(terminal.speechOutputs.value.map(item => [item.speech_id, item.text])).toEqual([
      ['speech_a', '第一段'], ['speech_b', '第二段'],
    ])
    expect(terminal.playbackQueue.value).toHaveLength(0)
  })

  it('keeps a failed playback result when the final failed record arrives', () => {
    const { socket, terminal } = openTerminal()
    socket.push('speech.delta', 1, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      text_delta: '失败内容' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.progress', 2, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      event: 'tts.progress', trigger: 'playback_finished', status: 'DEGRADED' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.completed', 3, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      text: '失败内容', status: 'failed' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.lastPlayback.value).toMatchObject({ text: '失败内容', result: 'failed' })
  })

  it('does not regress playing state when queued arrives after started', () => {
    const { socket, terminal } = openTerminal()
    socket.push('speech.started', 1, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      output_generation: 2, text: '已经开始', status: 'playing' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.queued', 2, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      output_generation: 2, text: '', status: 'READY' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.activeSpeech.value).toMatchObject({ text: '已经开始', state: 'playing' })
  })

  it('keeps a successor when an older speech completes or is replaced', () => {
    const { socket, terminal } = openTerminal()
    socket.push('speech.started', 1, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      output_generation: 1, text: '旧内容' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.queued', 2, { speech_id: 'speech_b', conversation_id: 'episode_a', turn_id: 'input_b',
      output_generation: 2, text: '新内容' }, { turn_id: 'input_b', speech_id: 'speech_b' })
    socket.push('speech.progress', 3, { speech_id: 'speech_a', old_speech_id: 'speech_a', new_speech_id: 'speech_b',
      conversation_id: 'episode_a', turn_id: 'input_b', output_generation: 2, event: 'speech.replaced', status: 'replaced' },
    { turn_id: 'input_b', speech_id: 'speech_a' })
    socket.push('speech.completed', 4, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      output_generation: 1, text: '旧内容', status: 'replaced' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.playbackQueue.value).toHaveLength(1)
    expect(terminal.activeSpeech.value).toMatchObject({ speech_id: 'speech_b', text: '新内容' })
  })

  it('restores only active and pending speech from an authoritative runtime snapshot', () => {
    const { socket, terminal } = openTerminal()
    socket.push('runtime.snapshot', 1, {
      state: 'LISTENING', listening: true, can_start: false,
      services: { audio: 'RUNNING', kws: 'DISABLED', asr: 'RUNNING', llm: 'READY', tts: 'READY' },
      current: { turn_id: 'input_a' },
      agent: {
        conversation_id: 'episode_a', agent_busy: false, active_speech: {
          speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a', text: '播放中', status: 'SPEAKING',
        }, speech_queue: [{
          speech_id: 'speech_b', conversation_id: 'episode_a', turn_id: 'input_b', text: '等待中', status: 'QUEUED',
        }], outputs: [{
          speech_id: 'speech_old', conversation_id: 'episode_a', turn_id: 'input_old', text: '历史', status: 'completed',
        }],
      },
    })
    expect(terminal.playbackQueue.value.map(item => [item.speech_id, item.state])).toEqual([
      ['speech_a', 'playing'], ['speech_b', 'waiting'],
    ])
    expect(terminal.activeSpeech.value?.speech_id).toBe('speech_a')

    socket.push('runtime.snapshot', 2, {
      state: 'LISTENING', listening: true, can_start: false,
      services: { audio: 'RUNNING', kws: 'DISABLED', asr: 'RUNNING', llm: 'READY', tts: 'READY' },
      current: { turn_id: 'input_a' },
      agent: { conversation_id: 'episode_a', agent_busy: false, active_speech: null, speech_queue: [], outputs: [{
        speech_id: 'speech_old', conversation_id: 'episode_a', turn_id: 'input_old', text: '历史', status: 'completed',
      }] },
    })
    expect(terminal.playbackQueue.value).toHaveLength(0)
    expect(terminal.activeSpeech.value).toBeNull()
    expect(terminal.speechOutputs.value[0]?.speech_id).toBe('speech_old')
  })

  it('does not expose the internal append window as active playback after reconnect', () => {
    const { socket, terminal } = openTerminal()
    socket.push('runtime.snapshot', 1, {
      state: 'LISTENING', listening: true, can_start: false,
      services: { audio: 'RUNNING', kws: 'DISABLED', asr: 'RUNNING', llm: 'READY', tts: 'READY' },
      current: { turn_id: 'input_a', tts_status: 'FINISHED' },
      agent: {
        conversation_id: 'episode_a', agent_busy: false,
        active_speech: { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
          text: '已经播完', status: 'SPEAKING' },
        speech_queue: [{ speech_id: 'speech_b', conversation_id: 'episode_a', turn_id: 'input_b',
          text: '后继内容', status: 'QUEUED' }],
        speak_snapshot: { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
          text: '已经播完', queue_status: 'speaking' },
      },
    })
    expect(terminal.playbackQueue.value).toEqual([expect.objectContaining({ speech_id: 'speech_b', state: 'waiting' })])
    expect(terminal.lastPlayback.value).toMatchObject({ speech_id: 'speech_a', result: 'finished' })

    socket.push('runtime.snapshot', 2, {
      state: 'LISTENING', listening: true, can_start: false,
      services: { audio: 'RUNNING', kws: 'DISABLED', asr: 'RUNNING', llm: 'READY', tts: 'READY' },
      current: { turn_id: 'input_a', tts_status: 'FINISHED' },
      agent: {
        conversation_id: 'episode_a', agent_busy: false,
        active_speech: { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
          text: '已经播完', status: 'SPEAKING' },
        speech_queue: [],
        speak_snapshot: { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
          text: '已经播完', queue_status: 'speaking' },
      },
    })
    expect(terminal.playbackQueue.value).toHaveLength(0)
    expect(terminal.lastPlayback.value).toMatchObject({ speech_id: 'speech_a', result: 'finished' })
  })

  it('does not recreate an older result after a new speech clears it', () => {
    const { socket, terminal } = openTerminal()
    socket.push('speech.delta', 1, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      text_delta: '旧内容' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.progress', 2, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      event: 'tts.progress', trigger: 'playback_finished', status: 'FINISHED' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.queued', 3, { speech_id: 'speech_b', conversation_id: 'episode_a', turn_id: 'input_b',
      text: '新内容' }, { turn_id: 'input_b', speech_id: 'speech_b' })
    socket.push('speech.completed', 4, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      text: '旧内容', status: 'completed' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.lastPlayback.value).toBeNull()
    expect(terminal.activeSpeech.value?.speech_id).toBe('speech_b')
  })

  it('ignores retired conversations while keeping the active output queue on input stop', () => {
    const { socket, terminal } = openTerminal()
    socket.push('agent.state', 1, { conversation_id: 'episode_old', agent_busy: false })
    socket.push('speech.delta', 2, { speech_id: 'speech_old', conversation_id: 'episode_old', turn_id: 'input_old',
      text_delta: '旧内容' }, { turn_id: 'input_old', speech_id: 'speech_old' })
    socket.push('agent.state', 3, { conversation_id: 'episode_new', agent_busy: false })
    socket.push('speech.delta', 4, { speech_id: 'speech_old', conversation_id: 'episode_old', turn_id: 'input_old',
      text_delta: '迟到内容' }, { turn_id: 'input_old', speech_id: 'speech_old' })
    socket.push('speech.queued', 5, { speech_id: 'speech_new', conversation_id: 'episode_new', turn_id: 'input_new',
      text: '新内容' }, { turn_id: 'input_new', speech_id: 'speech_new' })
    expect(terminal.playbackQueue.value.map(item => item.speech_id)).toEqual(['speech_new'])

    socket.push('session.state', 6, { state: 'IDLE', listening: false, can_start: true })
    expect(terminal.playbackQueue.value).toHaveLength(1)
    expect(terminal.activeSpeech.value?.speech_id).toBe('speech_new')
  })

  it('keeps the assistant queue and chat history after listening stops', () => {
    const { socket, terminal } = openTerminal()
    socket.push('agent.state', 1, { conversation_id: 'episode_a', agent_busy: true })
    socket.push('input.started', 2, {}, { turn_id: 'input_a' })
    socket.push('input.finalized', 3, { text: '用户内容' }, { turn_id: 'input_a' })
    socket.push('speech.queued', 4, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a', text: '' },
      { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.delta', 5, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a', text_delta: '模型内容' },
      { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('input.completed', 6, { conversation_id: 'episode_a', transcript: '用户内容' }, { turn_id: 'input_a' })
    expect(terminal.recentTurns.value[0]).toMatchObject({ transcript: '用户内容', response: '模型内容' })

    socket.push('session.state', 7, { state: 'IDLE', listening: false, can_start: true })
    expect(terminal.playbackQueue.value).toMatchObject([{ speech_id: 'speech_a', text: '模型内容' }])
    expect(terminal.speechOutputs.value).toHaveLength(0)
    expect(terminal.current.assistant_text).toBe('模型内容')
    expect(terminal.recentTurns.value[0]).toMatchObject({ transcript: '用户内容', response: '模型内容' })

    // Stopping input must not prevent the same queue item from settling into
    // the normal speech history after playback completes.
    socket.push('speech.completed', 8, { speech_id: 'speech_a', conversation_id: 'episode_a', turn_id: 'input_a',
      text: '模型内容', status: 'completed' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.playbackQueue.value).toHaveLength(0)
    expect(terminal.speechOutputs.value).toMatchObject([{ speech_id: 'speech_a', text: '模型内容', status: 'completed' }])
    terminal.dispose()
  })

  it('renders a recorded fixture as the ordered conversation', () => {
    const socket = new FakeSocket()
    const terminal = useVoiceTerminal({ webSocketUrl: 'ws://test.invalid/ws', webSocketFactory: () => socket })
    terminal.injectReplay(voiceShadowReplayFixture)

    const entries = terminal.conversationEntries.value
    const turns = voiceShadowReplayFixture.recentInputs
    expect(entries).toHaveLength(turns.length * 2)
    // The fixture is newest-first; the rail reads it oldest-first with each
    // recorded reply directly after the turn it answered.
    expect(entries[0]).toMatchObject({
      kind: 'user_turn',
      text: turns[turns.length - 1].transcript,
      status: 'completed',
    })
    expect(entries[1]).toMatchObject({
      kind: 'assistant_speech',
      text: turns[turns.length - 1].response,
      status: 'completed',
    })
    expect(entries.map(entry => entry.seq)).toEqual(entries.map((_entry, index) => index + 1))
    expect(terminal.latestConversationSeq.value).toBe(entries.length)
    terminal.dispose()
  })

  it('shows a speak preview while the JSON decision is still uncommitted', () => {
    const { socket, terminal } = openTerminal()
    socket.push('agent.state', 1, { conversation_id: 'conversation_a', request_id: 'decision_a', agent_busy: true })
    socket.push('speak.preview', 2, { conversation_id: 'conversation_a', request_id: 'decision_a',
      state: 'speak', text: '正在生成的候选文本', text_delta: '正在生成的候选文本', committed: false })
    expect(terminal.speakPreview.value?.text).toBe('正在生成的候选文本')
    socket.push('speak.preview', 3, { conversation_id: 'conversation_a', request_id: 'decision_a', status: 'committed' })
    expect(terminal.speakPreview.value).toBeNull()
  })

  it('keeps a recoverable capture gap diagnostic without changing server listening state', () => {
    const { socket, terminal } = openTerminal()
    socket.push('runtime.snapshot', 1, { state: 'LISTENING', listening: true, can_start: true,
      services: { audio: 'RUNNING', kws: 'DISABLED', asr: 'RUNNING', llm: 'READY', tts: 'READY' },
      current: { capture_diagnostics: { reason: 'capture_gap', dropped_chunks: 2 } } })

    expect(terminal.listening.value).toBe(true)
    expect(terminal.current.capture_diagnostics?.reason).toBe('capture_gap')
    expect(terminal.current.capture_diagnostics?.dropped_chunks).toBe(2)
  })

  it('keeps late SenseVoice text supplemental and applies turn latency payloads', () => {
    const { socket, terminal } = openTerminal()
    socket.push('input.started', 1, {}, { turn_id: 'input_a' })
    socket.push('input.finalized', 2, { text: 'Paraformer 主文本', source: 'paraformer' }, { turn_id: 'input_a' })
    socket.push('input.finalized', 3, { text: 'SenseVoice 精修文本', source: 'sensevoice' }, { turn_id: 'input_a' })
    socket.push('metrics.snapshot', 4, {
      turn_id: 'input_a', input_id: 'input_a', speech_id: 'speech_a',
      latency: { speech_end_to_first_pcm_ms: 812, replacement_count: 1 },
    }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.progress', 5, {
      event: 'tts.prewarm', status: 'READY', turn_id: 'input_a',
    }, { turn_id: 'input_a' })

    expect(terminal.current.final_text).toBe('Paraformer 主文本')
    expect(terminal.current.refined_text).toBe('SenseVoice 精修文本')
    expect(terminal.current.refinement_available).toBe(true)
    expect(terminal.currentLatency.speech_end_to_first_pcm_ms).toBe(812)
    expect(terminal.currentLatency.replacement_count).toBe(1)
    expect(terminal.current.tts_prewarm_status).toBe('READY')
    terminal.dispose()
  })

  it('restores and incrementally updates the safe Shadow product projection', () => {
    const { socket, terminal } = openTerminal()
    socket.push('runtime.snapshot', 1, {
      state: 'IDLE', listening: false, can_start: true,
      services: { audio: 'READY', kws: 'DISABLED', asr: 'READY', llm: 'READY', tts: 'READY' },
      current: {},
      shadow: {
        available: true, active: true, phase: 'working', user_input_required: false,
        focus: '整理当前会话', summary: '正在后台分析',
        timeline: [{ state: 'active', label: '后台处理中', text: '正在分析', timestamp: '2026-09-07T10:00:00.000Z' }],
        activities: [{ run_seq: 8, item_id: 'thinking-8', stream: 'thinking', phase: 'started', text: '正在', timestamp: '2026-09-07T10:00:00.000Z' }],
        context: { used_tokens: 86000, limit_tokens: 256000, usage_percent: 34 },
        provider: { input_tokens: 86000, cached_tokens: 81700, cache_hit_ratio: 0.95 },
      },
    })
    expect(terminal.shadowProduct.value).toMatchObject({
      available: true, active: true, phase: 'working', focus: '整理当前会话',
    })
    expect(terminal.shadowProduct.value.context?.used_tokens).toBe(86000)

    socket.push('shadow.product', 2, {
      available: true, active: false, phase: 'idle', user_input_required: false,
      focus: '已完成', summary: '后台结果已更新',
      timeline: Array.from({ length: 7 }, (_, index) => ({
        state: 'done', label: `步骤 ${index}`, text: `结果 ${index}`,
        timestamp: '2026-09-07T10:00:00.000Z',
      })),
      activities: [{ run_seq: 8, item_id: 'thinking-8', stream: 'thinking', phase: 'started', text: '正在', timestamp: '2026-09-07T10:00:00.000Z' }],
    })
    expect(terminal.shadowProduct.value.active).toBe(false)
    expect(terminal.shadowProduct.value.activities?.[0]).toMatchObject({
      run_seq: 8, item_id: 'thinking-8', text: '正在', phase: 'started',
    })
    expect(terminal.shadowProduct.value.timeline).toHaveLength(5)
    expect(terminal.shadowProduct.value.timeline[0].label).toBe('步骤 2')

    socket.push('shadow.activity', 3, {
      run_seq: 8, item_id: 'thinking-8', stream: 'thinking', phase: 'delta',
      delta: '分析', timestamp: '2026-09-07T10:00:01.000Z',
    })
    socket.push('shadow.activity', 4, {
      run_seq: 8, item_id: 'thinking-8', stream: 'thinking', phase: 'completed',
      timestamp: '2026-09-07T10:00:02.000Z',
    })
    socket.push('shadow.activity', 5, {
      run_seq: 8, item_id: 'tool-8', stream: 'tool', phase: 'started',
      tool_name: 'files_search', tool_status: 'running', timestamp: '2026-09-07T10:00:03.000Z',
    })
    socket.push('shadow.activity', 6, {
      run_seq: 7, item_id: 'late-7', stream: 'event', phase: 'completed',
      event_kind: 'result', text: '旧运行', timestamp: '2026-09-07T10:00:04.000Z',
    })
    socket.push('shadow.activity', 7, {
      run_seq: 8, item_id: 'thinking-8', stream: 'thinking', phase: 'delta',
      delta: '迟到', timestamp: '2026-09-07T10:00:05.000Z',
    })
    socket.push('shadow.activity', 8, {
      run_seq: 8, item_id: 'tool-8', stream: 'tool', phase: 'completed',
      tool_name: 'files_search', tool_status: 'completed', text: '完成', timestamp: '2026-09-07T10:00:05.500Z',
    })
    socket.push('shadow.activity', 9, {
      run_seq: 9, item_id: 'run-9', stream: 'lifecycle', phase: 'started',
      text: '新一轮运行', timestamp: '2026-09-07T10:00:06.000Z',
    })
    expect(terminal.shadowProduct.value.activities).toEqual(expect.arrayContaining([
      expect.objectContaining({ item_id: 'thinking-8', text: '正在分析', phase: 'completed' }),
      expect.objectContaining({ item_id: 'tool-8', tool_name: 'files_search', tool_status: 'completed' }),
      expect.objectContaining({ item_id: 'run-9', run_seq: 9, phase: 'started' }),
    ]))
    expect(terminal.shadowProduct.value.activities?.some(item => item.item_id === 'thinking-8')).toBe(true)
    expect(terminal.shadowProduct.value.activities?.some(item => item.item_id === 'late-7')).toBe(false)
  })

  it('keeps a DSH observer summary and its activity synchronized in the WebUI', () => {
    const { socket, terminal } = openTerminal()
    const summary = '已确认 DSH 正在处理当前任务，已提交一个模型步骤，当前没有待处理工具。'

    socket.push('runtime.snapshot', 1, {
      state: 'IDLE', listening: false, can_start: true,
      services: { audio: 'READY', kws: 'DISABLED', asr: 'READY', llm: 'READY', tts: 'READY' },
      current: {},
      shadow: {
        available: true, active: true, phase: 'working', user_input_required: false,
        focus: '', summary: '', timeline: [], activities: [],
      },
    })
    // The Bridge publishes the public activity first.  The following product
    // snapshot is authoritative and must retain the same observer item while
    // making its summary immediately available to the rendered status card.
    socket.push('shadow.activity', 2, {
      run_seq: 4, item_id: 'observer-4', stream: 'thinking', phase: 'delta',
      text: summary, timestamp: '2026-09-21T12:00:01.000Z',
    })
    socket.push('shadow.product', 3, {
      available: true, active: true, phase: 'working', user_input_required: false,
      focus: summary, summary,
      timeline: [{
        state: 'active', label: '状态汇报', text: summary,
        timestamp: '2026-09-21T12:00:01.000Z',
      }],
      activities: [{
        run_seq: 4, item_id: 'observer-4', stream: 'thinking', phase: 'delta',
        text: summary, timestamp: '2026-09-21T12:00:01.000Z',
      }],
      last_activity_at: '2026-09-21T12:00:01.000Z',
    })

    expect(terminal.shadowProduct.value.summary).toBe(summary)
    expect(terminal.shadowProduct.value.timeline).toEqual([
      expect.objectContaining({ label: '状态汇报', text: summary }),
    ])
    expect(terminal.shadowProduct.value.activities).toEqual([
      expect.objectContaining({
        run_seq: 4, item_id: 'observer-4', stream: 'thinking', phase: 'delta', text: summary,
      }),
    ])
    expect(terminal.shadowProduct.value.last_activity_at).toBe('2026-09-21T12:00:01.000Z')
    terminal.dispose()
  })
})

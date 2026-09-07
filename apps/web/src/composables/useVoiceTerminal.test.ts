import { describe, expect, it } from 'vitest'
import { useVoiceTerminal, type VoiceSocket } from './useVoiceTerminal'

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

function openTerminal() {
  const socket = new FakeSocket()
  const terminal = useVoiceTerminal({ webSocketFactory: () => socket })
  terminal.connect(); socket.open()
  return { socket, terminal }
}

describe('V0.5 session protocol', () => {
  it('subscribes and applies a server-owned session snapshot', () => {
    const { socket, terminal } = openTerminal()
    expect(JSON.parse(socket.sent[0]).command).toBe('subscribe')
    socket.push('runtime.snapshot', 1, { state: 'IDLE', listening: false, can_start: true,
      services: { audio: 'READY', kws: 'DISABLED', asr: 'READY', llm: 'READY', tts: 'READY' },
      current: {}, ledger: [] })
    expect(terminal.state.value).toBe('IDLE')
    expect(terminal.canStart.value).toBe(true)
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
  })

  it('renders only authorized speech deltas and complete delivery', () => {
    const { socket, terminal } = openTerminal()
    socket.push('agent.state', 1, { episode_id: 'episode_a', agent_step_id: 'decision_a', context_version: 1, agent_state: 'INVOKING' })
    socket.push('speech.delta', 2, { speech_id: 'speech_a', episode_id: 'episode_a', agent_step_id: 'decision_a',
      context_version: 1, text_delta: '已授权输出' }, { turn_id: 'input_a', speech_id: 'speech_a' })
    socket.push('speech.completed', 3, { speech_id: 'speech_a', episode_id: 'episode_a', agent_step_id: 'decision_a',
      context_version: 1, text: '已授权输出', status: 'completed', delivery: { status: 'full', heard_text: '已授权输出' } },
      { turn_id: 'input_a', speech_id: 'speech_a' })
    expect(terminal.activeSpeech.value?.text).toBe('已授权输出')
    expect(terminal.speechOutputs.value[0].delivery?.heard_text).toBe('已授权输出')
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
})

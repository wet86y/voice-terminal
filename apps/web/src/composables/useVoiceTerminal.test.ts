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

describe('V0.6 session protocol', () => {
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
    expect(terminal.speechOutputs.value).toHaveLength(0)
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

  it('ignores retired conversations and clears dashboard state when the session stops', () => {
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
    expect(terminal.playbackQueue.value).toHaveLength(0)
    expect(terminal.lastPlayback.value).toBeNull()
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
  })
})

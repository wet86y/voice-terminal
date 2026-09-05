import { computed, reactive, ref, shallowRef } from 'vue'
import type {
  CommandName,
  ConnectionState,
  CurrentTurnSnapshot,
  LlmContextMessage,
  LatencySnapshotPayload,
  LatencyValues,
  RuntimeSnapshotPayload,
  ServerMessage,
  ServiceStatuses,
  SessionStatePayload,
  TerminalState,
  TtsStatusPayload,
  TurnCompletedPayload,
  TurnRecord,
} from '../types'
import { TERMINAL_STATES } from '../types'

const DEFAULT_WS_URL = 'ws://127.0.0.1:8765/ws'
const OPEN_READY_STATE = 1
const MAX_RECENT_TURNS = 10

export interface VoiceSocket {
  readyState: number
  send(data: string): void
  close(): void
  onopen: (() => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onerror: (() => void) | null
  onclose: (() => void) | null
}

export interface UseVoiceTerminalOptions {
  webSocketFactory?: (url: string) => VoiceSocket
  webSocketUrl?: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isTerminalState(value: unknown): value is TerminalState {
  return typeof value === 'string' && TERMINAL_STATES.includes(value as TerminalState)
}

function readString(payload: Record<string, unknown>, key: string): string | null {
  return typeof payload[key] === 'string' ? payload[key] as string : null
}

function createEmptyCurrentTurn(): CurrentTurnSnapshot {
  return {
    partial_text: '',
    final_text: '',
    assistant_text: '',
    tts_text: '',
    tts_status: '',
    audio_buffer_ms: null,
    tts_returned_ms: 0,
    tts_played_ms: 0,
    tts_remaining_ms: 0,
    v02: false, input_revision: 0, generation_id: 0, committed: false,
    agent_phase: 'IDLE', paraformer_final: '', refined_text: '',
    segments: [], prediction: {}, tools: [], audio_cues: [], analysis: {},
  }
}

function createEmptyLatency(): LatencyValues {
  return {
    wake_detection_ms: null,
    asr_first_text_ms: null,
    speech_end_confirmation_ms: null,
    llm_first_token_ms: null,
    llm_request_to_first_tts_text_ms: null,
    tts_text_to_first_audio_ms: null,
    first_audio_to_playback_ms: null,
    speech_end_to_playback_ms: null,
    turn_total_ms: null,
    first_segment_ms: null, first_prediction_ms: null, smart_turn_ms: null,
    sensevoice_refine_ms: null, commit_to_final_llm_ms: null,
  }
}

function createOfflineStatuses(): ServiceStatuses {
  return {
    audio: 'OFFLINE',
    kws: 'OFFLINE',
    asr: 'OFFLINE',
    llm: 'OFFLINE',
    tts: 'OFFLINE',
    analysis: 'DISABLED',
  }
}

function parseServerMessage(raw: unknown): ServerMessage | null {
  if (typeof raw !== 'string') return null

  try {
    const parsed: unknown = JSON.parse(raw)
    if (!isRecord(parsed)) return null
    if (typeof parsed.type !== 'string') return null
    if (typeof parsed.seq !== 'number' || !Number.isFinite(parsed.seq)) return null
    if (typeof parsed.timestamp !== 'string') return null
    if (parsed.turn_id !== null && typeof parsed.turn_id !== 'string') return null
    return parsed as unknown as ServerMessage
  } catch {
    return null
  }
}

function copyLatencyValues(input: unknown): LatencyValues {
  const result = createEmptyLatency()
  if (!isRecord(input)) return result

  for (const key of Object.keys(result) as Array<keyof LatencyValues>) {
    const value = input[key]
    if (value === null || (typeof value === 'number' && Number.isFinite(value))) {
      result[key] = value as number | null
    }
  }
  return result
}

export function useVoiceTerminal(options: UseVoiceTerminalOptions = {}) {
  const socket = shallowRef<VoiceSocket | null>(null)
  const connection = ref<ConnectionState>('offline')
  const connectionError = ref('')
  const state = ref<TerminalState>('STARTING')
  const listening = ref(false)
  const canStart = ref(false)
  const currentTurnId = ref<string | null>(null)
  const current = reactive<CurrentTurnSnapshot>(createEmptyCurrentTurn())
  const currentLatency = reactive<LatencyValues>(createEmptyLatency())
  const currentLatencyComplete = ref(false)
  const services = reactive<ServiceStatuses>(createOfflineStatuses())
  const recentTurns = ref<TurnRecord[]>([])
  const llmContext = ref<LlmContextMessage[]>([])
  const llmContextTurnId = ref<string | null>(null)
  const lastServerTimestamp = ref<string | null>(null)
  const lastSequence = ref<number | null>(null)

  const webSocketUrl = options.webSocketUrl ?? import.meta.env.VITE_VOICE_WS_URL ?? DEFAULT_WS_URL
  const createSocket = options.webSocketFactory ?? ((url: string) => new WebSocket(url) as unknown as VoiceSocket)
  let requestSequence = 0

  const connectionLabel = computed(() => {
    if (connection.value === 'connected') return 'CONNECTED'
    if (connection.value === 'connecting') return 'CONNECTING'
    return 'OFFLINE'
  })

  function resetCurrentTurn() {
    Object.assign(current, createEmptyCurrentTurn())
    Object.assign(currentLatency, createEmptyLatency())
    currentLatencyComplete.value = false
  }

  function adoptTurn(turnId: string | null) {
    if (!turnId || currentTurnId.value === turnId) return
    currentTurnId.value = turnId
    resetCurrentTurn()
  }

  function acceptsCurrentTurn(turnId: string | null) {
    if (!turnId) return false
    if (currentTurnId.value === null) currentTurnId.value = turnId
    return currentTurnId.value === turnId
  }

  function applyCurrentSnapshot(snapshot: unknown) {
    if (!isRecord(snapshot)) return
    applyWorkspace(snapshot)
    const partialText = readString(snapshot, 'partial_text')
    const finalText = readString(snapshot, 'final_text')
    const assistantText = readString(snapshot, 'assistant_text')
    const ttsText = readString(snapshot, 'tts_text')
    const ttsStatus = readString(snapshot, 'tts_status')
    const audioBufferMs = snapshot.audio_buffer_ms
    const ttsReturnedMs = snapshot.tts_returned_ms
    const ttsPlayedMs = snapshot.tts_played_ms
    const ttsRemainingMs = snapshot.tts_remaining_ms

    if (partialText !== null) current.partial_text = partialText
    if (finalText !== null) current.final_text = finalText
    if (assistantText !== null) current.assistant_text = assistantText
    if (ttsText !== null) current.tts_text = ttsText
    if (ttsStatus !== null) current.tts_status = ttsStatus
    if (audioBufferMs === null || (typeof audioBufferMs === 'number' && Number.isFinite(audioBufferMs))) {
      current.audio_buffer_ms = audioBufferMs as number | null
    }
    for (const [key, value] of [
      ['tts_returned_ms', ttsReturnedMs],
      ['tts_played_ms', ttsPlayedMs],
      ['tts_remaining_ms', ttsRemainingMs],
    ] as const) {
      if (value === null || (typeof value === 'number' && Number.isFinite(value))) {
        current[key] = value as number | null
      }
    }
  }

  function applyWorkspace(snapshot: Record<string, unknown>) {
    for (const key of ['input_revision', 'generation_id'] as const) {
      if (typeof snapshot[key] === 'number') current[key] = snapshot[key]
    }
    for (const key of ['v02', 'committed'] as const) {
      if (typeof snapshot[key] === 'boolean') current[key] = snapshot[key]
    }
    for (const key of ['agent_phase', 'paraformer_final', 'refined_text'] as const) {
      if (typeof snapshot[key] === 'string') current[key] = snapshot[key]
    }
    if (Array.isArray(snapshot.segments)) current.segments = snapshot.segments as NonNullable<CurrentTurnSnapshot['segments']>
    if (Array.isArray(snapshot.tools)) current.tools = snapshot.tools as NonNullable<CurrentTurnSnapshot['tools']>
    if (Array.isArray(snapshot.audio_cues)) current.audio_cues = snapshot.audio_cues as NonNullable<CurrentTurnSnapshot['audio_cues']>
    if (isRecord(snapshot.prediction)) current.prediction = snapshot.prediction
    if (isRecord(snapshot.analysis)) current.analysis = snapshot.analysis as NonNullable<CurrentTurnSnapshot['analysis']>
  }

  function applyLatencySnapshot(payload: LatencySnapshotPayload) {
    if (!isRecord(payload.values)) return
    for (const key of Object.keys(currentLatency) as Array<keyof LatencyValues>) {
      const value = payload.values[key]
      if (value === null || (typeof value === 'number' && Number.isFinite(value))) {
        currentLatency[key] = value as number | null
      }
    }
    currentLatencyComplete.value = payload.complete === true
  }

  function applyServices(nextServices: unknown) {
    if (!isRecord(nextServices)) return
    for (const key of Object.keys(services) as Array<keyof ServiceStatuses>) {
      const value = nextServices[key]
      if (typeof value === 'string') services[key] = value
    }
  }

  function applyRuntimeSnapshot(payload: RuntimeSnapshotPayload, turnId: string | null) {
    if (isTerminalState(payload.state)) state.value = payload.state
    if (typeof payload.listening === 'boolean') listening.value = payload.listening
    if (typeof payload.can_start === 'boolean') canStart.value = payload.can_start
    applyServices(payload.services)
    const snapshotTurnId = isRecord(payload.current) ? readString(payload.current, 'turn_id') : null
    if (turnId || snapshotTurnId) adoptTurn(turnId || snapshotTurnId)
    applyCurrentSnapshot(payload.current)
    if (payload.latency) applyLatencySnapshot(payload.latency)
    if (Array.isArray(payload.recent_turns)) {
      recentTurns.value = payload.recent_turns.filter(item => isRecord(item) && typeof item.turn_id === 'string')
        .slice(0, MAX_RECENT_TURNS).map(item => ({ ...item, latency: copyLatencyValues(item.latency) }))
    }
  }

  function applySessionState(payload: SessionStatePayload, turnId: string | null) {
    if (isTerminalState(payload.state)) state.value = payload.state
    if (typeof payload.listening === 'boolean') listening.value = payload.listening
    if (typeof payload.can_start === 'boolean') canStart.value = payload.can_start
    if (turnId) adoptTurn(turnId)
  }

  function applyCompletedTurn(payload: TurnCompletedPayload, turnId: string | null) {
    if (!turnId) return
    const transcript = typeof payload.transcript === 'string' ? payload.transcript : current.final_text
    const response = typeof payload.response === 'string' ? payload.response : current.assistant_text
    const completedAt = typeof payload.completed_at === 'string' ? payload.completed_at : ''
    const record: TurnRecord = {
      turn_id: turnId,
      transcript,
      response,
      completed_at: completedAt,
      latency: copyLatencyValues(payload.latency),
    }

    recentTurns.value = [record, ...recentTurns.value.filter((item) => item.turn_id !== turnId)]
      .slice(0, MAX_RECENT_TURNS)

    if (currentTurnId.value === null || currentTurnId.value === turnId) {
      currentTurnId.value = turnId
      current.final_text = transcript
      current.assistant_text = response
      Object.assign(currentLatency, record.latency)
      currentLatencyComplete.value = true
    }
  }

  function applyLlmContext(payload: unknown, turnId: string | null) {
    if (!acceptsCurrentTurn(turnId)) return
    if (!isRecord(payload) || !Array.isArray(payload.messages)) return

    const messages: LlmContextMessage[] = []
    for (const item of payload.messages) {
      if (!isRecord(item)) continue
      if (typeof item.role !== 'string' || typeof item.content !== 'string') continue
      messages.push({ role: item.role, content: item.content })
    }
    llmContext.value = messages
    llmContextTurnId.value = turnId
  }

  function handleMessage(raw: unknown) {
    const message = parseServerMessage(raw)
    if (!message) {
      connectionError.value = '收到无法解析的后端消息。'
      return
    }

    if (lastSequence.value !== null && message.seq <= lastSequence.value) return
    lastSequence.value = message.seq
    lastServerTimestamp.value = message.timestamp
    const payload = isRecord(message.payload) ? message.payload : {}
    if (message.type === 'turn.started') adoptTurn(message.turn_id)
    if (message.turn_id === currentTurnId.value && typeof payload.generation_id === 'number') {
      if (payload.generation_id < (current.generation_id ?? 0)) return
      if (typeof payload.input_revision === 'number' && payload.generation_id === current.generation_id
        && payload.input_revision < (current.input_revision ?? 0)) return
      current.generation_id = payload.generation_id
      if (isRecord(payload.workspace)) { current.v02 = true; applyWorkspace(payload.workspace) }
    }

    switch (message.type) {
      case 'turn.resumed':
        if (acceptsCurrentTurn(message.turn_id)) {
          current.final_text = ''; current.assistant_text = ''; current.tts_text = ''
          current.audio_buffer_ms = 0; current.tts_status = 'IDLE'
          current.tts_returned_ms = 0; current.tts_played_ms = 0; current.tts_remaining_ms = 0
        }
        break
      case 'runtime.snapshot':
        applyRuntimeSnapshot(payload as unknown as RuntimeSnapshotPayload, message.turn_id)
        break
      case 'session.state':
        applySessionState(payload as unknown as SessionStatePayload, message.turn_id)
        break
      case 'transcript.partial':
        if (acceptsCurrentTurn(message.turn_id)) {
          const text = readString(payload, 'text')
          if (text !== null) current.partial_text = text
        }
        break
      case 'transcript.final':
        if (acceptsCurrentTurn(message.turn_id)) {
          const text = readString(payload, 'text')
          if (text !== null) {
            current.final_text = text
            current.partial_text = ''
          }
        }
        break
      case 'assistant.delta':
        if (acceptsCurrentTurn(message.turn_id)) {
          const textDelta = readString(payload, 'text_delta')
          if (textDelta !== null) current.assistant_text += textDelta
        }
        break
      case 'llm.context':
        applyLlmContext(payload, message.turn_id)
        break
      case 'tts.status':
      case 'tts.progress':
        if (acceptsCurrentTurn(message.turn_id)) {
          const waitingText = readString(payload, 'waiting_text')
          const status = readString(payload, 'status')
          const bufferMs = payload.audio_buffer_ms
          const returnedMs = payload.audio_returned_ms
          const playedMs = payload.audio_played_ms
          const remainingMs = payload.audio_remaining_ms
          if (waitingText !== null) current.tts_text = waitingText
          if (status !== null) current.tts_status = status
          if (bufferMs === null || (typeof bufferMs === 'number' && Number.isFinite(bufferMs))) {
            current.audio_buffer_ms = bufferMs as number | null
          }
          if (returnedMs === null || (typeof returnedMs === 'number' && Number.isFinite(returnedMs))) {
            current.tts_returned_ms = returnedMs as number | null
          }
          if (playedMs === null || (typeof playedMs === 'number' && Number.isFinite(playedMs))) {
            current.tts_played_ms = playedMs as number | null
          }
          if (remainingMs === null || (typeof remainingMs === 'number' && Number.isFinite(remainingMs))) {
            current.tts_remaining_ms = remainingMs as number | null
          }
        }
        break
      case 'latency.snapshot':
        if (acceptsCurrentTurn(message.turn_id)) {
          applyLatencySnapshot(payload as unknown as LatencySnapshotPayload)
        }
        break
      case 'turn.completed':
        applyCompletedTurn(payload as unknown as TurnCompletedPayload, message.turn_id)
        break
      case 'error': {
        const messageText = readString(payload, 'message')
        connectionError.value = messageText ?? '后端报告了未知错误。'
        break
      }
      default:
        break
    }
  }

  function sendCommand(command: CommandName, payload: Record<string, unknown> = {}) {
    const activeSocket = socket.value
    if (!activeSocket || activeSocket.readyState !== OPEN_READY_STATE) return false

    requestSequence += 1
    activeSocket.send(JSON.stringify({
      type: 'command' as const,
      request_id: `req_${String(requestSequence).padStart(6, '0')}`,
      command,
      payload,
    }))
    return true
  }

  function connect() {
    if (connection.value === 'connecting' || connection.value === 'connected') return

    connection.value = 'connecting'
    connectionError.value = ''
    canStart.value = false
    lastSequence.value = null

    try {
      const nextSocket = createSocket(webSocketUrl)
      socket.value = nextSocket

      nextSocket.onopen = () => {
        if (socket.value !== nextSocket) return
        connection.value = 'connected'
        sendCommand('subscribe', { client: 'voice-terminal-web', version: '0.1.0' })
      }
      nextSocket.onmessage = (event) => {
        if (socket.value === nextSocket) handleMessage(event.data)
      }
      nextSocket.onerror = () => {
        if (socket.value !== nextSocket) return
        connectionError.value = '无法连接本地 Voice Server，请确认后端已启动。'
        canStart.value = false
      }
      nextSocket.onclose = () => {
        if (socket.value !== nextSocket) return
        socket.value = null
        connection.value = 'offline'
        canStart.value = false
        listening.value = false
        Object.assign(services, createOfflineStatuses())
        if (!connectionError.value) connectionError.value = 'Voice Server 连接已关闭。'
      }
    } catch (error) {
      connection.value = 'offline'
      canStart.value = false
      connectionError.value = error instanceof Error ? error.message : 'WebSocket 初始化失败。'
    }
  }

  function toggleListening() {
    if (connection.value !== 'connected') return false
    if (listening.value) return sendCommand('session.stop')
    if (!canStart.value) return false
    return sendCommand('session.start')
  }

  function dispose() {
    const activeSocket = socket.value
    socket.value = null
    activeSocket?.close()
    connection.value = 'offline'
    canStart.value = false
  }

  return {
    connection,
    connectionError,
    connectionLabel,
    state,
    listening,
    canStart,
    currentTurnId,
    current,
    currentLatency,
    currentLatencyComplete,
    services,
    recentTurns,
    llmContext,
    llmContextTurnId,
    lastServerTimestamp,
    lastSequence,
    webSocketUrl,
    connect,
    toggleListening,
    dispose,
  }
}

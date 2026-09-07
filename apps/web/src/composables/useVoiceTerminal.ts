import { computed, reactive, ref, shallowRef } from 'vue'
import type {
  AgentEpisodeSnapshot,
  AgentTrace,
  SpeechOutputRecord,
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
  TurnDelivery,
  TtsStatusPayload,
  TurnCompletedPayload,
  InputRecord,
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

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && Number.isFinite(value) && value >= 0
}

function isAgentTrace(value: unknown): value is AgentTrace {
  return isRecord(value) && isNonNegativeInteger(value.trace_seq)
    && typeof value.kind === 'string'
    && (value.episode_id === null || typeof value.episode_id === 'string')
    && (value.context_version === undefined || isNonNegativeInteger(value.context_version))
}

function isSpeechOutput(value: unknown): value is SpeechOutputRecord {
  return isRecord(value) && typeof value.speech_id === 'string' && value.speech_id.length > 0
    && (value.episode_id === undefined || value.episode_id === null || typeof value.episode_id === 'string')
    && (value.agent_step_id === undefined || value.agent_step_id === null || typeof value.agent_step_id === 'string')
    && (value.context_version === undefined || isNonNegativeInteger(value.context_version))
    && (value.generation_id === undefined || isNonNegativeInteger(value.generation_id))
    && (value.status === undefined || typeof value.status === 'string')
}

function isTerminalState(value: unknown): value is TerminalState {
  return typeof value === 'string' && TERMINAL_STATES.includes(value as TerminalState)
}

function readString(payload: Record<string, unknown>, key: string): string | null {
  return typeof payload[key] === 'string' ? payload[key] as string : null
}

function readDelivery(value: unknown): TurnDelivery | undefined {
  return isRecord(value) ? value as TurnDelivery : undefined
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
    tts_output_estimated_ms: 0,
    tts_remaining_ms: 0,
    delivery: {},
    audio_processing_status: 'DISABLED',
    audio_processing_error: '',
    tts_transport: 'websocket',
    tts_scheduling: 'auto',
    input_runtime: true, input_revision: 0, generation_id: 0, committed: false,
    agent_phase: 'IDLE', paraformer_final: '', refined_text: '',
    segments: [], tools: [], audio_cues: [], analysis: {},
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
    first_segment_ms: null, smart_turn_ms: null,
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
    web_search: 'DISABLED',
    analysis: 'DISABLED',
  }
}

function parseServerMessage(raw: unknown): ServerMessage | null {
  if (typeof raw !== 'string') return null

  try {
    const parsed: unknown = JSON.parse(raw)
    if (!isRecord(parsed)) return null
    if (typeof parsed.type !== 'string') return null
    if (typeof parsed.seq !== 'number' || !Number.isInteger(parsed.seq) || !Number.isFinite(parsed.seq)) return null
    if (typeof parsed.timestamp !== 'string') return null
    if (typeof parsed.session_id !== 'string' || !parsed.session_id) return null
    for (const key of ['input_id', 'episode_id', 'decision_id', 'speech_id']) {
      if (parsed[key] !== undefined && parsed[key] !== null && typeof parsed[key] !== 'string') return null
    }
    if (!isRecord(parsed.payload)) return null
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
  const recentTurns = ref<InputRecord[]>([])
  const agentEpisode = ref<AgentEpisodeSnapshot | null>(null)
  const agentTrace = ref<AgentTrace[]>([])
  const speechOutputs = ref<SpeechOutputRecord[]>([])
  const activeSpeech = ref<SpeechOutputRecord | null>(null)
  const speechProgress = ref<Record<string, unknown> | null>(null)
  const prefill = ref<{ status?: string, text?: string } | null>(null)
  const reasoning = ref('')
  const retiredEpisodes = new Set<string>()

  function applyAgent(value: unknown, authoritative = false) {
    if (!isRecord(value) || !(value.episode_id === null || typeof value.episode_id === 'string')
      || !isNonNegativeInteger(value.context_version)
      || (value.agent_step_id !== undefined && value.agent_step_id !== null && typeof value.agent_step_id !== 'string')
      || (value.agent_state !== undefined && typeof value.agent_state !== 'string')
      || (value.decision_mode !== undefined && typeof value.decision_mode !== 'string')
      || (value.user_speaking !== undefined && value.user_speaking !== null && typeof value.user_speaking !== 'boolean')
      || (value.request_count !== undefined && !isNonNegativeInteger(value.request_count))
      || (value.context_chars !== undefined && !isNonNegativeInteger(value.context_chars))
      || (value.context_limit_tokens !== undefined && !isNonNegativeInteger(value.context_limit_tokens))
      || (value.context_token_estimate !== undefined && !isNonNegativeInteger(value.context_token_estimate))
      || (value.provider_prompt_tokens !== undefined && value.provider_prompt_tokens !== null
        && !isNonNegativeInteger(value.provider_prompt_tokens))
      || (value.context_usage_percent !== undefined
        && (typeof value.context_usage_percent !== 'number' || !Number.isFinite(value.context_usage_percent)
          || value.context_usage_percent < 0))) return
    const previous = agentEpisode.value
    if (!authoritative && typeof value.episode_id === 'string' && retiredEpisodes.has(value.episode_id)) return
    if (!authoritative && previous?.episode_id === value.episode_id && value.context_version < previous.context_version) return
    if (previous?.episode_id && previous.episode_id !== value.episode_id) {
      retiredEpisodes.add(previous.episode_id)
      speechProgress.value = null
    }
    if (value.episode_id === null) speechProgress.value = null
    if (retiredEpisodes.size > 64) retiredEpisodes.delete(retiredEpisodes.values().next().value!)
    agentEpisode.value = value as unknown as AgentEpisodeSnapshot
    if (Array.isArray(value.trace)) agentTrace.value = value.trace.filter(isAgentTrace).slice(-200)
    if (Array.isArray(value.outputs)) speechOutputs.value = value.outputs.filter(isSpeechOutput).slice(-32)
    prefill.value = isRecord(value.pending_prefill) ? value.pending_prefill as { status?: string, text?: string } : null
    if (isRecord(value.reasoning) && typeof value.reasoning.text === 'string') reasoning.value = value.reasoning.text
    if (isSpeechOutput(value.active_speech)) activeSpeech.value = value.active_speech
    else if (value.episode_id === null) activeSpeech.value = null
    else if (value.active_speech === null && Array.isArray(value.outputs)) {
      activeSpeech.value = [...speechOutputs.value].reverse()
        .find(output => output.episode_id === value.episode_id) ?? null
    }
  }

  function applyAgentTrace(value: Record<string, unknown>) {
    if (!isAgentTrace(value)) return
    if (!agentTrace.value.some(item => item.trace_seq === value.trace_seq && item.episode_id === value.episode_id)) {
      agentTrace.value = [...agentTrace.value, value as unknown as AgentTrace].slice(-200)
    }
  }

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

  function resetConnectionData() {
    currentTurnId.value = null
    agentEpisode.value = null
    agentTrace.value = []
    speechOutputs.value = []
    activeSpeech.value = null
    speechProgress.value = null
    retiredEpisodes.clear()
    resetCurrentTurn()
    llmContext.value = []
    llmContextTurnId.value = null
    lastServerTimestamp.value = null
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
    const ttsOutputEstimatedMs = snapshot.tts_output_estimated_ms
    const ttsRemainingMs = snapshot.tts_remaining_ms
    const delivery = readDelivery(snapshot.delivery)

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
      ['tts_output_estimated_ms', ttsOutputEstimatedMs],
      ['tts_remaining_ms', ttsRemainingMs],
    ] as const) {
      if (value === null || (typeof value === 'number' && Number.isFinite(value))) {
        current[key] = value as number | null
      }
    }
    if (delivery !== undefined) current.delivery = delivery
    for (const key of ['audio_processing_status', 'audio_processing_error', 'tts_transport', 'tts_scheduling'] as const) {
      if (typeof snapshot[key] === 'string') current[key] = snapshot[key] as string
    }
  }

  function applyWorkspace(snapshot: Record<string, unknown>) {
    for (const key of ['input_revision', 'generation_id'] as const) {
      if (typeof snapshot[key] === 'number') current[key] = snapshot[key]
    }
    for (const key of ['input_runtime', 'committed'] as const) {
      if (typeof snapshot[key] === 'boolean') current[key] = snapshot[key]
    }
    for (const key of ['agent_phase', 'paraformer_final', 'refined_text'] as const) {
      if (typeof snapshot[key] === 'string') current[key] = snapshot[key]
    }
    if (Array.isArray(snapshot.segments)) current.segments = snapshot.segments as NonNullable<CurrentTurnSnapshot['segments']>
    if (Array.isArray(snapshot.tools)) current.tools = snapshot.tools as NonNullable<CurrentTurnSnapshot['tools']>
    if (Array.isArray(snapshot.audio_cues)) current.audio_cues = snapshot.audio_cues as NonNullable<CurrentTurnSnapshot['audio_cues']>
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
    const hasCurrentTurnId = isRecord(payload.current)
      && Object.prototype.hasOwnProperty.call(payload.current, 'input_id')
    const snapshotTurnId = isRecord(payload.current) ? readString(payload.current, 'input_id') : null
    if (turnId || snapshotTurnId) adoptTurn(turnId || snapshotTurnId)
    else if (hasCurrentTurnId && currentTurnId.value !== null) resetConnectionData()
    applyCurrentSnapshot(payload.current)
    if (payload.agent) applyAgent(payload.agent, true)
    if (payload.latency) applyLatencySnapshot(payload.latency)
    if (Array.isArray(payload.recent_inputs)) {
      recentTurns.value = (payload.recent_inputs.filter(item => isRecord(item) && typeof item.input_id === 'string') as InputRecord[])
        .slice(0, MAX_RECENT_TURNS).map(item => ({
          ...item,
          latency: copyLatencyValues(item.latency),
          delivery: readDelivery(item.delivery),
        }))
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
    const delivery = readDelivery(payload.delivery)
    const record: InputRecord = {
      input_id: turnId,
      episode_id: payload.episode_id,
      details: payload.details,
      transcript,
      response,
      completed_at: completedAt,
      latency: copyLatencyValues(payload.latency),
      status: typeof payload.status === 'string' ? payload.status : delivery?.termination,
      delivery,
    }

    recentTurns.value = [record, ...recentTurns.value.filter((item) => item.input_id !== turnId)]
      .slice(0, MAX_RECENT_TURNS)

    if (currentTurnId.value === null || currentTurnId.value === turnId) {
      currentTurnId.value = turnId
      current.final_text = transcript
      current.assistant_text = response
      if (delivery !== undefined) current.delivery = delivery
      Object.assign(currentLatency, record.latency)
      currentLatencyComplete.value = true
    }
  }

  function applyLlmContext(payload: unknown, turnId: string | null) {
    if (!isRecord(payload) || !Array.isArray(payload.messages)) return
    for (const key of ['context_chars', 'context_limit_tokens', 'context_token_estimate'] as const) {
      if (payload[key] !== undefined && !isNonNegativeInteger(payload[key])) return
    }
    if (payload.context_usage_percent !== undefined
      && (typeof payload.context_usage_percent !== 'number' || !Number.isFinite(payload.context_usage_percent)
        || payload.context_usage_percent < 0)) return
    if (typeof payload.episode_id === 'string') {
      if (payload.episode_id !== agentEpisode.value?.episode_id) return
      if (payload.agent_step_id !== agentEpisode.value?.agent_step_id) return
      if (payload.context_version !== undefined
        && (!isNonNegativeInteger(payload.context_version)
          || payload.context_version < (agentEpisode.value?.context_version ?? 0))) return
    } else if (!acceptsCurrentTurn(turnId)) return

    const messages: LlmContextMessage[] = []
    for (const item of payload.messages) {
      if (!isRecord(item)) continue
      if (typeof item.role !== 'string' || !(typeof item.content === 'string' || item.content === null)) continue
      messages.push({ role: item.role, content: typeof item.content === 'string' ? item.content : '',
        ...(Array.isArray(item.tool_calls) ? { tool_calls: item.tool_calls } : {}),
        ...(typeof item.tool_call_id === 'string' ? { tool_call_id: item.tool_call_id } : {}),
      })
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
    // Agent/output generations are independent of the currently displayed input turn.
    if (message.type === 'agent.state') { applyAgent(payload); return }
    if (message.type === 'agent.trace') { applyAgentTrace(payload); return }
    if (message.type === 'agent.prefill') { prefill.value = isRecord(payload.prefill) ? payload.prefill as { status?: string, text?: string } : null; return }
    if (message.type === 'agent.reasoning') { if (typeof payload.text === 'string') reasoning.value = payload.text; return }
    if (message.type === 'speech.queued') return
    if (message.type === 'speech.completed') {
      if (isSpeechOutput(payload)) {
        speechOutputs.value = [...speechOutputs.value.filter(item => item.speech_id !== payload.speech_id), payload].slice(-32)
        if (speechProgress.value?.speech_id === payload.speech_id) speechProgress.value = null
        if (activeSpeech.value?.speech_id === payload.speech_id) activeSpeech.value = payload
      }
      return
    }
    if (message.type === 'speech.delta') {
      const currentAgent = agentEpisode.value
      const textDelta = readString(payload, 'text_delta')
      if (textDelta !== null && typeof payload.speech_id === 'string'
        && payload.episode_id === currentAgent?.episode_id
        && payload.agent_step_id === currentAgent?.agent_step_id
        && payload.context_version === currentAgent?.context_version) {
        if (activeSpeech.value?.speech_id !== payload.speech_id) {
          activeSpeech.value = {
            speech_id: payload.speech_id,
            episode_id: payload.episode_id as string,
            agent_step_id: payload.agent_step_id as string,
            context_version: payload.context_version as number,
            input_id: typeof payload.input_id === 'string' ? payload.input_id : message.input_id || '',
            generation_id: typeof payload.generation_id === 'number' ? payload.generation_id : 0,
            text: '', status: 'streaming',
          }
        }
        if (activeSpeech.value) activeSpeech.value = { ...activeSpeech.value, text: activeSpeech.value.text + textDelta }
      }
      return
    }
    if (message.type === 'speech.progress') {
      const currentAgent = agentEpisode.value
      const stepMatches = payload.agent_step_id === undefined
        || payload.agent_step_id === currentAgent?.agent_step_id
      if (typeof payload.speech_id === 'string' && typeof payload.episode_id === 'string'
        && isNonNegativeInteger(payload.context_version)
        && payload.episode_id === currentAgent?.episode_id
        && payload.context_version === currentAgent?.context_version
        && stepMatches
        && !speechOutputs.value.some(item => item.speech_id === payload.speech_id)) speechProgress.value = payload
      return
    }
    if (message.type === 'input.started') adoptTurn(message.input_id ?? null)
    if (message.input_id === currentTurnId.value && typeof payload.generation_id === 'number') {
      if (payload.generation_id < (current.generation_id ?? 0)) return
      if (typeof payload.input_revision === 'number' && payload.generation_id === current.generation_id
        && payload.input_revision < (current.input_revision ?? 0)) return
      current.generation_id = payload.generation_id
      if (isRecord(payload.workspace)) { current.input_runtime = true; applyWorkspace(payload.workspace) }
    }

    switch (message.type) {
      case 'runtime.snapshot':
        applyRuntimeSnapshot(payload as unknown as RuntimeSnapshotPayload, message.input_id ?? null)
        break
      case 'session.state':
        applySessionState(payload as unknown as SessionStatePayload, message.input_id ?? null)
        break
      case 'input.updated':
        if (acceptsCurrentTurn(message.input_id ?? null)) {
          const text = readString(payload, 'text')
          if (text !== null) current.partial_text = text
        }
        break
      case 'input.finalized':
        if (acceptsCurrentTurn(message.input_id ?? null)) {
          const text = readString(payload, 'text')
          if (text !== null) {
            current.final_text = text
            current.partial_text = ''
          }
        }
        break
      case 'speech.delta':
        if (acceptsCurrentTurn(message.input_id ?? null)) {
          const textDelta = readString(payload, 'text_delta')
          if (textDelta !== null) current.assistant_text += textDelta
        }
        break
      case 'context.snapshot':
        applyLlmContext(payload, message.input_id ?? null)
        break
      case 'speech.progress':
        if (acceptsCurrentTurn(message.input_id ?? null)) {
          const waitingText = readString(payload, 'waiting_text')
          const status = readString(payload, 'status')
          const bufferMs = payload.audio_buffer_ms
          const returnedMs = payload.audio_returned_ms
          const playedMs = payload.audio_played_ms
          const outputEstimatedMs = payload.audio_output_estimated_ms
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
          if (outputEstimatedMs === null || (typeof outputEstimatedMs === 'number' && Number.isFinite(outputEstimatedMs))) {
            current.tts_output_estimated_ms = outputEstimatedMs as number | null
          }
          if (remainingMs === null || (typeof remainingMs === 'number' && Number.isFinite(remainingMs))) {
            current.tts_remaining_ms = remainingMs as number | null
          }
        }
        break
      case 'metrics.snapshot':
        if (acceptsCurrentTurn(message.input_id ?? null)) {
          applyLatencySnapshot(payload as unknown as LatencySnapshotPayload)
        }
        break
      case 'input.completed':
        applyCompletedTurn(payload as unknown as TurnCompletedPayload, message.input_id ?? null)
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

    resetConnectionData()
    connection.value = 'connecting'
    connectionError.value = ''
    state.value = 'STARTING'
    listening.value = false
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
        state.value = 'STARTING'
        resetConnectionData()
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

  function resetSession() {
    if (connection.value !== 'connected') return false
    resetCurrentTurn()
    activeSpeech.value = null
    speechProgress.value = null
    llmContext.value = []
    llmContextTurnId.value = null
    return sendCommand('session.reset')
  }

  function dispose() {
    const activeSocket = socket.value
    socket.value = null
    activeSocket?.close()
    connection.value = 'offline'
    state.value = 'STARTING'
    listening.value = false
    canStart.value = false
    resetConnectionData()
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
    agentEpisode,
    agentTrace,
    speechOutputs,
    activeSpeech,
    speechProgress,
    prefill,
    reasoning,
    llmContext,
    llmContextTurnId,
    lastServerTimestamp,
    lastSequence,
    webSocketUrl,
    connect,
    toggleListening,
    resetSession,
    dispose,
  }
}

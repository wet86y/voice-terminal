import { computed, reactive, ref, shallowRef } from 'vue'
import type {
  AgentEpisodeSnapshot,
  AgentTrace,
  LastPlaybackResult,
  PlaybackQueueItem,
  PlaybackResultStatus,
  SpeakPreview,
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
  ReasoningMode,
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
    && (value.conversation_id === null || typeof value.conversation_id === 'string')
}

function isSpeechOutput(value: unknown): value is SpeechOutputRecord {
  return isRecord(value) && typeof value.speech_id === 'string' && value.speech_id.length > 0
    && (value.conversation_id === undefined || value.conversation_id === null || typeof value.conversation_id === 'string')
    && (value.trace_id === undefined || value.trace_id === null || typeof value.trace_id === 'string')
    && (value.output_generation === undefined || isNonNegativeInteger(value.output_generation))
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
    capture_diagnostics: {},
    playback_diagnostics: {},
    tts_transport: 'websocket',
    tts_scheduling: 'auto',
    input_runtime: true, output_generation: 0, committed: false,
    agent_phase: 'IDLE', paraformer_final: '', refined_text: '',
    refinement_available: false, tts_prewarm_started: false, tts_prewarm_status: 'NOT_REQUESTED',
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
    asr_final_to_commit_ms: null, refine_duration_ms: null,
    refine_to_redecision_ms: null, agent_request_to_speak_ms: null,
    tts_prewarm_requested_ms: null, tts_prewarm_ready_ms: null,
    tts_checkout_ms: null, tts_cold_start_ms: null,
    speech_end_to_first_pcm_ms: null, first_pcm_to_player_submit_ms: null,
    replacement_count: null,
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
    if (typeof parsed.conversation_id !== 'string' || !parsed.conversation_id) return null
    for (const key of ['turn_id', 'speech_id']) {
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
  const reasoningMode = ref<ReasoningMode>('balanced')
  const currentTurnId = ref<string | null>(null)
  const current = reactive<CurrentTurnSnapshot>(createEmptyCurrentTurn())
  const currentLatency = reactive<LatencyValues>(createEmptyLatency())
  const currentLatencyComplete = ref(false)
  const services = reactive<ServiceStatuses>(createOfflineStatuses())
  const recentTurns = ref<InputRecord[]>([])
  const agentEpisode = ref<AgentEpisodeSnapshot | null>(null)
  const agentTrace = ref<AgentTrace[]>([])
  const speechOutputs = ref<SpeechOutputRecord[]>([])
  const playbackQueue = ref<PlaybackQueueItem[]>([])
  const lastPlayback = ref<LastPlaybackResult | null>(null)
  const activeSpeech = computed(() =>
    playbackQueue.value.find(item => item.state === 'playing') ?? playbackQueue.value[0] ?? null,
  )
  const speechProgress = ref<Record<string, unknown> | null>(null)
  const reasoning = ref('')
  const speakPreview = ref<SpeakPreview | null>(null)
  const retiredEpisodes = new Set<string>()
  const terminalPlaybackKeys = new Set<string>()

  function speechKey(conversationId: string, speechId: string) {
    return `${conversationId}\u0000${speechId}`
  }

  function readPlaybackItem(value: Record<string, unknown>, stateValue: PlaybackQueueItem['state']): PlaybackQueueItem | null {
    const speechId = readString(value, 'speech_id')
    const conversationId = readString(value, 'conversation_id')
    if (!speechId || !conversationId || retiredEpisodes.has(conversationId)) return null
    return {
      speech_id: speechId,
      conversation_id: conversationId,
      request_id: readString(value, 'request_id') ?? readString(value, 'trace_id') ?? undefined,
      turn_id: readString(value, 'turn_id') ?? '',
      output_generation: isNonNegativeInteger(value.output_generation) ? value.output_generation : undefined,
      text: readString(value, 'text') ?? '',
      state: stateValue,
      emotion_requested: readString(value, 'emotion_requested') ?? readString(value, 'emotion') ?? undefined,
      emotion_applied: readString(value, 'emotion_applied') ?? undefined,
    }
  }

  function upsertPlaybackItem(incoming: PlaybackQueueItem) {
    const key = speechKey(incoming.conversation_id, incoming.speech_id)
    const index = playbackQueue.value.findIndex(item => speechKey(item.conversation_id, item.speech_id) === key)
    if (index < 0) {
      playbackQueue.value = [...playbackQueue.value, incoming]
      return
    }
    const currentItem = playbackQueue.value[index]
    if (currentItem.output_generation !== undefined && incoming.output_generation !== undefined
      && incoming.output_generation < currentItem.output_generation) return
    const next = {
      ...currentItem,
      ...incoming,
      text: incoming.text || currentItem.text,
      state: currentItem.state === 'playing' ? 'playing' as const : incoming.state,
    }
    playbackQueue.value = playbackQueue.value.map((item, itemIndex) => itemIndex === index ? next : item)
  }

  function removePlaybackItem(conversationId: string, speechId: string) {
    const key = speechKey(conversationId, speechId)
    const removed = playbackQueue.value.find(item => speechKey(item.conversation_id, item.speech_id) === key) ?? null
    playbackQueue.value = playbackQueue.value.filter(item => speechKey(item.conversation_id, item.speech_id) !== key)
    return removed
  }

  function recordPlaybackResult(payload: Record<string, unknown>, result: PlaybackResultStatus) {
    const speechId = readString(payload, 'speech_id')
    const conversationId = readString(payload, 'conversation_id')
    if (!speechId || !conversationId || retiredEpisodes.has(conversationId)) return
    const activeItem = playbackQueue.value.find(item => item.speech_id === speechId && item.conversation_id === conversationId)
    if (activeItem?.output_generation !== undefined && isNonNegativeInteger(payload.output_generation)
      && payload.output_generation < activeItem.output_generation) return
    terminalPlaybackKeys.add(speechKey(conversationId, speechId))
    if (terminalPlaybackKeys.size > 128) terminalPlaybackKeys.delete(terminalPlaybackKeys.values().next().value!)
    const removed = removePlaybackItem(conversationId, speechId)
    const previous = lastPlayback.value?.speech_id === speechId
      && lastPlayback.value.conversation_id === conversationId ? lastPlayback.value : null
    lastPlayback.value = {
      speech_id: speechId,
      conversation_id: conversationId,
      turn_id: readString(payload, 'turn_id') ?? removed?.turn_id ?? previous?.turn_id ?? '',
      text: readString(payload, 'text') || removed?.text || previous?.text || '',
      result: previous?.result ?? result,
    }
  }

  function resultFromCompletion(status: string | null): PlaybackResultStatus {
    if (status === 'completed') return 'finished'
    if (status === 'failed') return 'failed'
    if (status === 'replaced' || status === 'superseded') return 'replaced'
    return 'interrupted'
  }

  function applyAuthoritativePlaybackSnapshot(value: Record<string, unknown>) {
    const restored: PlaybackQueueItem[] = []
    const hasStructuredPlaybackSnapshot = Object.prototype.hasOwnProperty.call(value, 'active_speech')
      || Object.prototype.hasOwnProperty.call(value, 'speech_queue')
    const append = (candidate: unknown, stateValue: PlaybackQueueItem['state']) => {
      if (!isRecord(candidate)) return
      const item = readPlaybackItem(candidate, stateValue)
      if (!item) return
      const key = speechKey(item.conversation_id, item.speech_id)
      if (!restored.some(existing => speechKey(existing.conversation_id, existing.speech_id) === key)) restored.push(item)
    }
    const playbackAlreadyFinished = current.tts_status === 'FINISHED' || current.tts_status === 'DEGRADED'
    if (isRecord(value.active_speech) && playbackAlreadyFinished) {
      recordPlaybackResult(value.active_speech, current.tts_status === 'FINISHED' ? 'finished' : 'failed')
    } else if (isRecord(value.active_speech)) {
      append(value.active_speech, readString(value.active_speech, 'status')?.toUpperCase() === 'SPEAKING' ? 'playing' : 'waiting')
    }
    if (Array.isArray(value.speech_queue)) value.speech_queue.forEach(item => append(item, 'waiting'))
    if (!hasStructuredPlaybackSnapshot && !restored.length && isRecord(value.speak_snapshot)
      && readString(value.speak_snapshot, 'queue_status') !== 'empty') {
      append(value.speak_snapshot, readString(value.speak_snapshot, 'queue_status') === 'speaking' ? 'playing' : 'waiting')
    }
    playbackQueue.value = restored
  }

  function applyAgent(value: unknown, authoritative = false) {
    if (!isRecord(value) || !(value.conversation_id === null || typeof value.conversation_id === 'string')
      || (value.trace_id !== undefined && value.trace_id !== null && typeof value.trace_id !== 'string')
      || (value.agent_busy !== undefined && typeof value.agent_busy !== 'boolean')
      || (value.user_speaking !== undefined && value.user_speaking !== null && typeof value.user_speaking !== 'boolean')
      || (value.request_count !== undefined && !isNonNegativeInteger(value.request_count))
      || (value.context_chars !== undefined && !isNonNegativeInteger(value.context_chars))
      || (value.context_limit_tokens !== undefined && !isNonNegativeInteger(value.context_limit_tokens))
      || (value.context_token_estimate !== undefined && !isNonNegativeInteger(value.context_token_estimate))
      || (value.provider_prompt_tokens !== undefined && value.provider_prompt_tokens !== null
        && !isNonNegativeInteger(value.provider_prompt_tokens))
      || (value.provider_input_tokens !== undefined && value.provider_input_tokens !== null
        && !isNonNegativeInteger(value.provider_input_tokens))
      || (value.provider_cached_tokens !== undefined && value.provider_cached_tokens !== null
        && !isNonNegativeInteger(value.provider_cached_tokens))
      || (value.provider_cache_miss_tokens !== undefined && value.provider_cache_miss_tokens !== null
        && !isNonNegativeInteger(value.provider_cache_miss_tokens))
      || (value.provider_cache_hit_ratio !== undefined && value.provider_cache_hit_ratio !== null
        && (typeof value.provider_cache_hit_ratio !== 'number' || !Number.isFinite(value.provider_cache_hit_ratio)
          || value.provider_cache_hit_ratio < 0 || value.provider_cache_hit_ratio > 1))
      || (value.context_usage_percent !== undefined
        && (typeof value.context_usage_percent !== 'number' || !Number.isFinite(value.context_usage_percent)
          || value.context_usage_percent < 0))) return
    const previous = agentEpisode.value
    if (!authoritative && typeof value.conversation_id === 'string' && retiredEpisodes.has(value.conversation_id)) return
    if (previous?.conversation_id && previous.conversation_id !== value.conversation_id) {
      retiredEpisodes.add(previous.conversation_id)
      speechProgress.value = null
      speakPreview.value = null
      playbackQueue.value = []
      lastPlayback.value = null
      terminalPlaybackKeys.clear()
    }
    if (value.conversation_id === null) {
      speechProgress.value = null
      speakPreview.value = null
      playbackQueue.value = []
      lastPlayback.value = null
      terminalPlaybackKeys.clear()
    }
    if (retiredEpisodes.size > 64) retiredEpisodes.delete(retiredEpisodes.values().next().value!)
    agentEpisode.value = value as unknown as AgentEpisodeSnapshot
    if (Array.isArray(value.trace)) agentTrace.value = value.trace.filter(isAgentTrace).slice(-200)
    if (Array.isArray(value.outputs)) speechOutputs.value = value.outputs.filter(isSpeechOutput).slice(-32)
    if (isRecord(value.reasoning) && typeof value.reasoning.text === 'string') reasoning.value = value.reasoning.text
    if (authoritative) applyAuthoritativePlaybackSnapshot(value)
  }

  function applySpeakPreview(value: Record<string, unknown>) {
    const conversationId = readString(value, 'conversation_id')
    const requestId = readString(value, 'request_id')
    if (!conversationId || !requestId) return
    if (conversationId !== agentEpisode.value?.conversation_id) return
    const status = readString(value, 'status')
    if (status === 'committed' || status === 'discarded') {
      if (speakPreview.value?.request_id === requestId) speakPreview.value = null
      return
    }
    const text = readString(value, 'text')
    const textDelta = readString(value, 'text_delta')
    if (readString(value, 'state') !== 'speak' || (text === null && textDelta === null)) return
    speakPreview.value = {
      conversation_id: conversationId,
      request_id: requestId,
      turn_id: readString(value, 'turn_id'),
      state: 'speak',
      text: text ?? `${speakPreview.value?.request_id === requestId ? speakPreview.value.text || '' : ''}${textDelta || ''}`,
      text_delta: textDelta || undefined,
      committed: false,
    }
  }

  function applyAgentTrace(value: Record<string, unknown>) {
    if (!isAgentTrace(value)) return
    if (!agentTrace.value.some(item => item.trace_seq === value.trace_seq && item.conversation_id === value.conversation_id)) {
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
    playbackQueue.value = []
    lastPlayback.value = null
    speechProgress.value = null
    terminalPlaybackKeys.clear()
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
    if (isRecord(snapshot.capture_diagnostics)) {
      current.capture_diagnostics = snapshot.capture_diagnostics as CurrentTurnSnapshot['capture_diagnostics']
    }
    if (isRecord(snapshot.playback_diagnostics)) {
      current.playback_diagnostics = snapshot.playback_diagnostics as CurrentTurnSnapshot['playback_diagnostics']
    }
    for (const key of ['audio_processing_status', 'audio_processing_error', 'tts_transport', 'tts_scheduling'] as const) {
      if (typeof snapshot[key] === 'string') current[key] = snapshot[key] as string
    }
  }

  function applyWorkspace(snapshot: Record<string, unknown>) {
    for (const key of ['output_generation'] as const) {
      if (typeof snapshot[key] === 'number') current[key] = snapshot[key]
    }
    for (const key of ['input_runtime', 'committed'] as const) {
      if (typeof snapshot[key] === 'boolean') current[key] = snapshot[key]
    }
    for (const key of ['agent_phase', 'paraformer_final', 'refined_text', 'tts_prewarm_status'] as const) {
      if (typeof snapshot[key] === 'string') current[key] = snapshot[key]
    }
    for (const key of ['refinement_available', 'tts_prewarm_started'] as const) {
      if (typeof snapshot[key] === 'boolean') current[key] = snapshot[key]
    }
    if (Array.isArray(snapshot.segments)) current.segments = snapshot.segments as NonNullable<CurrentTurnSnapshot['segments']>
    if (Array.isArray(snapshot.tools)) current.tools = snapshot.tools as NonNullable<CurrentTurnSnapshot['tools']>
    if (Array.isArray(snapshot.audio_cues)) current.audio_cues = snapshot.audio_cues as NonNullable<CurrentTurnSnapshot['audio_cues']>
    if (isRecord(snapshot.analysis)) current.analysis = snapshot.analysis as NonNullable<CurrentTurnSnapshot['analysis']>
  }

  function applyLatencySnapshot(payload: LatencySnapshotPayload) {
    const values: Record<string, unknown> | null = isRecord(payload.values)
      ? payload.values as Record<string, unknown>
      : isRecord((payload as unknown as Record<string, unknown>).latency)
        ? (payload as unknown as Record<string, unknown>).latency as Record<string, unknown>
        : null
    if (!values) return
    for (const key of Object.keys(currentLatency) as Array<keyof LatencyValues>) {
      const value = values[key]
      if (value === null || (typeof value === 'number' && Number.isFinite(value))) {
        currentLatency[key] = value as number | null
      }
    }
    currentLatencyComplete.value = payload.complete === true
      || isRecord((payload as unknown as Record<string, unknown>).latency)
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
    if (payload.reasoning_mode === 'fast' || payload.reasoning_mode === 'balanced' || payload.reasoning_mode === 'deep') reasoningMode.value = payload.reasoning_mode
    applyServices(payload.services)
    const hasCurrentTurnId = isRecord(payload.current)
      && Object.prototype.hasOwnProperty.call(payload.current, 'turn_id')
    const snapshotTurnId = isRecord(payload.current) ? readString(payload.current, 'turn_id') : null
    if (turnId || snapshotTurnId) adoptTurn(turnId || snapshotTurnId)
    else if (hasCurrentTurnId && currentTurnId.value !== null) resetConnectionData()
    applyCurrentSnapshot(payload.current)
    if (payload.agent) applyAgent(payload.agent, true)
    if (payload.latency) applyLatencySnapshot(payload.latency)
    if (Array.isArray(payload.recent_inputs)) {
      recentTurns.value = (payload.recent_inputs.filter(item => isRecord(item) && typeof item.turn_id === 'string') as InputRecord[])
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
    if (payload.listening === false && payload.state === 'IDLE') {
      playbackQueue.value = []
      lastPlayback.value = null
      speechProgress.value = null
      terminalPlaybackKeys.clear()
    }
  }

  function applyCompletedTurn(payload: TurnCompletedPayload, turnId: string | null) {
    if (!turnId) return
    const transcript = typeof payload.transcript === 'string' ? payload.transcript : current.final_text
    const response = typeof payload.response === 'string' ? payload.response : current.assistant_text
    const completedAt = typeof payload.completed_at === 'string' ? payload.completed_at : ''
    const delivery = readDelivery(payload.delivery)
    const record: InputRecord = {
      turn_id: turnId,
      conversation_id: payload.conversation_id,
      details: payload.details,
      transcript,
      response,
      completed_at: completedAt,
      latency: copyLatencyValues(payload.latency),
      status: typeof payload.status === 'string' ? payload.status : delivery?.termination,
      delivery,
    }

    recentTurns.value = [record, ...recentTurns.value.filter((item) => item.turn_id !== turnId)]
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
    if (typeof payload.conversation_id === 'string') {
      if (payload.conversation_id !== agentEpisode.value?.conversation_id) return
    } else if (!acceptsCurrentTurn(turnId)) return

    const messages: LlmContextMessage[] = []
    for (const item of payload.messages) {
      if (!isRecord(item)) continue
      if (typeof item.role !== 'string' || !(typeof item.content === 'string' || item.content === null)) continue
      messages.push({ role: item.role, content: typeof item.content === 'string' ? item.content : '' })
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
    if (message.type === 'agent.reasoning') { if (typeof payload.text === 'string') reasoning.value = payload.text; return }
    if (message.type === 'speak.preview') { applySpeakPreview(payload); return }
    if (message.type === 'speech.queued') {
      const item = readPlaybackItem(payload, 'waiting')
      if (item && (!agentEpisode.value?.conversation_id || item.conversation_id === agentEpisode.value.conversation_id)) {
        lastPlayback.value = null
        upsertPlaybackItem(item)
      }
      return
    }
    if (message.type === 'speech.started') {
      const item = readPlaybackItem(payload, 'playing')
      if (item && (!agentEpisode.value?.conversation_id || item.conversation_id === agentEpisode.value.conversation_id)) {
        upsertPlaybackItem(item)
      }
      return
    }
    if (message.type === 'speech.completed') {
      if (isSpeechOutput(payload)) {
        speechOutputs.value = [...speechOutputs.value.filter(item => item.speech_id !== payload.speech_id), payload].slice(-32)
        if (speechProgress.value?.speech_id === payload.speech_id) speechProgress.value = null
        const conversationId = readString(payload, 'conversation_id')
        const speechId = readString(payload, 'speech_id')
        const key = conversationId && speechId ? speechKey(conversationId, speechId) : null
        if (!key || !terminalPlaybackKeys.has(key)
          || (lastPlayback.value?.conversation_id === conversationId && lastPlayback.value.speech_id === speechId)) {
          recordPlaybackResult(payload, resultFromCompletion(readString(payload, 'status')))
        } else if (conversationId && speechId) {
          removePlaybackItem(conversationId, speechId)
        }
      }
      return
    }
    if (message.type === 'speech.delta') {
      const textDelta = readString(payload, 'text_delta')
      const item = readPlaybackItem(payload, 'waiting')
      if (textDelta !== null && item
        && (!agentEpisode.value?.conversation_id || item.conversation_id === agentEpisode.value.conversation_id)) {
        const key = speechKey(item.conversation_id, item.speech_id)
        const existing = playbackQueue.value.find(candidate => speechKey(candidate.conversation_id, candidate.speech_id) === key)
        upsertPlaybackItem({
          ...item,
          text: `${existing?.text ?? ''}${textDelta}`,
          state: existing?.state ?? 'waiting',
        })
      }
      if (textDelta !== null && acceptsCurrentTurn(message.turn_id ?? readString(payload, 'turn_id'))) {
        current.assistant_text += textDelta
      }
      return
    }
    if (message.type === 'speech.progress') {
      const currentAgent = agentEpisode.value
      if (typeof payload.speech_id === 'string' && typeof payload.conversation_id === 'string'
        && payload.conversation_id === currentAgent?.conversation_id
        && !speechOutputs.value.some(item => item.speech_id === payload.speech_id)) speechProgress.value = payload
      const trigger = readString(payload, 'trigger')
      const status = readString(payload, 'status')
      const progressEvent = readString(payload, 'event')
      if (trigger === 'playback_finished' && status === 'FINISHED') {
        recordPlaybackResult(payload, 'finished')
      } else if (trigger === 'playback_finished' && status === 'DEGRADED') {
        recordPlaybackResult(payload, 'failed')
      } else if (progressEvent === 'speech.replaced') {
        const oldSpeechId = readString(payload, 'old_speech_id') ?? readString(payload, 'speech_id')
        const conversationId = readString(payload, 'conversation_id')
        if (oldSpeechId && conversationId) recordPlaybackResult({ ...payload, speech_id: oldSpeechId }, 'replaced')
      }
    }
    if (message.type === 'input.started') adoptTurn(message.turn_id ?? null)
    if (message.turn_id === currentTurnId.value && isRecord(payload.workspace)) {
      current.input_runtime = true
      applyWorkspace(payload.workspace)
    }

    switch (message.type) {
      case 'runtime.snapshot':
        applyRuntimeSnapshot(payload as unknown as RuntimeSnapshotPayload, message.turn_id ?? null)
        break
      case 'session.state':
        applySessionState(payload as unknown as SessionStatePayload, message.turn_id ?? null)
        break
      case 'input.updated':
        if (acceptsCurrentTurn(message.turn_id ?? null)) {
          const text = readString(payload, 'text')
          if (text !== null) current.partial_text = text
        }
        break
      case 'input.finalized':
        if (acceptsCurrentTurn(message.turn_id ?? null)) {
          const text = readString(payload, 'text')
          if (text !== null) {
            if (readString(payload, 'source') === 'sensevoice') {
              current.refined_text = text
              current.refinement_available = true
            } else {
              current.final_text = text
              current.partial_text = ''
            }
          }
        }
        break
      case 'context.snapshot':
        applyLlmContext(payload, message.turn_id ?? null)
        break
      case 'speech.progress':
        if (acceptsCurrentTurn(message.turn_id ?? null)) {
          const progressEvent = readString(payload, 'event')
          const waitingText = readString(payload, 'waiting_text')
          const status = readString(payload, 'status')
          const bufferMs = payload.audio_buffer_ms
          const returnedMs = payload.audio_returned_ms
          const playedMs = payload.audio_played_ms
          const outputEstimatedMs = payload.audio_output_estimated_ms
          const remainingMs = payload.audio_remaining_ms
          if (progressEvent === 'tts.prewarm') {
            if (status !== null) current.tts_prewarm_status = status
          } else {
            if (waitingText !== null) current.tts_text = waitingText
            if (status !== null) current.tts_status = status
          }
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
        if (acceptsCurrentTurn(message.turn_id ?? null)) {
          applyLatencySnapshot(payload as unknown as LatencySnapshotPayload)
        }
        break
      case 'input.completed':
        applyCompletedTurn(payload as unknown as TurnCompletedPayload, message.turn_id ?? null)
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
    playbackQueue.value = []
    lastPlayback.value = null
    speechProgress.value = null
    terminalPlaybackKeys.clear()
    llmContext.value = []
    llmContextTurnId.value = null
    return sendCommand('session.reset')
  }

  function setReasoningMode(mode: ReasoningMode) {
    reasoningMode.value = mode
    return sendCommand('session.reasoning_mode', { mode })
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
    reasoningMode,
    currentTurnId,
    current,
    currentLatency,
    currentLatencyComplete,
    services,
    recentTurns,
    agentEpisode,
    agentTrace,
    speechOutputs,
    playbackQueue,
    lastPlayback,
    activeSpeech,
    speechProgress,
    reasoning,
    speakPreview,
    llmContext,
    llmContextTurnId,
    lastServerTimestamp,
    lastSequence,
    webSocketUrl,
    connect,
    toggleListening,
    resetSession,
    setReasoningMode,
    dispose,
  }
}

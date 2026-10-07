import { computed, reactive, ref, shallowRef, watch } from 'vue'
import type {
  AgentEpisodeSnapshot,
  AgentTrace,
  ConversationEntry,
  ConversationSnapshotPayload,
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
  ShadowActivity,
  ShadowProductSnapshot,
  ShadowTimelineItem,
  SessionStatePayload,
  TerminalState,
  TurnDelivery,
  TtsStatusPayload,
  TurnCompletedPayload,
  InputRecord,
  ReasoningMode,
  VoiceReplayFixture,
} from '../types'
import { TERMINAL_STATES } from '../types'
import { ConversationLog, readConversationEntry } from '../conversation-log'
import { isRecord } from '../record'
import { MAX_SHADOW_ACTIVITIES, isNonNegativeInteger, isAgentTrace, isSpeechOutput, isTerminalState, readString, readDelivery, createEmptyCurrentTurn, createEmptyLatency, createOfflineStatuses, createEmptyShadowProduct, readShadowActivity, readShadowProduct, parseServerMessage, copyLatencyValues } from './voiceTerminalProjection'

const OPEN_READY_STATE = 1
const MAX_RECENT_TURNS = 10
const PERCEPTUAL_LATENCY_TICK_MS = 50

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
  /** Injectable only for deterministic tests; production uses a monotonic clock. */
  now?: () => number
}

export function useVoiceTerminal(options: UseVoiceTerminalOptions = {}) {
  const socket = shallowRef<VoiceSocket | null>(null)
  const connection = ref<ConnectionState>('offline')
  const connectionError = ref('')
  const state = ref<TerminalState>('STARTING')
  const listening = ref(false)
  const canStart = ref(false)
  const wakeAlwaysOn = ref(false)
  const inputOwner = ref<'none' | 'kws' | 'asr'>('none')
  const quietHours = reactive({ passive_quiet: false, user_interaction: false, within_quiet_hours: false })
  const reasoningMode = ref<ReasoningMode>('fast') // reasoningMode（运行模式）：默认快速。
  const currentTurnId = ref<string | null>(null)
  const current = reactive<CurrentTurnSnapshot>(createEmptyCurrentTurn())
  const currentLatency = reactive<LatencyValues>(createEmptyLatency())
  const currentLatencyComplete = ref(false)
  const services = reactive<ServiceStatuses>(createOfflineStatuses())
  const shadowProduct = ref<ShadowProductSnapshot>(createEmptyShadowProduct())
  const recentTurns = ref<InputRecord[]>([])
  const audioLevels = shallowRef<readonly number[]>([0, 0, 0, 0, 0]) // audioLevels（实际五频段音量）。
  let audioGeneration = -1 // audioGeneration（已确认的音频轮代）。
  let audioExpiry: ReturnType<typeof setTimeout> | undefined
  function clearAudioLevels() {
    if (audioExpiry !== undefined) clearTimeout(audioExpiry)
    audioExpiry = undefined
    audioLevels.value = [0, 0, 0, 0, 0]
  }
  function syncAudioGeneration(payload: { audio_generation?: number }) {
    if (Number.isInteger(payload.audio_generation) && payload.audio_generation !== audioGeneration) {
      clearAudioLevels()
      audioGeneration = payload.audio_generation!
    }
  }
  watch([state, listening, connection], clearAudioLevels, { flush: 'sync' })
  // Server-ordered conversation log.  The rail renders this instead of merging
  // recent inputs with the output projections, so ordering never depends on
  // client-side reconstruction of the chronology.
  const conversationLog = new ConversationLog()
  const conversationEntries = ref<ConversationEntry[]>([])
  const latestConversationSeq = ref(0)

  /** Mirror the log into reactive state after every mutation. */
  function syncConversation() {
    conversationLog.pinActive(currentTurnId.value || '', activeSpeech.value?.speech_id || '')
    conversationEntries.value = conversationLog.entries()
    latestConversationSeq.value = conversationLog.latestSeq()
  }
  const agentEpisode = ref<AgentEpisodeSnapshot | null>(null)
  const agentTrace = ref<AgentTrace[]>([])
  const speechOutputs = ref<SpeechOutputRecord[]>([])
  const playbackQueue = ref<PlaybackQueueItem[]>([])
  const lastPlayback = ref<LastPlaybackResult | null>(null)
  const activeSpeech = computed(() =>
    playbackQueue.value.find(item => item.state === 'playing') ?? playbackQueue.value[0] ?? null,
  )
  // playingSpeechId（实际播放标识）：输入仍在聆听或结束判定时，输出也可能正在播放。
  const playingSpeechId = computed(() => playbackQueue.value.find(item => item.state === 'playing')?.speech_id ?? null)
  watch(playingSpeechId, clearAudioLevels, { flush: 'sync' })
  const speechProgress = ref<Record<string, unknown> | null>(null)
  const reasoning = ref('')
  const speakPreview = ref<SpeakPreview | null>(null)
  const retiredEpisodes = new Set<string>()
  const terminalPlaybackKeys = new Set<string>()
  const perceptualLatencyMs = ref<number | null>(null)
  const perceptualLatencyRunning = ref(false)
  const now = options.now ?? (() => typeof performance !== 'undefined' ? performance.now() : Date.now())
  const pendingShadowActivities: unknown[] = []
  let shadowActivityFrame: number | undefined
  let perceptualLatencyTimer: ReturnType<typeof setInterval> | undefined
  let perceptualLatencyTurnId: string | null = null
  let perceptualLatencyStartedAt: number | null = null
  let perceptualLatencySettled = false

  function clearPerceptualLatencyTimer() {
    if (perceptualLatencyTimer !== undefined) clearInterval(perceptualLatencyTimer)
    perceptualLatencyTimer = undefined
  }

  function updatePerceptualLatency() {
    if (!perceptualLatencyRunning.value || perceptualLatencyStartedAt === null) return
    perceptualLatencyMs.value = Math.max(0, now() - perceptualLatencyStartedAt)
  }

  function resetPerceptualLatency() {
    clearPerceptualLatencyTimer()
    perceptualLatencyMs.value = null
    perceptualLatencyRunning.value = false
    perceptualLatencyTurnId = null
    perceptualLatencyStartedAt = null
    perceptualLatencySettled = false
  }

  function cancelShadowActivityFrame() {
    if (shadowActivityFrame === undefined) return
    if (typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
      window.cancelAnimationFrame(shadowActivityFrame)
    }
    shadowActivityFrame = undefined
  }

  function clearShadowActivityQueue() {
    pendingShadowActivities.length = 0
    cancelShadowActivityFrame()
  }

  function startPerceptualLatency(turnId: string | null) {
    if (!turnId) return
    // A late SenseVoice refinement can arrive after the primary final input;
    // it must not restart a stopwatch that is already measuring this turn.
    if (perceptualLatencyTurnId === turnId
      && (perceptualLatencyRunning.value || perceptualLatencySettled)) return

    resetPerceptualLatency()
    perceptualLatencyTurnId = turnId
    perceptualLatencyStartedAt = now()
    perceptualLatencyMs.value = 0
    perceptualLatencyRunning.value = true
    perceptualLatencyTimer = setInterval(updatePerceptualLatency, PERCEPTUAL_LATENCY_TICK_MS)
  }

  function stopPerceptualLatency(turnId: string | null = null) {
    if (!perceptualLatencyRunning.value || perceptualLatencyStartedAt === null) return
    if (turnId && perceptualLatencyTurnId !== turnId) return
    // Capture the boundary at event handling time rather than waiting for the
    // next interval tick.  This is the value users actually perceive.
    updatePerceptualLatency()
    clearPerceptualLatencyTimer()
    perceptualLatencyRunning.value = false
    perceptualLatencyStartedAt = null
    perceptualLatencySettled = true
  }

  function cancelPerceptualLatency(turnId: string | null = null) {
    if (turnId && perceptualLatencyTurnId !== turnId) return
    if (perceptualLatencyRunning.value) resetPerceptualLatency()
  }

  function preparePerceptualLatencyForInput(turnId: string | null) {
    if (turnId && perceptualLatencyTurnId !== turnId) return
    if (perceptualLatencyRunning.value || perceptualLatencySettled) resetPerceptualLatency()
  }

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

  function upsertSpeechOutput(incoming: SpeechOutputRecord) {
    const key = speechKey(incoming.conversation_id, incoming.speech_id)
    const index = speechOutputs.value.findIndex(item => speechKey(item.conversation_id, item.speech_id) === key)
    if (index < 0) {
      speechOutputs.value = [...speechOutputs.value, incoming].slice(-32)
      return
    }
    const currentOutput = speechOutputs.value[index]
    const next: SpeechOutputRecord = {
      ...currentOutput,
      ...incoming,
      text: incoming.text || currentOutput.text,
      status: incoming.status || currentOutput.status,
      delivery: incoming.delivery ?? currentOutput.delivery,
    }
    speechOutputs.value = speechOutputs.value.map((item, itemIndex) => itemIndex === index ? next : item)
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
    const turnId = readString(payload, 'turn_id')
    const activeItem = playbackQueue.value.find(item => item.speech_id === speechId && item.conversation_id === conversationId)
    if (activeItem?.output_generation !== undefined && isNonNegativeInteger(payload.output_generation)
      && payload.output_generation < activeItem.output_generation) return
    // A terminal failure/replacement without a playback-start event does not
    // have a perceptual response latency.  Clear only a still-running local
    // stopwatch; a value already stopped by speech.started must be preserved.
    cancelPerceptualLatency(turnId ?? activeItem?.turn_id ?? null)
    terminalPlaybackKeys.add(speechKey(conversationId, speechId))
    if (terminalPlaybackKeys.size > 128) terminalPlaybackKeys.delete(terminalPlaybackKeys.values().next().value!)
    const removed = removePlaybackItem(conversationId, speechId)
    const previous = lastPlayback.value?.speech_id === speechId
      && lastPlayback.value.conversation_id === conversationId ? lastPlayback.value : null
    const existingOutput = speechOutputs.value.find(item => speechKey(item.conversation_id, item.speech_id)
      === speechKey(conversationId, speechId))
    const output: SpeechOutputRecord = {
      ...(existingOutput || {}),
      speech_id: speechId,
      conversation_id: conversationId,
      turn_id: turnId ?? removed?.turn_id ?? previous?.turn_id ?? '',
      text: readString(payload, 'text') || removed?.text || previous?.text || existingOutput?.text || '',
      status: result === 'finished' ? 'completed' : result,
    }
    const requestId = readString(payload, 'request_id') ?? removed?.request_id
    if (requestId) output.request_id = requestId
    const traceId = readString(payload, 'trace_id')
    if (traceId) output.trace_id = traceId
    if (isNonNegativeInteger(payload.output_generation)) output.output_generation = payload.output_generation
    else if (removed?.output_generation !== undefined) output.output_generation = removed.output_generation
    const delivery = readDelivery(payload.delivery)
    if (delivery !== undefined) output.delivery = delivery
    const emotionRequested = readString(payload, 'emotion_requested') ?? removed?.emotion_requested
    if (emotionRequested) output.emotion_requested = emotionRequested
    const emotionApplied = readString(payload, 'emotion_applied') ?? removed?.emotion_applied
    if (emotionApplied) output.emotion_applied = emotionApplied
    upsertSpeechOutput(output)
    lastPlayback.value = {
      speech_id: speechId,
      conversation_id: conversationId,
      turn_id: turnId ?? removed?.turn_id ?? previous?.turn_id ?? '',
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
      speechOutputs.value = []
      playbackQueue.value = []
      lastPlayback.value = null
      terminalPlaybackKeys.clear()
    }
    if (value.conversation_id === null) {
      speechProgress.value = null
      speakPreview.value = null
      speechOutputs.value = []
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

  const createSocket = options.webSocketFactory ?? ((url: string) => new WebSocket(url) as unknown as VoiceSocket)
  let requestSequence = 0

  const connectionLabel = computed(() => {
    if (connection.value === 'connected') return 'CONNECTED'
    if (connection.value === 'connecting') return 'CONNECTING'
    return 'OFFLINE'
  })

  function resetCurrentTurn() {
    resetPerceptualLatency()
    Object.assign(current, createEmptyCurrentTurn())
    Object.assign(currentLatency, createEmptyLatency())
    currentLatencyComplete.value = false
  }

  function resetConnectionData() {
    clearAudioLevels()
    clearShadowActivityQueue()
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
    shadowProduct.value = createEmptyShadowProduct()
  }

  function applyShadowProductSnapshot(value: ShadowProductSnapshot) {
    // Flush deltas that arrived before this authoritative message so an
    // animation-frame batch cannot overwrite the snapshot out of order.
    flushShadowActivities()
    // A product snapshot is authoritative on reconnect.  Keep the activity
    // list bounded and never let an older backend snapshot resurrect a live
    // run that has already been reset.
    shadowProduct.value = {
      ...value,
      activities: value.activities
        ? value.activities.slice(-MAX_SHADOW_ACTIVITIES)
        : (shadowProduct.value.activities || []).slice(-MAX_SHADOW_ACTIVITIES),
    }
  }

  function mergeShadowActivity(value: unknown) {
    const incoming = readShadowActivity(value)
    if (!incoming) return
    const existingActivities = shadowProduct.value.activities || []
    const latestRun = existingActivities.reduce<number | null>(
      (currentRun, item) => currentRun === null ? item.run_seq : Math.max(currentRun, item.run_seq),
      null,
    )
    if (latestRun !== null && incoming.run_seq < latestRun) return
    const activities = incoming.run_seq > (latestRun ?? -1)
      ? existingActivities.filter(item => item.phase === 'completed' || item.phase === 'failed').slice(-32)
      : [...existingActivities]
    const index = activities.findIndex(item => item.run_seq === incoming.run_seq && item.item_id === incoming.item_id)
    if (index >= 0) {
      const current = activities[index]
      // A completed/failed item is final.  Provider retries get a new local
      // item id, so a late delta can never splice itself into the retry.
      if ((current.phase === 'completed' || current.phase === 'failed')
        && incoming.phase !== current.phase) return
      const merged: ShadowActivity = { ...current, ...incoming }
      if (incoming.phase === 'delta') {
        if (incoming.delta) merged.text = `${current.text || ''}${incoming.delta}`.slice(0, 4000)
        delete merged.delta
      }
      activities[index] = merged
    } else {
      const item = { ...incoming }
      if (item.phase === 'delta' && item.delta) {
        item.text = item.delta.slice(0, 4000)
        delete item.delta
      }
      activities.push(item)
    }
    shadowProduct.value = {
      ...shadowProduct.value,
      activities: activities.slice(-MAX_SHADOW_ACTIVITIES),
      last_activity_at: incoming.timestamp,
    }
  }

  function flushShadowActivities() {
    cancelShadowActivityFrame()
    const queued = pendingShadowActivities.splice(0)
    for (const value of queued) mergeShadowActivity(value)
  }

  function queueShadowActivity(value: unknown) {
    // Vitest and SSR do not provide requestAnimationFrame.  Keep those paths
    // synchronous for deterministic consumers; browsers get one DOM update
    // per frame while preserving the server sequence inside the queue.
    if (typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
      mergeShadowActivity(value)
      return
    }
    pendingShadowActivities.push(value)
    if (shadowActivityFrame !== undefined) return
    shadowActivityFrame = window.requestAnimationFrame(() => {
      shadowActivityFrame = undefined
      const queued = pendingShadowActivities.splice(0)
      for (const item of queued) mergeShadowActivity(item)
    })
  }

  function injectReplay(fixture: VoiceReplayFixture) {
    // This is deliberately a local development hook.  It fills the same
    // reactive stores used by the WebSocket protocol so the Product UI can be
    // inspected with a recorded session without changing the backend.
    connection.value = 'connected'
    connectionError.value = ''
    clearShadowActivityQueue()
    resetPerceptualLatency()
    state.value = fixture.state
    listening.value = fixture.listening
    canStart.value = fixture.canStart ?? true
    wakeAlwaysOn.value = fixture.wakeAlwaysOn ?? false
    inputOwner.value = fixture.inputOwner ?? 'none'
    reasoningMode.value = fixture.reasoningMode ?? 'fast'
    currentTurnId.value = fixture.current.turn_id ?? null
    Object.assign(current, createEmptyCurrentTurn(), fixture.current)
    Object.assign(currentLatency, createEmptyLatency(), fixture.currentLatency || {})
    currentLatencyComplete.value = fixture.currentLatencyComplete ?? true
    Object.assign(services, createOfflineStatuses(), fixture.services || {})
    recentTurns.value = fixture.recentInputs.map(item => ({ ...item }))
    shadowProduct.value = {
      ...fixture.shadow,
      timeline: fixture.shadow.timeline.map(item => ({ ...item })),
      activities: (fixture.shadow.activities || []).map(item => ({ ...item })),
      context: { ...(fixture.shadow.context || {}) },
      provider: { ...(fixture.shadow.provider || {}) },
    }
    agentEpisode.value = null
    agentTrace.value = []
    speechOutputs.value = []
    playbackQueue.value = []
    lastPlayback.value = null
    speechProgress.value = null
    speakPreview.value = null
    terminalPlaybackKeys.clear()
    retiredEpisodes.clear()
    if (fixture.agent) applyAgent(fixture.agent, true)
    if (fixture.agentTrace) agentTrace.value = fixture.agentTrace.map(item => ({ ...item }))
    if (fixture.speechOutputs) speechOutputs.value = fixture.speechOutputs.map(item => ({ ...item }))
    if (fixture.playbackQueue) playbackQueue.value = fixture.playbackQueue.map(item => ({ ...item }))
    if (fixture.lastPlayback) lastPlayback.value = { ...fixture.lastPlayback }
    reasoning.value = fixture.reasoning || ''
    // The fixture predates the server-ordered conversation, so its recorded
    // turns are converted once, in the fixture's own order.  Nothing is
    // ordered at render time.
    conversationLog.applySnapshot({
      entries: replayConversationEntries(fixture.recentInputs),
      latest_seq: (fixture.recentInputs?.length || 0) * 2,
    })
    syncConversation()
  }

  /**
   * Convert a recorded fixture into the ordered conversation shape.
   *
   * The fixture is a recording of the older projections (newest-first turns,
   * each carrying its own response text), so the conversion happens once when
   * the fixture is injected — never while rendering.
   */
  function replayConversationEntries(records: InputRecord[] | undefined): ConversationEntry[] {
    const entries: ConversationEntry[] = []
    let seq = 0
    for (const turn of [...(records || [])].reverse()) {
      seq += 1
      entries.push({
        seq,
        kind: 'user_turn',
        conversation_id: turn.conversation_id || 'conversation_replay',
        turn_id: turn.turn_id,
        speech_id: '',
        text: turn.transcript,
        status: turn.status || 'completed',
        delivery: turn.delivery || {},
        termination: turn.status || 'completed',
        at: turn.completed_at,
      })
      if (!turn.response) continue
      seq += 1
      entries.push({
        seq,
        kind: 'assistant_speech',
        conversation_id: turn.conversation_id || 'conversation_replay',
        turn_id: turn.turn_id,
        speech_id: `${turn.turn_id}-reply`,
        text: turn.response,
        status: 'completed',
        delivery: { status: 'full', termination: 'completed' },
        termination: 'completed',
        at: turn.completed_at,
      })
    }
    return entries
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
    if (isRecord(snapshot.speaker_decision)) current.speaker_decision = snapshot.speaker_decision as NonNullable<CurrentTurnSnapshot['speaker_decision']>
  }

  function applySpeakerDecision(payload: Record<string, unknown>, turnId: string | null) {
    const candidate = isRecord(payload.decision)
      ? payload.decision
      : isRecord(payload.state) ? payload.state : payload
    if (!isRecord(candidate)) return
    const candidateTurn = typeof candidate.turn_id === 'string' ? candidate.turn_id : turnId
    if (!acceptsCurrentTurn(candidateTurn ?? null)) return
    const candidateGeneration = candidate.generation
    const currentGeneration = current.output_generation
    if (typeof candidateGeneration === 'number' && typeof currentGeneration === 'number'
      && candidateGeneration < currentGeneration) return
    current.speaker_decision = candidate as NonNullable<CurrentTurnSnapshot['speaker_decision']>
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
    syncAudioGeneration(payload)
    if (payload.quiet_hours) Object.assign(quietHours, payload.quiet_hours)
    if (isTerminalState(payload.state)) state.value = payload.state
    if (typeof payload.listening === 'boolean') listening.value = payload.listening
    if (typeof payload.can_start === 'boolean') canStart.value = payload.can_start
    if (typeof payload.wake_always_on === 'boolean') wakeAlwaysOn.value = payload.wake_always_on
    if (payload.input_owner === 'none' || payload.input_owner === 'kws' || payload.input_owner === 'asr') {
      inputOwner.value = payload.input_owner
    }
    if (payload.reasoning_mode === 'fast' || payload.reasoning_mode === 'deep') reasoningMode.value = payload.reasoning_mode
    applyServices(payload.services)
    const hasCurrentTurnId = isRecord(payload.current)
      && Object.prototype.hasOwnProperty.call(payload.current, 'turn_id')
    const snapshotTurnId = isRecord(payload.current) ? readString(payload.current, 'turn_id') : null
    if (turnId || snapshotTurnId) adoptTurn(turnId || snapshotTurnId)
    else if (hasCurrentTurnId && currentTurnId.value !== null) resetConnectionData()
    applyCurrentSnapshot(payload.current)
    if (payload.agent) applyAgent(payload.agent, true)
    if (payload.latency) applyLatencySnapshot(payload.latency)
    const shadow = readShadowProduct(payload.shadow)
    if (shadow) applyShadowProductSnapshot(shadow)
    if (Array.isArray(payload.recent_inputs)) {
      recentTurns.value = (payload.recent_inputs.filter(item => isRecord(item) && typeof item.turn_id === 'string') as InputRecord[])
        .slice(0, MAX_RECENT_TURNS).map(item => ({
          ...item,
          latency: copyLatencyValues(item.latency),
          delivery: readDelivery(item.delivery),
        }))
    }
    // A snapshot without entries only re-states the head position; the ordered
    // conversation itself is never replaced by an unrelated state transition.
    if (payload.conversation) {
      conversationLog.applySnapshot(payload.conversation)
      syncConversation()
    }
  }

  function applySessionState(payload: SessionStatePayload, turnId: string | null) {
    syncAudioGeneration(payload)
    if (isTerminalState(payload.state)) state.value = payload.state
    if (typeof payload.listening === 'boolean') listening.value = payload.listening
    if (typeof payload.can_start === 'boolean') canStart.value = payload.can_start
    if (turnId) adoptTurn(turnId)
    if (payload.state === 'FINALIZING') {
      // This is the earliest public end-of-input boundary.  The clock is
      // local to the browser, so later backend latency snapshots cannot move
      // or delay the displayed perceptual measurement.
      startPerceptualLatency(turnId ?? currentTurnId.value)
    }
    if (payload.listening === false && payload.state === 'IDLE') {
      // Stopping the microphone is not an output cancellation boundary. The
      // LLM/TTS queue is an independent FIFO and must remain visible and
      // continue settling after input capture has stopped.
      cancelPerceptualLatency()
    }
  }

  function applyCompletedTurn(payload: TurnCompletedPayload, turnId: string | null) {
    if (!turnId) return
    cancelPerceptualLatency(turnId)
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
    const payload = isRecord(message.payload) ? message.payload : {}
    if (message.type === 'audio.levels') {
      // source（来源）、levels（音量值）、generation（轮代）必须与实际状态一致。
      const eligible = listening.value && (
        (state.value === 'LISTENING' && !playingSpeechId.value && payload.source === 'input')
        || ((playingSpeechId.value !== null || state.value === 'SPEAKING') && payload.source === 'playback'))
      if (!eligible || payload.generation !== audioGeneration || !Array.isArray(payload.levels)
        || payload.levels.length !== 5 || !payload.levels.every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1)) return
      if (audioExpiry !== undefined) clearTimeout(audioExpiry)
      audioLevels.value = [...payload.levels]
      audioExpiry = setTimeout(clearAudioLevels, 300)
      return
    }
    lastServerTimestamp.value = message.timestamp
    if (message.type === 'conversation.snapshot') {
      conversationLog.applySnapshot(payload as ConversationSnapshotPayload)
      syncConversation()
      return
    }
    if (message.type === 'conversation.append') {
      const entry = readConversationEntry(payload, message.turn_id ?? null)
      if (entry) {
        conversationLog.applyEntry(entry)
        syncConversation()
      }
      return
    }
    // Agent/output generations are independent of the currently displayed input turn.
    if (message.type === 'agent.state') { applyAgent(payload); return }
    if (message.type === 'agent.trace') { applyAgentTrace(payload); return }
    if (message.type === 'agent.reasoning') { if (typeof payload.text === 'string') reasoning.value = payload.text; return }
    if (message.type === 'shadow.product') {
      const shadow = readShadowProduct(payload)
      if (shadow) applyShadowProductSnapshot(shadow)
      return
    }
    if (message.type === 'shadow.activity') {
      queueShadowActivity(payload)
      return
    }
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
        stopPerceptualLatency(item.turn_id || message.turn_id || readString(payload, 'turn_id'))
      }
      return
    }
    if (message.type === 'speech.completed') {
      if (isSpeechOutput(payload)) {
        upsertSpeechOutput(payload)
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
    if (message.type === 'input.started') {
      // input.started also covers a resumed same-turn input.  Resetting here
      // lets the next finalized utterance get its own response measurement.
      resetPerceptualLatency()
      adoptTurn(message.turn_id ?? null)
    }
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
          // A new partial after playback (same turn continuation) begins a
          // new measurement window; ordinary partials before finalization do
          // not affect an idle stopwatch.
          preparePerceptualLatencyForInput(message.turn_id ?? null)
          const text = readString(payload, 'text')
          if (text !== null) current.partial_text = text
        }
        break
      case 'input.finalized':
        if (acceptsCurrentTurn(message.turn_id ?? null)) {
          // FINALIZING normally starts the timer earlier.  Keep this as a
          // compatibility fallback for servers that only send transcript
          // finalization, and make late refinements a no-op for the same turn.
          startPerceptualLatency(message.turn_id ?? readString(payload, 'turn_id'))
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
      case 'speaker.state':
      case 'speaker.decision':
      case 'speaker.blocked':
        applySpeakerDecision(payload, message.turn_id ?? null)
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
    if (typeof window !== 'undefined' && window.__VOICE_APPLICATION__?.business_ready === false) {
      connectionError.value = '应用宿主尚未允许业务连接。'
      return
    }

    resetConnectionData()
    connection.value = 'connecting'
    connectionError.value = ''
    state.value = 'STARTING'
    listening.value = false
    canStart.value = false
    wakeAlwaysOn.value = false
    inputOwner.value = 'none'
    lastSequence.value = null

    try {
      const runtimeUrl = typeof window !== 'undefined' ? window.__VOICE_APPLICATION__?.voice_ws_url : undefined
      const configuredUrl = options.webSocketUrl ?? runtimeUrl
      if (!configuredUrl) throw new Error('应用宿主尚未提供语音实时通信地址。')
      const nextSocket = createSocket(configuredUrl)
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
    clearShadowActivityQueue()
    shadowProduct.value = createEmptyShadowProduct()
    resetCurrentTurn()
    speechOutputs.value = []
    playbackQueue.value = []
    lastPlayback.value = null
    speechProgress.value = null
    terminalPlaybackKeys.clear()
    llmContext.value = []
    llmContextTurnId.value = null
    // The server publishes the empty conversation right after a reset; clear it
    // locally as well so the rail cannot show a superseded session meanwhile.
    conversationLog.clear()
    syncConversation()
    return sendCommand('session.reset')
  }

  function setReasoningMode(mode: ReasoningMode) {
    reasoningMode.value = mode
    return sendCommand('session.reasoning_mode', { mode })
  }

  function disconnect() {
    const activeSocket = socket.value
    socket.value = null
    activeSocket?.close()
    connection.value = 'offline'
    connectionError.value = ''
    state.value = 'STARTING'
    listening.value = false
    canStart.value = false
    resetConnectionData()
    Object.assign(services, createOfflineStatuses())
  }

  function dispose() { disconnect() }

  return {
    connection,
    audioLevels,
    connectionError,
    connectionLabel,
    state,
    listening,
    canStart,
    wakeAlwaysOn,
    inputOwner,
    quietHours,
    reasoningMode,
    currentTurnId,
    current,
    currentLatency,
    currentLatencyComplete,
    perceptualLatencyMs,
    perceptualLatencyRunning,
    services,
    recentTurns,
    conversationEntries,
    latestConversationSeq,
    agentEpisode,
    shadowProduct,
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
    connect,
    disconnect,
    injectReplay,
    toggleListening,
    resetSession,
    setReasoningMode,
    dispose,
  }
}

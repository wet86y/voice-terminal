import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useVoiceTerminal } from '../composables/useVoiceTerminal'
import type { ReasoningMode, ShadowProductSnapshot } from '../types'
import { productApi, ProductApiError } from './api'
import type {
  ProductAssignments,
  ProductModel,
  ProductModelRegistry,
  ProductMutationResult,
  ProductRuntimeState,
  ProductSettingsSnapshot,
  ProductSnapshot,
  ProductTtsProfile,
} from './types'
import { voiceShadowReplayFixture, voiceShadowReplayMeta } from './replay-data'

export type ProductSettingsTab = 'wake' | 'persona' | 'silence' | 'speaker' | 'models'
export type ProductPage = 'home' | 'settings'
export type AssignmentSlot = 'voice' | 'shadow' | 'status' | 'tts'

const NOTICE_DISPLAY_MS = 5000
const NOTICE_FADE_MS = 420
const SHADOW_REASONING_LEVELS = ['off', 'low', 'medium', 'high', 'max']

export interface ProductUiModel extends ProductModel {
  provider: string
  modelId: string
  path: string
  status: string
  maxContextTokens?: number
  allowedReasoning?: string[]
}

export interface ProviderFormState {
  name: string
  baseUrl: string
  protocol: 'openai_compatible' | 'deepseek' | 'opencode_go' | 'responses' | 'minimax' | 'manual'
  apiKey: string
  customModelId: string
  maxContextTokens: number
  allowedReasoning: string[]
  voiceId: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

const DEFAULT_SETTINGS: ProductSettingsSnapshot = {
  schema_version: 3,
  wake: { enabled: false, keyword: 'hei claw', keyword_label: '', sensitivity: 75 },
  silence: { seconds: 30, smart_turn_hard_silence_ms: 1400 },
  shadow: { experimental_context_gate_enabled: false },
  speaker: { mode: 'off', confirm_threshold: 0.5, continuity_threshold: 0.4 },
  persona: { instructions: '', personality: '', user: '', shadow: '' },
  warnings: [],
}

const DEFAULT_REGISTRY: ProductModelRegistry = {
  providers: [], models: [], tts_profiles: [], assignments: {},
}

function defaultCurrent() {
  return {
    partial_text: '', final_text: '', assistant_text: '', tts_text: '', tts_status: '',
  }
}

function defaultShadow(): ShadowProductSnapshot {
  return {
    available: false, active: false, phase: 'unavailable', user_input_required: false,
    focus: '', summary: '', timeline: [], activities: [], context: {}, provider: {},
  }
}

function normalizeReasoningEffort(value: unknown, fallback = 'off') {
  const normalized = typeof value === 'string' ? value.trim().toLowerCase() : ''
  return normalized === 'none' ? 'off' : SHADOW_REASONING_LEVELS.includes(normalized) ? normalized : fallback
}

function normalizeSettings(value: unknown): ProductSettingsSnapshot {
  if (!isRecord(value)) return structuredClone(DEFAULT_SETTINGS)
  const wake = isRecord(value.wake) ? value.wake : {}
  const silence = isRecord(value.silence) ? value.silence : {}
  const shadow = isRecord(value.shadow) ? value.shadow : {}
  const speaker = isRecord(value.speaker) ? value.speaker : {}
  const persona = isRecord(value.persona) ? value.persona : {}
  const text = (candidate: unknown) => typeof candidate === 'string' ? candidate : ''
  return {
    schema_version: typeof value.schema_version === 'number' ? value.schema_version : DEFAULT_SETTINGS.schema_version,
    wake: {
      enabled: wake.enabled === true,
      keyword: text(wake.keyword) || DEFAULT_SETTINGS.wake.keyword,
      keyword_label: text(wake.keyword_label),
      sensitivity: typeof wake.sensitivity === 'number' ? wake.sensitivity : DEFAULT_SETTINGS.wake.sensitivity,
    },
    silence: {
      seconds: typeof silence.seconds === 'number' ? silence.seconds : 30,
      smart_turn_hard_silence_ms: typeof silence.smart_turn_hard_silence_ms === 'number'
        ? silence.smart_turn_hard_silence_ms : 1400,
    },
    shadow: {
      experimental_context_gate_enabled: shadow.experimental_context_gate_enabled === true,
    },
    speaker: {
      mode: typeof speaker.mode === 'string' && ['off', 'observe', 'filter'].includes(speaker.mode)
        ? speaker.mode as ProductSettingsSnapshot['speaker']['mode'] : 'off',
      confirm_threshold: typeof speaker.confirm_threshold === 'number' ? speaker.confirm_threshold : 0.5,
      continuity_threshold: typeof speaker.continuity_threshold === 'number' ? speaker.continuity_threshold : 0.4,
    },
    persona: {
      instructions: text(persona.instructions),
      personality: text(persona.personality),
      user: text(persona.user),
      shadow: text(persona.shadow),
    },
    warnings: Array.isArray(value.warnings) ? value.warnings.filter(item => typeof item === 'string') : [],
  }
}

function normalizeRegistry(value: unknown): ProductModelRegistry {
  if (!isRecord(value)) return structuredClone(DEFAULT_REGISTRY)
  const models = Array.isArray(value.models) ? value.models.filter(isRecord).map(item => ({
    ...item,
    allowed_reasoning: Array.isArray(item.allowed_reasoning)
      ? [...new Set(item.allowed_reasoning.map(level => normalizeReasoningEffort(level)).filter(level => level))]
      : item.allowed_reasoning,
  })) : []
  return {
    credential_vault: isRecord(value.credential_vault)
      ? { available: value.credential_vault.available === true, diagnostic: typeof value.credential_vault.diagnostic === 'string' ? value.credential_vault.diagnostic : null }
      : undefined,
    providers: Array.isArray(value.providers) ? value.providers.filter(isRecord) as ProductModelRegistry['providers'] : [],
    models: models as ProductModelRegistry['models'],
    tts_profiles: Array.isArray(value.tts_profiles) ? value.tts_profiles.filter(isRecord) as ProductModelRegistry['tts_profiles'] : [],
    assignments: isRecord(value.assignments) ? value.assignments as ProductAssignments : {},
  }
}

function normalizeRuntimeState(value: string | undefined, connected: boolean): ProductRuntimeState {
  if (!connected) return 'offline'
  if (value === 'ERROR') return 'error'
  if (value === 'LISTENING' || value === 'FINALIZING' || value === 'INTERRUPTING') return 'listening'
  if (value === 'THINKING') return 'thinking'
  if (value === 'SPEAKING') return 'speaking'
  return 'standby'
}

function messageFromError(error: unknown) {
  if (error instanceof ProductApiError) return error.message
  if (error instanceof Error) return error.message
  return '产品请求失败。'
}

function updateSettingsFromResponse(settings: ProductSettingsSnapshot, result: ProductMutationResult) {
  const candidate: unknown = result.settings
  if (!isRecord(candidate)) return
  if (isRecord(candidate.wake)) Object.assign(settings.wake, candidate.wake)
  if (isRecord(candidate.silence)) Object.assign(settings.silence, candidate.silence)
  if (isRecord(candidate.shadow)) Object.assign(settings.shadow, candidate.shadow)
  if (isRecord(candidate.speaker)) Object.assign(settings.speaker, candidate.speaker)
  if (isRecord(candidate.persona)) Object.assign(settings.persona, candidate.persona)
  if (Array.isArray(candidate.warnings)) settings.warnings = candidate.warnings.filter((item): item is string => typeof item === 'string')
}

export function useProductAdapter(options: { webSocketUrl?: string } = {}) {
  const voice = useVoiceTerminal(options)
  const replayMode = import.meta.env.DEV
    && typeof window !== 'undefined'
    && new URLSearchParams(window.location.search).get('replay') === 'voice-shadow'
  const page = ref<ProductPage>('home')
  const settingsTab = ref<ProductSettingsTab>('wake')
  const snapshot = ref<ProductSnapshot | null>(null)
  const settings = reactive<ProductSettingsSnapshot>(structuredClone(DEFAULT_SETTINGS))
  const registry = ref<ProductModelRegistry>(structuredClone(DEFAULT_REGISTRY))
  const loading = ref(false)
  const saving = ref(false)
  const lastError = ref('')
  const notice = ref('')
  const noticeVisible = ref(false)
  const shadowExpanded = ref(false)
  const personaDocument = ref<keyof ProductSettingsSnapshot['persona']>('instructions')
  const silenceSeconds = ref(30)
  const modelScanState = ref<'idle' | 'scanning' | 'complete'>('idle')
  const providerStep = ref<'idle' | 'discovering' | 'select-model' | 'custom-id' | 'verified'>('idle')
  const speakerForm = reactive({ mode: 'off', confirm_threshold: 0.5, continuity_threshold: 0.4 })
  const speakerStatus = ref<Record<string, unknown>>({})
  const speakerProfile = ref<Record<string, unknown>>({})
  const speakerTestResult = ref<Record<string, unknown> | null>(null)
  const ambientContext = ref<Record<string, unknown>>({ records: [] })
  const speakerEnrollment = ref<{ id: string; segments: number[] } | null>(null)
  const discoveredModels = ref<string[]>([])
  const restartState = ref<'idle' | 'restarting' | 'complete' | 'error'>('idle')
  const applyImpact = ref<string[]>([])

  const wakeForm = reactive({ enabled: false, keyword: 'hei claw', sensitivity: 75 })
  const personaDocuments = reactive<Record<keyof ProductSettingsSnapshot['persona'], string>>({
    instructions: '', personality: '', user: '', shadow: '',
  })
  const providerForm = reactive<ProviderFormState>({
    name: '', baseUrl: 'https://api.example.com/v1', protocol: 'openai_compatible',
    apiKey: '', customModelId: '', maxContextTokens: 131072,
    allowedReasoning: ['off', 'low'], voiceId: 'female-shaonv',
  })
  // The template can toggle policy chips directly.  Keep the request
  // contract valid even when the user removes the last visible chip.
  watch(
    () => providerForm.allowedReasoning.slice(),
    values => {
      if (!values.length) providerForm.allowedReasoning = ['off']
    },
    { flush: 'sync' },
  )
  const modelAssignments = reactive<Record<AssignmentSlot, string>>({
    voice: '', shadow: '', status: '', tts: '',
  })

  let snapshotRequest: Promise<void> | null = null
  let reconnectTimer: number | undefined
  let replayTimer: number | undefined
  let noticeDisplayTimer: ReturnType<typeof setTimeout> | undefined
  let noticeFadeTimer: ReturnType<typeof setTimeout> | undefined
  let mutationQueue: Promise<unknown> = Promise.resolve()

  function enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const next = mutationQueue.then(operation, operation)
    mutationQueue = next.then(() => undefined, () => undefined)
    return next
  }

  function clearNoticeTimers() {
    if (noticeDisplayTimer !== undefined) clearTimeout(noticeDisplayTimer)
    if (noticeFadeTimer !== undefined) clearTimeout(noticeFadeTimer)
    noticeDisplayTimer = undefined
    noticeFadeTimer = undefined
  }

  function setNotice(value: string) {
    clearNoticeTimers()
    lastError.value = ''
    if (!value) {
      notice.value = ''
      noticeVisible.value = false
      return
    }
    notice.value = value
    noticeVisible.value = true
    noticeDisplayTimer = setTimeout(() => {
      noticeDisplayTimer = undefined
      noticeVisible.value = false
      noticeFadeTimer = setTimeout(() => {
        notice.value = ''
        noticeFadeTimer = undefined
      }, NOTICE_FADE_MS)
    }, NOTICE_DISPLAY_MS)
  }

  function setError(error: unknown) {
    clearNoticeTimers()
    lastError.value = messageFromError(error)
    notice.value = ''
    noticeVisible.value = false
  }

  function applySnapshot(value: ProductSnapshot) {
    snapshot.value = value
    const nextSettings = normalizeSettings(value.settings)
    Object.assign(settings.wake, nextSettings.wake)
    Object.assign(settings.silence, nextSettings.silence)
    Object.assign(settings.shadow, nextSettings.shadow)
    Object.assign(settings.speaker, nextSettings.speaker)
    Object.assign(settings.persona, nextSettings.persona)
    settings.schema_version = nextSettings.schema_version
    settings.warnings = nextSettings.warnings
    Object.assign(wakeForm, {
      enabled: nextSettings.wake.enabled,
      keyword: nextSettings.wake.keyword,
      sensitivity: nextSettings.wake.sensitivity,
    })
    silenceSeconds.value = nextSettings.silence.seconds
    Object.assign(speakerForm, nextSettings.speaker)
    Object.assign(personaDocuments, nextSettings.persona)
    registry.value = normalizeRegistry(value.models)
    syncAssignments(registry.value.assignments)
  }

  function applyRegistry(value: unknown) {
    registry.value = normalizeRegistry(value)
    syncAssignments(registry.value.assignments)
  }

  function syncAssignments(assignments: ProductAssignments) {
    const values: Record<AssignmentSlot, unknown> = {
      voice: assignments.voice_llm_model_id,
      shadow: assignments.shadow_llm_model_id,
      status: assignments.status_llm_model_id,
      tts: assignments.tts_profile_id,
    }
    for (const slot of Object.keys(values) as AssignmentSlot[]) {
      modelAssignments[slot] = typeof values[slot] === 'string' ? values[slot] : ''
    }
  }

  async function loadSnapshot() {
    if (replayMode) return
    if (snapshotRequest) return snapshotRequest
    loading.value = true
    snapshotRequest = productApi.snapshot().then(async value => {
      applySnapshot(value)
      await refreshSpeaker()
      lastError.value = ''
    }).catch(setError).finally(() => {
      loading.value = false
      snapshotRequest = null
    })
    return snapshotRequest
  }

  async function refreshSpeaker() {
    if (replayMode) return
    try {
      const [status, profile, ambient] = await Promise.all([
        productApi.speakerStatus(), productApi.speakerProfile(), productApi.ambientContext(),
      ])
      speakerStatus.value = status
      speakerProfile.value = profile
      ambientContext.value = ambient
    } catch (error) {
      // The sidecar is optional; do not make the main product snapshot fail.
      speakerStatus.value = { service: 'UNAVAILABLE', last_error: messageFromError(error) }
    }
  }

  async function saveSpeaker() {
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.saveSpeaker({ ...speakerForm })
        updateSettingsFromResponse(settings, result)
        Object.assign(speakerForm, settings.speaker)
        await refreshSpeaker()
        setNotice(result.message || '声纹设置已保存。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function startSpeakerEnrollment() {
    try {
      const value = await productApi.startSpeakerEnrollment()
      const id = value.enrollment_id
      if (typeof id !== 'string') throw new Error('后端未返回录音会话。')
      speakerEnrollment.value = { id, segments: [] }
      setNotice('录音会话已开始，请依次录制三段 5–10 秒声音。')
    } catch (error) { setError(error) }
  }

  async function cancelSpeakerEnrollment(options: unknown = {}) {
    const enrollment = speakerEnrollment.value
    speakerEnrollment.value = null
    if (enrollment) {
      try {
        await productApi.cancelSpeakerEnrollment(enrollment.id)
      } catch {
        // The local UI is already reset.  If the server is offline, its
        // short-lived enrollment session still expires and is never written
        // to disk; do not turn a best-effort cleanup into a blocking error.
      }
    }
    const notify = !isRecord(options) || options.notify !== false
    if (notify) {
      setNotice('已取消本次录音会话；未提交的原始录音不会保留。')
    }
  }

  async function addSpeakerEnrollmentSegment(index: number, label: string, pcmBase64: string) {
    const enrollment = speakerEnrollment.value
    if (!enrollment) return
    try {
      await productApi.addSpeakerEnrollmentSegment(enrollment.id, {
        index, label, pcm_base64: pcmBase64,
      })
      if (!enrollment.segments.includes(index)) enrollment.segments.push(index)
      setNotice(`第 ${index + 1} 段录音已接收。`)
    } catch (error) { setError(error) }
  }

  async function commitSpeakerEnrollment() {
    const enrollment = speakerEnrollment.value
    if (!enrollment || enrollment.segments.length !== 3) {
      setError(new Error('请先完成三段录音。'))
      return
    }
    try {
      await productApi.commitSpeakerEnrollment(enrollment.id)
      speakerEnrollment.value = null
      await refreshSpeaker()
      setNotice('声纹资料已更新；原始录音已删除。')
    } catch (error) { setError(error) }
  }

  async function deleteSpeakerTemplate(id: string) {
    try {
      const result = await productApi.deleteSpeakerTemplate(id)
      if (result.downgraded_to === 'off') {
        // Removing the last usable template safely disables filter mode on
        // the server; keep the form and local settings projection aligned.
        speakerForm.mode = 'off'
        settings.speaker.mode = 'off'
      }
      await refreshSpeaker()
      setNotice(result.downgraded_to === 'off'
        ? '最后一个声纹模板已删除，筛选模式已降级为直通。'
        : '声纹模板已删除。')
    } catch (error) { setError(error) }
  }

  async function setSpeakerTemplateCentroid(id: string, include: boolean) {
    try {
      await productApi.setSpeakerTemplateCentroid(id, include)
      await refreshSpeaker()
      setNotice(include ? '模板已纳入主声纹中心。' : '模板已从主声纹中心移除。')
    } catch (error) { setError(error) }
  }

  async function testSpeakerAudio(pcmBase64: string) {
    try {
      speakerTestResult.value = await productApi.testSpeaker({ pcm_base64: pcmBase64 })
      setNotice('现场声纹测试已完成。')
    } catch (error) { setError(error) }
  }

  async function refreshAmbientContext() {
    try { ambientContext.value = await productApi.ambientContext() } catch (error) { setError(error) }
  }

  async function clearAmbientContext() {
    try { ambientContext.value = await productApi.clearAmbientContext(); setNotice('环境记录已清空。') } catch (error) { setError(error) }
  }

  async function refreshRegistry() {
    try {
      applyRegistry(await productApi.registry())
      lastError.value = ''
    } catch (error) {
      setError(error)
    }
  }

  const connected = computed(() => voice.connection.value === 'connected')
  const runtimeState = computed<ProductRuntimeState>(() => normalizeRuntimeState(voice.state.value, connected.value))
  const runtimeSource = computed(() => snapshot.value?.runtime)
  const current = computed(() => connected.value
    ? voice.current
    : snapshot.value?.conversation.current || defaultCurrent())
  const conversation = computed(() => connected.value ? voice.recentTurns.value : snapshot.value?.conversation.recent || [])
  const shadow = computed<ShadowProductSnapshot>(() => connected.value ? voice.shadowProduct.value : snapshot.value?.shadow || defaultShadow())
  const uiModels = computed<ProductUiModel[]>(() => {
    const providers = new Map(registry.value.providers.map(provider => [provider.id, provider]))
    return registry.value.models.map(item => {
      const provider = providers.get(item.provider_id)
      return {
        ...item,
        provider: provider?.name || item.origin || '未知供应商',
        modelId: item.model_id,
        path: item.local_path || '',
        status: item.verification === 'verified' ? 'verified' : item.verification === 'inherited' ? 'inherited' : item.health || item.verification || 'needs_check',
        maxContextTokens: item.max_context_tokens,
        allowedReasoning: item.allowed_reasoning,
      }
    })
  })
  const availableLlmModels = computed(() => uiModels.value.filter(item =>
    item.capability === 'llm' && !item.hidden && ['verified', 'inherited'].includes(item.verification),
  ))
  const availableTtsModels = computed(() => uiModels.value.filter(item =>
    item.capability === 'tts' && !item.hidden && ['verified', 'inherited'].includes(item.verification),
  ))
  const visibleLocalPool = computed(() => uiModels.value.filter(item => item.capability !== 'llm' && item.capability !== 'tts' && !item.hidden))
  const hiddenPool = computed(() => uiModels.value.filter(item => item.hidden))
  const managedApiModelPool = computed(() => uiModels.value.filter(item => item.origin === 'product_managed' && (item.capability === 'llm' || item.capability === 'tts') && !item.hidden))
  const selectedModels = computed(() => ({
    voice: uiModels.value.find(item => item.id === modelAssignments.voice),
    shadow: uiModels.value.find(item => item.id === modelAssignments.shadow),
    status: uiModels.value.find(item => item.id === modelAssignments.status),
    tts: registry.value.tts_profiles.find(item => item.id === modelAssignments.tts),
  }))
  const activeModels = computed(() => {
    const committed = registry.value.assignments
    const runtimeLlm = uiModels.value.filter(item => item.origin === 'runtime_config' && item.capability === 'llm')
    const committedOrRuntime = (key: 'voice_llm_model_id' | 'shadow_llm_model_id' | 'status_llm_model_id', fallbackIndex: number) =>
      uiModels.value.find(item => item.id === committed[key]) || runtimeLlm[fallbackIndex]
    return {
      voice: committedOrRuntime('voice_llm_model_id', 0),
      shadow: committedOrRuntime('shadow_llm_model_id', 1),
      status: committedOrRuntime('status_llm_model_id', 2),
      tts: registry.value.tts_profiles.find(item => item.id === committed.tts_profile_id)
        || registry.value.tts_profiles.find(item => item.origin === 'runtime_config'),
    }
  })
  const selectedTtsProfile = computed<ProductTtsProfile | undefined>(() => selectedModels.value.tts)
  const selectedTtsModel = computed<ProductUiModel | undefined>(() => {
    const profile = selectedTtsProfile.value
    return profile ? uiModels.value.find(item => item.id === profile.model_id) : undefined
  })
  const activeTtsModel = computed<ProductUiModel | undefined>(() => {
    const profile = activeModels.value.tts
    return profile ? uiModels.value.find(item => item.id === profile.model_id) : undefined
  })

  const latencyMs = computed(() => {
    if (connected.value) {
      // The product metric is deliberately browser-owned: it starts when
      // FINALIZING reaches the UI and stops on speech.started.  Keep replay
      // mode's recorded value available without allowing backend telemetry to
      // overwrite a live measurement.
      if (voice.perceptualLatencyMs.value !== null || !replayMode) return voice.perceptualLatencyMs.value
      const values = voice.currentLatency
      const candidates = [values.speech_end_to_playback_ms, values.agent_request_to_speak_ms, values.turn_total_ms]
      return candidates.find(value => typeof value === 'number' && Number.isFinite(value)) ?? null
    }
    const values = runtimeSource.value?.latency?.values
    if (!values) return null
    const candidates = [values.speech_end_to_playback_ms, values.agent_request_to_speak_ms, values.turn_total_ms]
    return candidates.find(value => typeof value === 'number' && Number.isFinite(value)) ?? null
  })
  const latencyRunning = computed(() => connected.value && voice.perceptualLatencyRunning.value)

  function runtimeAction() {
    if (replayMode) {
      setNotice('当前是日志回放，只读查看测试数据；移除 URL 中的 replay 参数后恢复真实操作。')
      return false
    }
    if (!connected.value) {
      voice.connect()
      void loadSnapshot()
      return false
    }
    return voice.toggleListening()
  }

  function resetSession() {
    if (replayMode) {
      setNotice('当前是日志回放，只读查看测试数据；不会向后端发送 reset。')
      return false
    }
    if (!connected.value) return false
    return voice.resetSession()
  }

  function setReasoningMode(mode: ReasoningMode) {
    if (replayMode) {
      setNotice('当前是日志回放，只读查看测试数据；思考模式不会提交到后端。')
      return false
    }
    if (!connected.value) return false
    return voice.setReasoningMode(mode)
  }

  function toggleProviderReasoning(value: string) {
    const current = providerForm.allowedReasoning
    if (current.includes(value)) {
      if (current.length === 1) return
      providerForm.allowedReasoning = current.filter(item => item !== value)
      return
    }
    providerForm.allowedReasoning = [...current, value]
  }

  async function saveWake() {
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.saveWake({
          enabled: wakeForm.enabled,
          keyword: wakeForm.keyword,
          sensitivity: wakeForm.sensitivity,
        })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || 'KWS 设置已保存，将在下一次会话生效。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function saveSilence() {
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.saveSilence({ seconds: silenceSeconds.value })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || '静默设置已保存，将在下一次 Episode 生效。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function saveShadowGate() {
    const desired = settings.shadow.experimental_context_gate_enabled
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.saveShadow({ experimental_context_gate_enabled: desired })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || 'Shadow 上下文门禁设置已立即生效。')
      } catch (error) {
        settings.shadow.experimental_context_gate_enabled = !desired
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function savePersona() {
    await enqueue(async () => {
      saving.value = true
      try {
        const result = await productApi.savePersona({ ...personaDocuments })
        updateSettingsFromResponse(settings, result)
        setNotice(result.message || 'Persona 已保存，将在下一次 Episode 生效。')
      } catch (error) {
        setError(error)
      } finally {
        saving.value = false
      }
    })
  }

  async function scanModels() {
    await enqueue(async () => {
      modelScanState.value = 'scanning'
      try {
        applyRegistry(await productApi.scanModels())
        modelScanState.value = 'complete'
        setNotice('已完成真实扫描：现有配置和受管模型目录已同步。')
      } catch (error) {
        modelScanState.value = 'idle'
        setError(error)
      }
    })
  }

  function providerPayload(modelId?: string) {
    const isTts = providerForm.protocol === 'minimax'
    const payload: Record<string, unknown> = {
      name: providerForm.name,
      base_url: providerForm.baseUrl,
      protocol: providerForm.protocol,
      capability: isTts ? 'tts' : 'llm',
      api_key: providerForm.apiKey,
      ...(modelId ? { model_id: modelId } : {}),
    }
    if (!isTts) {
      payload.model_policy = {
        max_context_tokens: providerForm.maxContextTokens,
        allowed_reasoning: [...providerForm.allowedReasoning],
      }
    } else {
      payload.voice_id = providerForm.voiceId
      payload.output_format = 'pcm'
      payload.transport = 'websocket_bidi'
      payload.scheduling = 'direct'
      payload.tts_profile = {
        voice_id: providerForm.voiceId,
        output_format: 'pcm',
        transport: 'websocket_bidi',
        scheduling: 'direct',
      }
    }
    return payload
  }

  function requireProviderKey() {
    if (providerForm.apiKey.trim()) return true
    setError(new Error('请输入 API Key；它只会保留到当前一次真实请求结束。'))
    return false
  }

  async function discoverProvider() {
    if (!requireProviderKey()) return
    providerStep.value = 'discovering'
    discoveredModels.value = []
    try {
      const result = await productApi.validateProvider(providerPayload())
      const discovered = Array.isArray(result.discovered_models)
        ? result.discovered_models.filter(item => typeof item === 'string') as string[]
        : []
      discoveredModels.value = discovered
      if (discovered.length) {
        providerStep.value = 'select-model'
        setNotice('已发现模型列表；请再次输入 API Key，再选择一个模型执行最小真实能力测试。')
      } else if (result.requires_custom_model_id) {
        providerStep.value = 'custom-id'
        setNotice('该服务未提供模型列表；请再次输入 API Key，填写模型 ID 后执行最小能力测试。')
      } else if (result.ok === true && Array.isArray(result.models) && result.models.length) {
        providerForm.customModelId = String(result.models[0])
        providerStep.value = 'verified'
      } else {
        providerStep.value = 'idle'
        setError(new Error(typeof result.message === 'string' ? result.message : '供应商发现失败。'))
      }
    } catch (error) {
      providerStep.value = 'idle'
      setError(error)
    } finally {
      // Discovery is one complete credential use.  The next validation step
      // deliberately asks for the key again instead of retaining it in the
      // adapter between requests.
      providerForm.apiKey = ''
    }
  }

  async function verifyCustomModel(modelId = providerForm.customModelId) {
    const selected = modelId.trim()
    if (!selected) {
      providerForm.apiKey = ''
      setError(new Error('请输入模型 ID 后再执行最小能力测试。'))
      return
    }
    if (!requireProviderKey()) return
    providerStep.value = 'discovering'
    try {
      const result = await productApi.validateProvider(providerPayload(selected))
      if (result.ok === true && Array.isArray(result.models) && result.models.includes(selected)) {
        providerForm.customModelId = selected
        providerStep.value = 'verified'
        setNotice('已通过最小真实能力测试，可以加入模型池。')
      } else {
        providerStep.value = 'custom-id'
        setError(new Error(typeof result.message === 'string' ? result.message : '模型最小能力测试未通过。'))
      }
    } catch (error) {
      providerStep.value = 'custom-id'
      setError(error)
    } finally {
      providerForm.apiKey = ''
    }
  }

  async function addVerifiedProvider() {
    const modelId = providerForm.customModelId.trim()
    if (providerStep.value !== 'verified' || !modelId) {
      setError(new Error('请先完成模型发现和最小真实能力测试。'))
      return
    }
    if (!requireProviderKey()) return
    await enqueue(async () => {
      try {
        const result = await productApi.addProvider(providerPayload(modelId))
        applyRegistry(result)
        providerForm.apiKey = ''
        providerForm.customModelId = ''
        providerStep.value = 'idle'
        setNotice('验证成功，模型已加入产品模型池。')
      } catch (error) {
        setError(error)
      } finally {
        // The key is never retained after either outcome.
        providerForm.apiKey = ''
      }
    })
  }

  async function updateModel(model: ProductUiModel, changes: Record<string, unknown>) {
    await enqueue(async () => {
      try {
        applyRegistry(await productApi.updateModel(model.id, changes))
        setNotice(`${model.provider} · ${model.name} 的模型策略已保存。`)
      } catch (error) {
        setError(error)
      }
    })
  }

  async function deleteModel(model: ProductUiModel) {
    await enqueue(async () => {
      try {
        applyRegistry(await productApi.deleteModel(model.id))
        setNotice(`${model.provider} · ${model.name} 已从模型池删除。`)
      } catch (error) {
        setError(error)
      }
    })
  }

  async function hideModel(model: ProductUiModel) {
    await enqueue(async () => {
      try {
        applyRegistry(await productApi.hideModel(model.id))
        setNotice(`${model.provider} · ${model.name} 已隐藏。`)
      } catch (error) {
        setError(error)
      }
    })
  }

  async function restoreModel(model: ProductUiModel) {
    await enqueue(async () => {
      try {
        applyRegistry(await productApi.restoreModel(model.id))
        setNotice(`${model.provider} · ${model.name} 已恢复。`)
      } catch (error) {
        setError(error)
      }
    })
  }

  function assignmentPayload() {
    return {
      voice_llm_model_id: modelAssignments.voice || null,
      shadow_llm_model_id: modelAssignments.shadow || null,
      status_llm_model_id: modelAssignments.status || null,
      tts_profile_id: modelAssignments.tts || null,
    }
  }

  async function requestModelApply() {
    try {
      const result = await enqueue(() => productApi.applyAssignments(assignmentPayload(), false))
      applyImpact.value = Array.isArray(result.impact) ? result.impact.filter(item => typeof item === 'string') : []
      return result
    } catch (error) {
      setError(error)
      return null
    }
  }

  async function confirmModelApply() {
    restartState.value = 'restarting'
    try {
      const result = await enqueue(() => productApi.applyAssignments(assignmentPayload(), true))
      if (result.applied === true) {
        restartState.value = 'complete'
        if (isRecord(result.assignments)) applyRegistry({ ...registry.value, assignments: result.assignments })
        setNotice(typeof result.message === 'string' ? result.message : '模型服务已重启，新的配置已生效。')
        await loadSnapshot()
      } else {
        restartState.value = 'error'
        setError(new Error(typeof result.message === 'string' ? result.message : '模型服务重启失败，已保留上一份有效配置。'))
      }
      return result
    } catch (error) {
      restartState.value = 'error'
      setError(error)
      return null
    }
  }

  function retry() {
    if (!connected.value) voice.connect()
    void loadSnapshot()
  }

  function connect() {
    voice.connect()
    void loadSnapshot()
  }

  function scheduleReplay() {
    if (!replayMode) return
    if (replayTimer !== undefined) window.clearTimeout(replayTimer)
    replayTimer = window.setTimeout(() => {
      voice.injectReplay(voiceShadowReplayFixture)
    }, 220)
  }

  watch(voice.connection, value => {
    if (value === 'connected') {
      void loadSnapshot()
      scheduleReplay()
    }
    if (value === 'offline') {
      setError(new Error(voice.connectionError.value || 'Voice Server 连接已关闭。'))
    }
  })

  onMounted(() => {
    if (replayMode) {
      // Replay is a self-contained visual fixture.  Avoid opening a real
      // socket or snapshot request here so an offline local server cannot
      // obscure the fixture with a connection error toast.
      voice.injectReplay(voiceShadowReplayFixture)
      return
    }
    connect()
    reconnectTimer = window.setInterval(() => {
      if (voice.connection.value === 'offline') voice.connect()
    }, 3000)
  })

  onBeforeUnmount(() => {
    if (reconnectTimer !== undefined) window.clearInterval(reconnectTimer)
    if (replayTimer !== undefined) window.clearTimeout(replayTimer)
    clearNoticeTimers()
    voice.dispose()
  })

  return {
    voice,
    page,
    settingsTab,
    snapshot,
    settings,
    registry,
    loading,
    saving,
    connected,
    replayMode,
    replayMeta: voiceShadowReplayMeta,
    runtimeState,
    current,
    conversation,
    shadow,
    latencyMs,
    latencyRunning,
    wakeForm,
    silenceSeconds,
    personaDocument,
    personaDocuments,
    speakerForm,
    speakerStatus,
    speakerProfile,
    speakerTestResult,
    ambientContext,
    speakerEnrollment,
    providerForm,
    providerStep,
    discoveredModels,
    modelAssignments,
    uiModels,
    availableLlmModels,
    availableTtsModels,
    visibleLocalPool,
    hiddenPool,
    managedApiModelPool,
    selectedModels,
    activeModels,
    selectedTtsProfile,
    selectedTtsModel,
    activeTtsModel,
    shadowExpanded,
    modelScanState,
    restartState,
    applyImpact,
    lastError,
    notice,
    noticeVisible,
    loadSnapshot,
    refreshRegistry,
    runtimeAction,
    resetSession,
    setReasoningMode,
    toggleProviderReasoning,
    saveWake,
    saveSilence,
    saveShadowGate,
    savePersona,
    saveSpeaker,
    refreshSpeaker,
    startSpeakerEnrollment,
    cancelSpeakerEnrollment,
    addSpeakerEnrollmentSegment,
    commitSpeakerEnrollment,
    deleteSpeakerTemplate,
    setSpeakerTemplateCentroid,
    testSpeakerAudio,
    refreshAmbientContext,
    clearAmbientContext,
    scanModels,
    discoverProvider,
    verifyCustomModel,
    addVerifiedProvider,
    updateModel,
    deleteModel,
    hideModel,
    restoreModel,
    requestModelApply,
    confirmModelApply,
    retry,
    setNotice,
    setError,
  }
}

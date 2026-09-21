<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import type { ReasoningMode, ShadowActivity } from '../types'
import {
  useProductAdapter,
  type AssignmentSlot,
  type ConversationRow,
  type PendingSpeechRow,
  type ProductSettingsTab,
  type ProductUiModel,
} from './useProductAdapter'

const adapter = useProductAdapter()
const {
  voice, page: activePage, settingsTab: activeSettingsTab, settings, loading, saving,
  connected, replayMode, replayMeta, runtimeState, current, conversationRows, pendingSpeechRows,
  currentTurnLogged, currentTurnSpeechLogged, currentTurnSpeechPending, shadow, latencyMs, latencyRunning, wakeForm,
  maxWakeKeywords, addWakeKeyword, removeWakeKeyword,
  silenceSeconds, personaDocument: activePersona, personaDocuments, providerForm,
  providerStep, discoveredModels, modelAssignments, uiModels: modelEntries,
  availableLlmModels, availableTtsModels, visibleLocalPool, hiddenPool,
  managedApiModelPool, selectedModels, activeModels, selectedTtsProfile, selectedTtsModel, activeTtsModel, registry,
  shadowExpanded, modelScanState, restartState, applyImpact, dsh, openDshConsole,
  lastError, notice, noticeVisible, runtimeAction, resetSession, setReasoningMode,
  toggleProviderReasoning,
  saveWake, saveSilence, saveShadowGate, savePersona, scanModels, discoverProvider, verifyCustomModel,
  addVerifiedProvider,
  requestModelApply, confirmModelApply, retry, setNotice,
  speakerForm, speakerStatus, speakerProfile, speakerTestResult, ambientContext, speakerEnrollment,
  saveSpeaker, refreshSpeaker, startSpeakerEnrollment, addSpeakerEnrollmentSegment,
  commitSpeakerEnrollment, cancelSpeakerEnrollment, deleteSpeakerTemplate, setSpeakerTemplateCentroid, testSpeakerAudio,
  captureSpeakerEnrollmentSegment, captureSpeakerTest, refreshAmbientContext, clearAmbientContext,
} = adapter
const reasoningMode = voice.reasoningMode
const listening = voice.listening
const waitingForWake = computed(() => (
  listening.value
  && voice.wakeAlwaysOn.value
  && voice.inputOwner.value === 'kws'
))
const contextUsagePercent = computed(() => {
  const episode = voice.agentEpisode.value
  // An idle Agent reports a zero-initialized estimate before the first real
  // provider/context measurement.  Keep that implementation default out of
  // the product resource meter instead of presenting a fabricated 0%.
  if (!episode || typeof episode.context_token_estimate !== 'number'
    || !Number.isFinite(episode.context_token_estimate) || episode.context_token_estimate <= 0) return null
  return typeof episode.context_usage_percent === 'number'
    && Number.isFinite(episode.context_usage_percent)
    ? episode.context_usage_percent
    : null
})

const showApplyConfirm = ref(false)
const expandedModelSections = reactive({ add: true, config: true, pool: false })
const editingModelId = ref<string | null>(null)
const deleteCandidateId = ref<string | null>(null)
const modelEditDraft = reactive({
  name: '', maxContextTokens: 131072, allowedReasoning: [] as string[],
})

const roleLabels: Record<AssignmentSlot, { title: string; hint: string; kind: string }> = {
  voice: { title: '语音交互主模型', hint: '正式回答与 speak 队列', kind: 'LLM 01' },
  shadow: { title: 'Shadow Agent 模型', hint: '后台理解、工具与任务推进', kind: 'LLM 02' },
  status: { title: '状态汇报模型', hint: 'Shadow 的可读实时工作简报', kind: 'LLM 03' },
  tts: { title: '语音合成模型', hint: '将回复转换为可播放语音', kind: 'TTS' },
}
const llmAssignmentSlots: AssignmentSlot[] = ['voice', 'shadow', 'status']
const personaItems: Array<{ id: keyof typeof personaDocuments; label: string; path: string; owner: string }> = [
  { id: 'instructions', label: '工作指令', path: 'config/persona/instructions.md', owner: '主 LLM' },
  { id: 'personality', label: '人格风格', path: 'config/persona/personality.md', owner: '主 LLM' },
  { id: 'user', label: '用户资料', path: 'config/persona/user.md', owner: '主 LLM' },
  { id: 'shadow', label: 'Shadow 指令', path: 'config/shadow_agent/instructions.md', owner: 'Shadow Agent' },
]
const modelContextOptions = [32768, 65536, 131072, 262144, 524288]
const reasoningOptions = ['off', 'low', 'medium', 'high', 'max']

const runtimeMeta = computed(() => {
  const labels = {
    offline: ['离线', '后端连接已断开，页面仍保留，可重试连接。', 'OFF'],
    standby: ['待机', 'Voice Server 已准备好，点击即可开始语音会话。', 'LIVE'],
    listening: ['正在聆听', '正在接收本机音频并等待可靠的语句结束。', 'LIVE'],
    thinking: ['正在思考', '主 Agent 正在准备下一段可播报回复。', 'LIVE'],
    speaking: ['正在播报', '正在播放真实 TTS 输出，随时可以打断。', 'LIVE'],
    error: ['需要检查', '运行时报告了错误，请查看服务状态或重试。', 'ERROR'],
  } as const
  const meta = waitingForWake.value
    ? ['等待唤醒', 'KWS 正在监听唤醒词；唤醒后才会进入语音识别。', 'LIVE'] as const
    : labels[runtimeState.value]
  const [label, description, chip] = meta
  return { label, description, chip, timestamp: replayMode ? replayMeta.label : connected.value ? '实时运行时状态' : '等待 Voice Server' }
})

const currentDocument = computed(() => personaItems.find(item => item.id === activePersona.value) || personaItems[0])
const hasConversation = computed(() => conversationRows.value.length > 0
  || pendingSpeechRows.value.length > 0
  || Boolean(current.value.partial_text || current.value.final_text || current.value.assistant_text || current.value.tts_text))
// The rail renders the server-ordered conversation log plus the transactions
// that have not settled into it yet.  Ordering (including where the KWS wake
// acknowledgement belongs) is the server's ``seq``, never a client guess.
const conversationEntries = computed(() => conversationRows.value)
const liveSpeechEntries = computed(() => pendingSpeechRows.value)
const liveUserText = computed(() => (currentTurnLogged.value ? '' : current.value.final_text))
const liveAssistantText = computed(() => (
  current.value.assistant_text && !currentTurnSpeechLogged.value && !currentTurnSpeechPending.value
    ? current.value.assistant_text
    : ''
))
const shadowContext = computed(() => {
  const context = shadow.value.context
  if (!context || (context.used_tokens == null && context.limit_tokens == null && context.usage_percent == null)) return null
  return context
})
const shadowCacheHitRatio = computed(() => {
  const ratio = shadow.value.provider?.cache_hit_ratio
  return typeof ratio === 'number' && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1 ? ratio : null
})
// The backend projection writes the same event text into both ``focus`` and
// ``summary`` for status/progress/observation events.  Showing the focus line
// then repeats the summary verbatim, so only render it when it carries
// genuinely different information.
const shadowFocusText = computed(() => {
  const focus = (shadow.value.focus || '').trim()
  const summary = (shadow.value.summary || '').trim()
  if (!focus || focus === summary) return ''
  return focus
})
const conversationContext = computed(() => {
  const episode = voice.agentEpisode.value
  if (!episode || typeof episode.context_token_estimate !== 'number'
    || !Number.isFinite(episode.context_token_estimate) || episode.context_token_estimate <= 0) return null
  return {
    usedTokens: episode.context_token_estimate,
    limitTokens: episode.context_limit_tokens,
    usagePercent: episode.context_usage_percent,
  }
})
const conversationCacheHitRatio = computed(() => {
  const ratio = voice.agentEpisode.value?.provider_cache_hit_ratio
  return typeof ratio === 'number' && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1 ? ratio : null
})
const shadowContextTooltip = computed(() => {
  if (!shadowContext.value) return '上下文占用 · 后端尚未提供 telemetry'
  return `上下文占用 · ${formatTokens(shadowContext.value.used_tokens)} / ${formatTokens(shadowContext.value.limit_tokens)} · ${shadowContext.value.usage_percent ?? '—'}%`
})
const conversationContextTooltip = computed(() => {
  if (!conversationContext.value) return '上下文占用 · 后端尚未提供 telemetry'
  return `上下文占用 · ${formatTokens(conversationContext.value.usedTokens)} / ${formatTokens(conversationContext.value.limitTokens)} · ${conversationContext.value.usagePercent ?? '—'}%`
})
const modelPoolSummary = computed(() => `${managedApiModelPool.value.length} 个可编辑 API · ${availableLlmModels.value.length} LLM · ${availableTtsModels.value.length} TTS`)
const apiModelPool = computed(() => modelEntries.value.filter(item => (item.capability === 'llm' || item.capability === 'tts') && !item.hidden))
const hiddenApiModelPool = computed(() => modelEntries.value.filter(item => (item.capability === 'llm' || item.capability === 'tts') && item.hidden))
const hiddenLocalPool = computed(() => modelEntries.value.filter(item => item.capability !== 'llm' && item.capability !== 'tts' && item.hidden))
const assignedModelIds = computed(() => new Set([
  modelAssignments.voice, modelAssignments.shadow, modelAssignments.status,
  selectedModels.value.tts?.model_id,
]))
const selectedShadowReasoning = computed(() => 'high')

const shadowActivities = computed(() => shadow.value.activities || [])
const shadowTraceElement = ref<HTMLElement | null>(null)
// ``null`` means the user has not touched the trace box yet; the run trace
// defaults to the bottom so the newest activity is always the visible one.
let shadowTraceFollowTail: boolean | null = null

function captureShadowTraceScroll() {
  const element = shadowTraceElement.value
  if (!element) return
  shadowTraceFollowTail = element.scrollHeight - element.scrollTop - element.clientHeight < 24
}

function followShadowTraceTail() {
  const element = shadowTraceElement.value
  if (!element) return
  // Follow the tail on first paint and while the user stays at the bottom.
  // Scrolling up to read earlier steps must not be yanked back.
  const atTail = element.scrollHeight - element.scrollTop - element.clientHeight < 24
  if (shadowTraceFollowTail === false && !atTail) return
  element.scrollTop = element.scrollHeight
  shadowTraceFollowTail = true
}

watch(shadowActivities, async () => {
  await nextTick()
  followShadowTraceTail()
}, { deep: true })

// The box height is driven by the flex layout, so a window resize can leave it
// showing the oldest steps even though the user never scrolled away.
let shadowTraceResizeObserver: ResizeObserver | null = null
watch(shadowTraceElement, (element, previous) => {
  if (previous) shadowTraceResizeObserver?.disconnect()
  shadowTraceResizeObserver = null
  if (!element || typeof ResizeObserver === 'undefined') return
  shadowTraceResizeObserver = new ResizeObserver(() => followShadowTraceTail())
  shadowTraceResizeObserver.observe(element)
}, { immediate: true })

const conversationScrollElement = ref<HTMLElement | null>(null)
let conversationFollowTail: boolean | null = null
// A chat rail pins short histories to the bottom.  Doing that with
// ``justify-content: flex-end`` also pushes overflowing content into the
// unreachable main-start region, so anchor from the top edge and only add the
// bottom auto-margin while the rail genuinely fits.
const conversationOverflow = ref(false)

function measureConversationOverflow(element: HTMLElement) {
  conversationOverflow.value = element.scrollHeight - element.clientHeight > 4
}

function captureConversationScrollPosition() {
  const element = conversationScrollElement.value
  if (!element) return
  conversationFollowTail = element.scrollHeight - element.scrollTop - element.clientHeight < 24
  measureConversationOverflow(element)
}

watch(
  [conversationRows, pendingSpeechRows,
    () => current.value.final_text, () => current.value.partial_text, () => current.value.assistant_text],
  async () => {
    const elementBeforeUpdate = conversationScrollElement.value
    if (conversationFollowTail === null && elementBeforeUpdate) {
      conversationFollowTail = elementBeforeUpdate.scrollHeight
        - elementBeforeUpdate.scrollTop - elementBeforeUpdate.clientHeight < 24
    }
    await nextTick()
    const element = conversationScrollElement.value
    if (element) measureConversationOverflow(element)
    if (!conversationFollowTail) return
    if (element) element.scrollTop = element.scrollHeight
  },
  { deep: true },
)

const shadowLastActivityAt = computed(() => (
  shadow.value.last_activity_at
  || shadowActivities.value[shadowActivities.value.length - 1]?.timestamp
  || shadow.value.timeline[shadow.value.timeline.length - 1]?.timestamp
  || ''
))
const shadowDisplayStatus = computed(() => {
  if (!shadow.value.available) return '不可用'
  if (shadow.value.user_input_required || shadow.value.phase === 'waiting') return '等待输入'
  if (shadow.value.active || shadow.value.phase === 'working') return '运行中'
  if (shadow.value.phase === 'error' || shadow.value.phase === 'failed') return '失败'
  if (shadow.value.phase === 'completed' || shadow.value.phase === 'done') return '已完成'
  return '空闲'
})
const shadowStatusClass = computed(() => {
  if (!shadow.value.available) return 'unavailable'
  if (shadow.value.user_input_required || shadow.value.phase === 'waiting') return 'waiting'
  if (shadow.value.active || shadow.value.phase === 'working') return 'working'
  if (shadow.value.phase === 'error' || shadow.value.phase === 'failed') return 'failed'
  if (shadow.value.phase === 'completed' || shadow.value.phase === 'done') return 'completed'
  return 'idle'
})

function legacyShadowEvents(): ShadowActivity[] {
  const eventKinds: Record<string, ShadowActivity['event_kind']> = {
    '后台处理中': 'progress',
    '发布观察': 'observation',
    '等待你的输入': 'user_input_required',
    '完成': 'result',
    '需要注意': 'failed',
  }
  return shadow.value.timeline.map((item, index) => ({
    run_seq: 0,
    item_id: `legacy-event-${index}`,
    stream: 'event',
    phase: item.state === 'error' ? 'failed' : 'completed',
    text: item.text || item.label,
    event_kind: eventKinds[item.label],
    timestamp: item.timestamp,
  }))
}

// The run box shows the trajectory of the current activation.  It is bounded by
// the backend projection (96 activities) and scrolls internally, so the full
// trajectory is kept: capping it here would make that scrolling pointless.
const shadowRunningTrace = computed(() => shadowActivities.value
  .filter(item => item.stream === 'thinking' || item.stream === 'lifecycle' || item.stream === 'tool'))
const shadowEventActivities = computed(() => {
  const values = shadowActivities.value.filter(item => item.stream === 'event')
  return values.length ? values : legacyShadowEvents()
})
// A fixed output box only presents the newest public ShadowEvent; older ones
// remain in the bounded snapshot but are deliberately not stacked here.
const shadowLatestEventActivity = computed<ShadowActivity | null>(() => {
  const values = shadowEventActivities.value
  return values.length ? values[values.length - 1] : null
})

function shadowActivityPhaseLabel(activity: ShadowActivity) {
  if (activity.stream === 'tool') {
    const labels: Record<string, string> = {
      preparing: '准备调用', running: '执行中', completed: '已完成',
      failed: '失败', waiting: '等待输入', skipped: '已跳过', ready: '待执行',
    }
    if (activity.tool_status && labels[activity.tool_status]) return labels[activity.tool_status]
  }
  if (activity.event_kind === 'user_input_required') return '等待输入'
  if (activity.event_kind === 'failed' || activity.phase === 'failed') return '失败'
  if (activity.phase === 'started' || activity.phase === 'delta') return '进行中'
  return '已完成'
}

function shadowActivityText(activity: ShadowActivity) {
  if (activity.text) return activity.text
  if (activity.stream === 'tool') return activity.tool_name ? `正在处理 ${activity.tool_name}` : '正在处理工具调用'
  if (activity.event_kind === 'user_input_required') return 'Shadow 正在等待你的输入'
  if (activity.phase === 'failed') return '这项活动没有完成'
  return activity.phase === 'started' || activity.phase === 'delta' ? '正在运行' : '已更新'
}

function shadowActivityKindLabel(activity: ShadowActivity) {
  const labels: Record<string, string> = {
    progress: '处理中', observation: '观察', user_input_required: '需要输入',
    result: '结果', failed: '失败',
  }
  return activity.event_kind ? labels[activity.event_kind] || activity.event_kind : '事件'
}

function shadowActivityClass(activity: ShadowActivity) {
  return {
    active: activity.phase === 'started' || activity.phase === 'delta',
    complete: activity.phase === 'completed',
    failed: activity.phase === 'failed' || activity.tool_status === 'failed',
    waiting: activity.tool_status === 'waiting' || activity.event_kind === 'user_input_required',
  }
}

function formatTime(value: string | undefined) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatResponseLatency(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (value < 1000) return `${Math.round(value)} ms`
  const seconds = Number((value / 1000).toFixed(2))
  return `${seconds} s`
}

function formatTokens(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1000) return `${Math.round(value / 1000)}K`
  return String(Math.round(value))
}

function contextPercent(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0
}

function providerClass(item: ProductUiModel | undefined) {
  return item?.provider.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/-+$/, '') || 'managed'
}

function modelTitle(item: ProductUiModel | undefined) {
  return item ? `${item.provider} · ${item.name}` : '未选择模型'
}

function selectedModel(slot: AssignmentSlot) {
  if (slot === 'tts') return undefined
  const id = modelAssignments[slot]
  return modelEntries.value.find(item => item.id === id)
}

function modelStatusLabel(item: ProductUiModel | undefined) {
  if (!item) return '未选择'
  if (item.verification === 'verified') return '已验证'
  if (item.verification === 'inherited') return '运行时导入'
  if (item.health === 'healthy') return '健康'
  return item.health || item.verification || '待检查'
}

function formatContext(value: number | undefined) {
  return value ? `${formatTokens(value)} tokens` : '未设置'
}

function turnStatusLabel(row: ConversationRow) {
  const labels: Record<string, string> = {
    completed: '已完成识别',
    empty_input: '没有听到内容',
    stopped: '已停止',
    reset: '已重置',
    failed: '识别失败',
    pending: '识别中',
  }
  return labels[row.status] || labels[row.termination] || row.status || row.termination || '已记录'
}

function speechStatusLabel(row: ConversationRow) {
  const labels: Record<string, string> = {
    completed: '已完成播报',
    failed: '播报失败',
    interrupted: '播报中断',
    replaced: '已被新内容替换',
    superseded: '已被新内容替换',
    speaking: '正在播报',
    playing: '正在播报',
    queued: '等待播放',
    waiting: '等待播放',
  }
  const status = row.status || ''
  return labels[status.toLowerCase()] || status || '已记录'
}

function speechStatusKey(row: ConversationRow) {
  return row.status ? row.status.toLowerCase() : 'recorded'
}

function pendingSpeechStatusLabel(row: PendingSpeechRow) {
  return row.state === 'playing' ? '正在播报' : '等待播放'
}

function deliveryStatusLabel(delivery: { status?: string; termination?: string } | undefined) {
  if (!delivery) return ''
  const statusLabels: Record<string, string> = {
    full: '已完整传达',
    partial: '已传达前缀',
    none: '未传达',
    unknown: '传达边界未知',
  }
  const terminationLabels: Record<string, string> = {
    interrupted: '被插话打断',
    failed: 'TTS 失败',
    stopped: '手动停止',
    completed: '正常结束',
  }
  const status = statusLabels[delivery.status || ''] || delivery.status || '播放信息不可用'
  const termination = terminationLabels[delivery.termination || '']
  return termination ? `${status} · ${termination}` : status
}

function reasoningSummary(item: ProductUiModel | undefined) {
  if (!item || item.capability !== 'llm') return '不适用'
  const values = item.allowedReasoning || []
  return values.length ? values.join(' / ') : '—'
}

function modelPolicySummary(item: ProductUiModel | undefined) {
  if (!item) return '尚未分配'
  if (item.capability === 'tts') return '语音合成 · profile 管理'
  return `${formatContext(item.maxContextTokens)} · ${reasoningSummary(item)}`
}

function ttsModelId(profile: { model_id: string } | undefined) {
  return modelEntries.value.find(item => item.id === profile?.model_id)?.modelId || profile?.model_id || '—'
}

function toggleModelSection(section: keyof typeof expandedModelSections) {
  expandedModelSections[section] = !expandedModelSections[section]
}

function isAssignedModel(item: ProductUiModel) {
  return assignedModelIds.value.has(item.id)
}

function isEditableApiModel(item: ProductUiModel) {
  return item.origin === 'product_managed' && (item.capability === 'llm' || item.capability === 'tts')
}

function startEditingModel(item: ProductUiModel) {
  if (!isEditableApiModel(item)) return
  editingModelId.value = item.id
  deleteCandidateId.value = null
  modelEditDraft.name = item.name
  modelEditDraft.maxContextTokens = item.maxContextTokens || 131072
  modelEditDraft.allowedReasoning = [...(item.allowedReasoning || ['off'])]
}

function toggleReasoningLevel(value: string) {
  if (value === 'off') {
    modelEditDraft.allowedReasoning = ['off']
    return
  }
  const withoutOff = modelEditDraft.allowedReasoning.filter(item => item !== 'off')
  const next = withoutOff.includes(value) ? withoutOff.filter(item => item !== value) : [...withoutOff, value]
  modelEditDraft.allowedReasoning = next.length ? next : ['off']
}

async function saveModelEdit(item: ProductUiModel) {
  const name = modelEditDraft.name.trim()
  if (!name) {
    adapter.setError(new Error('显示名称不能为空。'))
    return
  }
  const changes: Record<string, unknown> = { name }
  if (item.capability === 'llm') {
    changes.max_context_tokens = modelEditDraft.maxContextTokens
    changes.allowed_reasoning = [...modelEditDraft.allowedReasoning]
  }
  await updateModel(item, changes)
  editingModelId.value = null
}

async function updateModel(item: ProductUiModel, changes: Record<string, unknown>) {
  await adapter.updateModel(item, changes)
}

function requestDeleteModel(item: ProductUiModel) {
  if (!isEditableApiModel(item)) return
  if (isAssignedModel(item)) {
    adapter.setError(new Error('该模型正在被分配使用，请先在模型配置中改用其他条目。'))
    return
  }
  editingModelId.value = null
  deleteCandidateId.value = deleteCandidateId.value === item.id ? null : item.id
}

async function confirmDeleteModel(item: ProductUiModel) {
  if (deleteCandidateId.value !== item.id || isAssignedModel(item)) return
  await deleteModel(item)
  deleteCandidateId.value = null
}

async function deleteModel(item: ProductUiModel) {
  await adapter.deleteModel(item)
}

async function toggleHidden(item: ProductUiModel) {
  if (isAssignedModel(item)) {
    adapter.setError(new Error('该模型正在被分配使用，请先改用其他条目。'))
    return
  }
  if (item.hidden) await restoreModel(item)
  else await hideModel(item)
}

async function hideModel(item: ProductUiModel) {
  await adapter.hideModel(item)
}

async function restoreModel(item: ProductUiModel) {
  await adapter.restoreModel(item)
}

async function openApplyDialog() {
  const result = await requestModelApply()
  if (result?.requires_confirmation) showApplyConfirm.value = true
}

async function confirmApply() {
  showApplyConfirm.value = false
  await confirmModelApply()
}

async function selectDiscoveredModel(event: Event) {
  const target = event.target as HTMLSelectElement
  providerForm.customModelId = target.value
}

const speakerRecordingSegment = ref<number | null>(null)
const speakerTestRecording = ref(false)
const speakerRecordingSeconds = ref(0)
/** True while the server is recording; there is nothing for the browser to stop. */
const speakerServerCapture = ref(false)
type SpeakerCapture = {
  index: number
  label: string
  kind: 'enrollment' | 'test'
  context: AudioContext
  stream: MediaStream
  source: MediaStreamAudioSourceNode
  processor: ScriptProcessorNode
  sink: GainNode
  chunks: Float32Array[]
  startedAt: number
  timeout?: number
  ticker?: number
}
let speakerCapture: SpeakerCapture | null = null

const speakerServiceLabel = computed(() => {
  if (speakerStatus.value.model_present === false) return '未安装'
  const profile = speakerStatus.value.profile
  if (profile && (profile as Record<string, unknown>).available === false) return '未注册'
  const service = String(speakerStatus.value.service || '').toUpperCase()
  if (service === 'READY') return '就绪'
  if (service === 'STARTING') return '启动中'
  if (service === 'DEGRADED') return '降级直通'
  if (service === 'ERROR') return '错误'
  return service === 'DISABLED' ? '已关闭' : '不可用'
})

const speakerTemplates = computed(() => {
  const templates = speakerProfile.value.templates
  return Array.isArray(templates) ? templates.filter(item => item && typeof item === 'object') as Array<Record<string, unknown>> : []
})

const ambientRecords = computed(() => {
  const records = ambientContext.value.records
  return Array.isArray(records) ? records.filter(item => item && typeof item === 'object') as Array<Record<string, unknown>> : []
})

const speakerHasProfile = computed(() => (
  speakerProfile.value.available === true
  || (speakerStatus.value.profile && (speakerStatus.value.profile as Record<string, unknown>).available === true)
))

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const speakerNeedsReenrollment = computed(() => {
  const profile = speakerProfile.value
  if (!isRecord(profile)) return false
  if (profile.stale === true || profile.needs_reenrollment === true) return true
  const nested = speakerStatus.value.profile
  return isRecord(nested) && (nested.stale === true || nested.needs_reenrollment === true)
})

const speakerStaleReason = computed(() => {
  const profile = isRecord(speakerProfile.value) ? speakerProfile.value : {}
  const nested = isRecord(speakerStatus.value.profile) ? speakerStatus.value.profile as Record<string, unknown> : {}
  const reason = String(profile.stale_reason || nested.stale_reason || '')
  const labels: Record<string, string> = {
    feature_frontend_changed: '特征前端版本已更新；',
    feature_numeric_mismatch: '特征数值指纹与注册时不一致；',
    speaker_model_changed: '声纹模型已更换；',
    profile_schema_upgraded: '声纹资料格式已升级；',
    profile_missing_probe: '旧资料缺少特征指纹；',
  }
  return labels[reason] || ''
})

const speakerFilterAllowed = computed(() => {
  if (!speakerHasProfile.value || speakerStatus.value.model_present === false) return false
  const service = String(speakerStatus.value.service || '').toUpperCase()
  // When the saved mode is off, the runtime intentionally keeps the
  // sidecar disconnected.  A registered profile should still be able to
  // request filter mode; the server will connect/probe the Worker during the
  // save and reject the change if it is genuinely unavailable.  Requiring
  // worker_connected here made the option permanently impossible to select
  // from the default off state (a circular UI gate).
  return service === 'DISABLED' || service === 'STARTING'
    || (service === 'READY' && speakerStatus.value.worker_connected === true)
})

const speakerEnrollmentComplete = computed(() => (
  speakerEnrollment.value?.segments.length === 3
))

function speakerStateLabel(value: unknown) {
  const labels: Record<string, string> = {
    owner_confirmed: '确认是本人',
    owner_continuity: '连续保持本人',
    non_owner_confirmed: '明确非本人',
    unknown: '证据不足',
    unavailable: '声纹不可用',
  }
  return labels[String(value || '')] || String(value || '—')
}

function restoreSpeakerDefaults() {
  // Mirrors config/default.yaml.  Changing the feature frontend moved the raw
  // cosine operating point, so these numbers are not comparable with any
  // threshold calibrated before that change.
  speakerForm.confirm_threshold = 0.6
  speakerForm.continuity_threshold = 0.5
  setNotice('声纹阈值已恢复默认值（0.60 / 0.50），点击保存后应用。')
}

function mergeFloat32(chunks: Float32Array[]) {
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0)
  const result = new Float32Array(length)
  let offset = 0
  for (const chunk of chunks) {
    result.set(chunk, offset)
    offset += chunk.length
  }
  return result
}

function pcmWavBase64(samples: Float32Array, sampleRate: number) {
  const targetRate = 16000
  const sourceRate = Math.max(1, sampleRate)
  const ratio = sourceRate / targetRate
  const count = Math.max(1, Math.round(samples.length / ratio))
  const pcm = new Int16Array(count)
  for (let index = 0; index < count; index += 1) {
    const source = index * ratio
    const left = Math.floor(source)
    const right = Math.min(samples.length - 1, left + 1)
    const fraction = source - left
    const value = (samples[left] || 0) * (1 - fraction) + (samples[right] || 0) * fraction
    const clipped = Math.max(-1, Math.min(1, value))
    pcm[index] = clipped < 0 ? Math.round(clipped * 32768) : Math.round(clipped * 32767)
  }
  const buffer = new ArrayBuffer(44 + pcm.byteLength)
  const view = new DataView(buffer)
  const writeText = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index))
  }
  writeText(0, 'RIFF')
  view.setUint32(4, 36 + pcm.byteLength, true)
  writeText(8, 'WAVE')
  writeText(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, targetRate, true)
  view.setUint32(28, targetRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeText(36, 'data')
  view.setUint32(40, pcm.byteLength, true)
  new Int16Array(buffer, 44).set(pcm)
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + 0x8000, bytes.length)))
  }
  return btoa(binary)
}

async function openSpeakerCapture(index: number, label: string, kind: SpeakerCapture['kind']) {
  if (speakerRecordingSegment.value !== null || speakerTestRecording.value) return
  if (!navigator.mediaDevices?.getUserMedia) {
    adapter.setError(new Error('当前浏览器不支持麦克风录音。'))
    return
  }
  try {
    // Fallback path only.  The primary enrollment/test flow records on the
    // server so that it uses the same device and processing chain as a live
    // turn.  If that path is unavailable, at least ask for a 16 kHz context so
    // the browser resamples properly, and keep WebRTC AEC/NS/AGC off so the
    // signal is not reshaped differently from the runtime chain.
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    })
    const context = new AudioContext({ sampleRate: 16000 })
    await context.resume()
    const source = context.createMediaStreamSource(stream)
    const processor = context.createScriptProcessor(4096, 1, 1)
    const sink = context.createGain()
    sink.gain.value = 0
    const capture: SpeakerCapture = {
      index, label: label.trim() || '自定义', kind, context, stream, source, processor, sink,
      chunks: [] as Float32Array[], startedAt: performance.now(),
    }
    processor.onaudioprocess = event => {
      const active = speakerCapture
      if (!active) return
      active.chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)))
      speakerRecordingSeconds.value = Math.floor((performance.now() - active.startedAt) / 1000)
    }
    source.connect(processor)
    processor.connect(sink)
    sink.connect(context.destination)
    speakerCapture = capture
    speakerRecordingSegment.value = kind === 'enrollment' ? index : null
    speakerTestRecording.value = kind === 'test'
    speakerRecordingSeconds.value = 0
    capture.ticker = window.setInterval(() => {
      if (speakerCapture) speakerRecordingSeconds.value = Math.floor((performance.now() - speakerCapture.startedAt) / 1000)
    }, 200)
    capture.timeout = window.setTimeout(() => { void stopSpeakerRecording() }, 10000)
  } catch (error) {
    adapter.setError(error instanceof Error ? error : new Error('无法打开麦克风。'))
  }
}

/** Seconds of audio requested for one segment of each flow. */
const SPEAKER_ENROLLMENT_SECONDS = 8
const SPEAKER_TEST_SECONDS = 6

/**
 * Record through the server.  Returns false when no capture device is
 * available so the caller can fall back to browser recording.
 */
async function captureOnServer(
  kind: SpeakerCapture['kind'], index: number, label: string, seconds: number,
) {
  speakerRecordingSeconds.value = 0
  speakerRecordingSegment.value = kind === 'enrollment' ? index : null
  speakerTestRecording.value = kind === 'test'
  speakerServerCapture.value = true
  let done = false
  const ticker = window.setInterval(() => {
    if (!done) speakerRecordingSeconds.value += 1
  }, 1000)
  try {
    const ok = kind === 'test'
      ? await captureSpeakerTest(seconds)
      : await captureSpeakerEnrollmentSegment(index, label, seconds)
    return ok
  } finally {
    done = true
    speakerServerCapture.value = false
    window.clearInterval(ticker)
    speakerRecordingSegment.value = null
    speakerTestRecording.value = false
    speakerRecordingSeconds.value = 0
  }
}

async function beginSpeakerRecording(index: number, label: string) {
  if (speakerRecordingSegment.value !== null || speakerTestRecording.value) return
  if (!speakerEnrollment.value) await startSpeakerEnrollment()
  if (!speakerEnrollment.value) return
  const started = await captureOnServer('enrollment', index, label, SPEAKER_ENROLLMENT_SECONDS)
  if (!started) await openSpeakerCapture(index, label, 'enrollment')
}

async function beginSpeakerTest() {
  if (!speakerHasProfile.value) {
    adapter.setError(new Error('请先完成声纹注册，再进行现场测试。'))
    return
  }
  const started = await captureOnServer('test', -1, '现场测试', SPEAKER_TEST_SECONDS)
  if (!started) await openSpeakerCapture(-1, '现场测试', 'test')
}

function speakerPrompt(index: number) {
  const prompts = speakerEnrollment.value?.prompts || []
  return prompts[index] || ''
}

function speakerSegmentLabel(index: number, fallback: string) {
  if (index !== 2 || typeof document === 'undefined') return fallback
  const input = document.getElementById(`speaker-label-${index}`) as HTMLInputElement | null
  return input?.value?.trim() || '自定义'
}

async function stopSpeakerRecording() {
  const capture = speakerCapture
  if (!capture) return
  speakerCapture = null
  speakerRecordingSegment.value = null
  speakerTestRecording.value = false
  if (capture.timeout !== undefined) window.clearTimeout(capture.timeout)
  if (capture.ticker !== undefined) window.clearInterval(capture.ticker)
  capture.processor.disconnect()
  capture.source.disconnect()
  capture.sink.disconnect()
  capture.stream.getTracks().forEach(track => track.stop())
  const sampleRate = capture.context.sampleRate
  const samples = mergeFloat32(capture.chunks)
  await capture.context.close()
  speakerRecordingSeconds.value = 0
  const durationMs = Math.round(samples.length * 1000 / sampleRate)
  if (durationMs < 5000 || durationMs > 10000) {
    adapter.setError(new Error('每段声纹录音需要 5–10 秒；本段未上传，请重新录制。'))
    return
  }
  const encoded = pcmWavBase64(samples, sampleRate)
  if (capture.kind === 'test') {
    await testSpeakerAudio(encoded)
  } else {
    await addSpeakerEnrollmentSegment(capture.index, capture.label, encoded)
  }
}

onBeforeUnmount(() => {
  shadowTraceResizeObserver?.disconnect()
  shadowTraceResizeObserver = null
  const capture = speakerCapture
  if (capture) {
    speakerCapture = null
    speakerRecordingSegment.value = null
    speakerTestRecording.value = false
    if (capture.timeout !== undefined) window.clearTimeout(capture.timeout)
    if (capture.ticker !== undefined) window.clearInterval(capture.ticker)
    capture.processor.disconnect()
    capture.source.disconnect()
    capture.sink.disconnect()
    capture.stream.getTracks().forEach(track => track.stop())
    void capture.context.close()
  }
  // Release any segments already uploaded to the short-lived server session
  // when the settings view itself is closed.
  void cancelSpeakerEnrollment({ notify: false })
})

function pageLabel() {
  return activePage.value === 'home' ? '工作台' : '设置'
}

</script>

<template>
  <main class="prototype-shell product-shell" :class="`runtime-${runtimeState}`">
    <header class="prototype-topbar">
      <button class="brand" type="button" aria-label="回到首页" @click="activePage = 'home'">
        <span class="brand-symbol"><i></i><i></i><i></i></span>
        <span><strong>Voice Terminal</strong><small>全天候桌面语音助手</small></span>
      </button>
      <nav class="page-nav" aria-label="主导航">
        <button type="button" :class="{ active: activePage === 'home' }" @click="activePage = 'home'">工作台</button>
        <button type="button" :class="{ active: activePage === 'settings' }" @click="activePage = 'settings'">设置</button>
      </nav>
      <div class="dsh-console-control">
        <button
          class="dsh-console-button"
          type="button"
          :disabled="dsh.state !== 'ready'"
          :title="dsh.message || `DeepSeek Harness ${dsh.version || ''}`"
          @click="openDshConsole"
        >{{ dsh.state === 'starting' ? 'DSH 启动中' : '打开 DSH 控制台' }}</button>
        <small v-if="shadow.backend === 'dsh'">Shadow 模型与 reasoning 由 DSH Harness 管理</small>
        <small v-if="dsh.state === 'unavailable' || dsh.state === 'exited'">{{ dsh.message || 'DSH 不可用' }}</small>
      </div>
      <div class="topbar-status">
        <span class="prototype-tag"><i></i> PRODUCT UI · LIVE</span>
        <span class="mock-tag">{{ replayMode ? `${replayMeta.label} · 只读` : connected ? '本机已连接' : '离线可重试' }}</span>
      </div>
    </header>

    <!-- Keep both top-level views mounted; v-show preserves state without paying for a hidden layout. -->
    <section v-show="activePage === 'home'" :class="{ 'view-hidden': activePage !== 'home' }" class="workbench workbench-reordered" aria-label="Voice Terminal 工作台">
      <aside class="shadow-column panel">
        <header class="shadow-header">
          <div><p class="panel-kicker"><span class="shadow-spark">✦</span> Shadow Agent</p><h2>后台协作</h2></div>
          <button class="expand-button" type="button" :aria-expanded="shadowExpanded" @click="shadowExpanded = !shadowExpanded">{{ shadowExpanded ? '收起' : '展开' }} <span>{{ shadowExpanded ? '−' : '+' }}</span></button>
        </header>
        <div class="shadow-scroll">
          <section class="shadow-summary-card" :class="`status-${shadowStatusClass}`">
            <div class="shadow-summary-heading">
              <p class="section-label">当前 Shadow 摘要</p>
              <span class="shadow-status-pill" :class="shadowStatusClass"><i></i>{{ shadowDisplayStatus }}</span>
              <h3>{{ shadow.summary || (shadow.available ? '等待 Shadow 生成本轮可读摘要。' : 'Shadow Agent 当前不可用') }}</h3>
            </div>
            <p v-if="shadowFocusText" class="shadow-focus-line"><span>当前关注</span>{{ shadowFocusText }}</p>
            <div class="shadow-summary-meta"><span>{{ shadow.active ? '运行中' : shadow.user_input_required ? '等待你的输入' : '最近更新' }}</span><time>{{ shadowLastActivityAt ? formatTime(shadowLastActivityAt) : '尚无活动记录' }}</time></div>
          </section>

          <section class="shadow-runtime-card" :class="`status-${shadowStatusClass}`">
            <!-- One heading row for the whole run card.  It used to repeat the
                 same title twice ("AGENT 运行轨迹" over "运行轨迹") and spend a
                 second row on the status chip alone. -->
            <section class="shadow-activity-section thinking-section">
              <header>
                <span class="activity-section-icon thinking-icon">✦</span>
                <div><p class="section-label">Agent 运行轨迹</p><strong>运行轨迹</strong></div>
                <span class="shadow-run-indicator" :class="shadowStatusClass"><i></i>{{ shadow.active ? 'LIVE' : shadowDisplayStatus }}</span>
                <em>{{ shadowRunningTrace.length }}</em>
              </header>
              <div ref="shadowTraceElement" class="shadow-trace-box" @scroll="captureShadowTraceScroll">
                <ol class="shadow-activity-list">
                  <li v-for="activity in shadowRunningTrace" :key="`${activity.run_seq}-${activity.item_id}`" class="shadow-activity-item" :class="shadowActivityClass(activity)">
                    <span class="activity-node"><i></i></span><div class="activity-copy"><div class="activity-line"><strong>{{ activity.stream === 'tool' ? (activity.tool_name || '未知工具') : shadowActivityText(activity) }}</strong><span>{{ shadowActivityPhaseLabel(activity) }}</span></div><p v-if="activity.stream === 'tool'">{{ shadowActivityText(activity) }}</p><time>{{ formatTime(activity.timestamp) }}</time></div>
                  </li>
                  <li v-if="!shadowRunningTrace.length" class="shadow-activity-empty"><span class="activity-node"><i></i></span><div><strong>{{ shadow.available ? (shadow.active ? '正在分析' : '暂无运行轨迹') : '等待 Shadow 可用' }}</strong><span>{{ shadow.available ? '当前运行的动作会固定显示在这个区域。' : '连接后会恢复当前运行活动。' }}</span></div></li>
                </ol>
              </div>
            </section>

            <section class="shadow-activity-section event-section">
              <header><span class="activity-section-icon event-icon">↗</span><div><strong>传递到事件的输出</strong><small>最新 ShadowEvent 公共事件</small></div><em>{{ shadowLatestEventActivity ? 1 : 0 }}</em></header>
              <div class="shadow-event-box">
                <ol class="shadow-activity-list">
                  <li v-if="shadowLatestEventActivity" class="shadow-activity-item" :class="shadowActivityClass(shadowLatestEventActivity)">
                    <span class="activity-node"><i></i></span><div class="activity-copy"><div class="activity-line"><strong>{{ shadowActivityKindLabel(shadowLatestEventActivity) }}</strong><span>{{ shadowActivityPhaseLabel(shadowLatestEventActivity) }}</span></div><p>{{ shadowActivityText(shadowLatestEventActivity) }}</p><time>{{ formatTime(shadowLatestEventActivity.timestamp) }}</time></div>
                  </li>
                  <li v-else class="shadow-activity-empty"><span class="activity-node"><i></i></span><div><strong>{{ shadow.user_input_required ? '等待你的输入' : '暂无事件输出' }}</strong><span>{{ shadow.user_input_required ? 'Shadow 会在收到输入后继续。' : '最新一条进度、观察或结果会固定显示在这个区域。' }}</span></div></li>
                </ol>
              </div>
            </section>
            <section v-if="shadowExpanded" class="shadow-details"><div class="detail-row"><span>运行状态</span><strong>{{ shadowDisplayStatus }}</strong></div><div class="detail-row"><span>当前激活</span><strong>{{ shadow.active ? '处理中' : '已结束' }}</strong></div><div v-if="shadow.user_input_required" class="detail-row"><span>需要操作</span><strong>等待用户输入</strong></div><p>这里仅显示 Shadow 的公开活动、可读摘要和事件状态；原始私有 reasoning、prompt、工具参数与内部 revision 不会进入产品页面。</p></section>
          </section>
        </div>
        <footer class="panel-resource-footer shadow-resource-footer">
          <section class="resource-metrics shadow-resource-metrics" aria-label="Shadow 模型资源与思考强度">
            <div class="resource-meter context-meter" tabindex="0" role="img" :aria-label="shadowContextTooltip" :data-tooltip="shadowContextTooltip">
              <span>上下文占用</span><strong>{{ formatTokens(shadowContext?.used_tokens) }}<small v-if="shadowContext?.limit_tokens != null"> / {{ formatTokens(shadowContext.limit_tokens) }}</small></strong><i :class="{ 'meter-empty': shadowContext?.usage_percent == null }"><b :style="{ width: `${contextPercent(shadowContext?.usage_percent)}%` }"></b></i><em>{{ shadowContext?.usage_percent ?? '—' }}<span v-if="shadowContext?.usage_percent != null">%</span></em>
            </div>
            <div class="thinking-meter" aria-label="Shadow 思考等级固定为 High">
              <div class="thinking-control"><span class="thinking-trigger"><strong>high</strong></span></div>
              <span class="thinking-apply-state">High · 固定</span>
            </div>
            <div class="resource-meter cache-rate-number shadow-cache-rate-number" tabindex="0" role="img" :aria-label="shadowCacheHitRatio === null ? '缓存命中率暂无数据' : `缓存命中率 ${Math.round(shadowCacheHitRatio * 100)}%`" :data-tooltip="shadowCacheHitRatio === null ? '缓存命中率 · 暂无数据' : `缓存命中率 · ${Math.round(shadowCacheHitRatio * 100)}%`"><strong>{{ shadowCacheHitRatio === null ? '—' : `${Math.round(shadowCacheHitRatio * 100)}%` }}</strong></div>
          </section>
        </footer>
      </aside>

      <section class="system-column system-focus-column panel">
        <p class="panel-kicker"><span class="eyebrow-dot"></span>语音运行时</p>
        <div class="system-heading"><div><h2 class="system-state">{{ runtimeMeta.label }}</h2><p class="system-subtitle">{{ runtimeMeta.timestamp }}</p></div><span class="state-chip" :class="runtimeState">{{ runtimeMeta.chip }}</span></div>
        <div class="orb-stage" :class="`orb-${runtimeState}`"><span class="orb-halo halo-one"></span><span class="orb-halo halo-two"></span><span class="orb-halo halo-three"></span><span class="voice-orb"><i></i><i></i><i></i><i></i><i></i></span></div>
        <p class="orb-caption">{{ runtimeMeta.description }}</p>
        <button class="runtime-button" type="button" :disabled="loading || replayMode" @click="runtimeAction">{{ replayMode ? '日志回放中' : connected ? (listening ? '停止当前会话' : '开始语音会话') : '重试连接' }}</button>
        <button v-if="connected && !listening && !replayMode" class="ghost-action" type="button" @click="resetSession">重置当前会话</button>
        <section class="metric-card" :class="{ 'metric-card-live': latencyRunning }"><div class="metric-label"><span>本轮响应</span><button type="button" title="前端计时：从用户输入结束到 TTS 开始播放">i</button></div><strong>{{ formatResponseLatency(latencyMs) }}</strong><p>{{ latencyRunning ? '计时中 · 等待 TTS 开始播放' : '用户说完 → AI 开口' }}</p><div class="metric-spark"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div></section>
        <section class="mode-picker"><div class="section-row"><span>运行模式</span><small>下一轮生效</small></div><div class="mode-options"><button v-for="mode in [{ value: 'fast', label: '快速' }, { value: 'balanced', label: '均衡' }, { value: 'deep', label: '深度' }]" :key="mode.value" type="button" :class="{ selected: reasoningMode === mode.value }" :disabled="!connected || replayMode" @click="setReasoningMode(mode.value as ReasoningMode)">{{ mode.label }}</button></div></section>
        <section class="system-model-config"><div class="section-row"><span>当前模型</span><small>{{ connected ? '已应用运行时' : '等待连接' }}</small></div><div class="system-model-grid"><article class="system-model-card"><p>VOICE</p><strong>{{ activeModels.voice?.name || '未分配' }}</strong><small>{{ activeModels.voice?.modelId || '—' }}</small></article><article class="system-model-card"><p>SHADOW</p><strong>{{ activeModels.shadow?.name || '未分配' }}</strong><small>{{ activeModels.shadow?.modelId || '—' }}</small></article><article class="system-model-card"><p>TTS</p><strong>{{ activeModels.tts?.name || '未分配' }}</strong><small>{{ activeTtsModel?.modelId || (activeModels.tts ? activeModels.tts.model_id : '—') }}</small></article></div></section>
      </section>

      <section class="conversation-column panel">
        <header class="conversation-header"><div><p class="panel-kicker">实时会话</p><h1>{{ hasConversation ? '当前 Voice Terminal 会话' : '准备开始真实对话' }}</h1></div><div class="session-security"><span class="lock-icon">⌁</span><span>本机处理</span></div></header>
        <div ref="conversationScrollElement" class="conversation-stream" :class="{ empty: !hasConversation }" @scroll="captureConversationScrollPosition">
          <div v-if="!hasConversation" class="empty-conversation"><span class="empty-mark">✦</span><strong>{{ runtimeState === 'error' ? '语音服务需要检查' : runtimeState === 'offline' ? '后端离线，页面仍可重试' : '等待真实语音输入' }}</strong><p>{{ runtimeState === 'offline' ? (lastError || '确认 Voice Server 已启动后点击重试。') : '语音、转写、助手回复和 TTS 进度会从真实运行时进入这里。' }}</p><button v-if="runtimeState === 'offline'" class="outline-button" type="button" @click="retry">重试连接</button></div>
          <div v-else class="conversation-stack" :class="{ 'is-tail-anchored': !conversationOverflow }">
            <template v-for="entry in conversationEntries" :key="entry.key">
              <article v-if="entry.role === 'user'" class="message user-message">
                <div class="message-meta"><span class="avatar user-avatar">你</span><span>你</span><time>{{ formatTime(entry.at) }}</time></div>
                <p>{{ entry.text || '（没有可显示的转写）' }}</p>
                <span class="message-status">{{ turnStatusLabel(entry) }}</span>
              </article>
              <article v-else class="message assistant-message speech-history" :class="[{ speaking: entry.speaking }, `speech-${speechStatusKey(entry)}`]">
                <div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ entry.kind === 'system_speech' ? '系统播报' : formatTime(entry.at) }}</time></div>
                <p v-if="entry.text">{{ entry.text }}</p>
                <div class="speech-queue"><span class="queue-dot" :class="{ playing: entry.speaking }"></span>{{ speechStatusLabel(entry) }}<span v-if="entry.delivery?.status"> · {{ deliveryStatusLabel(entry.delivery) }}</span></div>
              </article>
            </template>
            <article v-for="entry in liveSpeechEntries" :key="entry.key" class="message assistant-message speech-history speech-pending" :class="{ speaking: entry.speaking }">
              <div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ pendingSpeechStatusLabel(entry) }}</time></div>
              <p>{{ entry.text }}</p>
              <div class="speech-queue"><span class="queue-dot" :class="{ playing: entry.speaking }"></span>{{ pendingSpeechStatusLabel(entry) }}</div>
            </article>
            <article v-if="liveUserText" class="message user-message live-final"><div class="message-meta"><span class="avatar user-avatar">你</span><span>你</span><time>本轮</time></div><p>{{ liveUserText }}</p><span class="message-status">已完成识别 · 等待回复</span></article>
            <div v-if="current.partial_text" class="live-asr"><span class="live-dot"></span><span>实时识别</span><p>{{ current.partial_text }}</p><span class="typing-bars"><i></i><i></i><i></i></span></div>
            <div v-if="runtimeState === 'thinking' && !current.assistant_text" class="assistant-pending"><span><i></i><i></i><i></i></span>正在准备回复</div>
            <article v-if="liveAssistantText" class="message assistant-message" :class="{ speaking: runtimeState === 'speaking' }"><div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ runtimeState === 'speaking' ? '正在播报' : '本轮' }}</time></div><p>{{ liveAssistantText }}</p><div class="speech-queue"><span class="queue-dot" :class="{ playing: runtimeState === 'speaking' }"></span>{{ runtimeState === 'speaking' ? '正在播放真实 TTS · 随时可以打断' : current.tts_status || '等待播报结算' }}</div></article>
          </div>
        </div>
        <footer class="panel-resource-footer conversation-footer"><div><span class="footer-mic"><i></i>{{ waitingForWake ? 'KWS 正在监听唤醒词' : listening ? '正在聆听你的声音' : connected ? '语音由本机服务处理' : '等待本机 Voice Server' }}</span><span>无输入框 · 通过语音继续 · {{ pageLabel() }}</span></div><section class="resource-metrics conversation-resource-metrics" aria-label="模型资源概览"><div class="resource-meter context-meter" tabindex="0" role="img" :aria-label="conversationContextTooltip" :data-tooltip="conversationContextTooltip"><span>上下文占用</span><strong>{{ formatTokens(conversationContext?.usedTokens) }}<small v-if="conversationContext?.limitTokens != null"> / {{ formatTokens(conversationContext.limitTokens) }}</small></strong><i :class="{ 'meter-empty': conversationContext?.usagePercent == null }"><b :style="{ width: `${contextPercent(conversationContext?.usagePercent)}%` }"></b></i><em>{{ conversationContext?.usagePercent ?? '—' }}<span v-if="conversationContext?.usagePercent != null">%</span></em></div><div class="resource-meter cache-rate-number" tabindex="0" role="img" :aria-label="conversationCacheHitRatio === null ? '缓存命中率暂无数据' : `缓存命中率 ${Math.round(conversationCacheHitRatio * 100)}%`" :data-tooltip="conversationCacheHitRatio === null ? '缓存命中率 · 暂无数据' : `缓存命中率 · ${Math.round(conversationCacheHitRatio * 100)}%`"><strong>{{ conversationCacheHitRatio === null ? '—' : `${Math.round(conversationCacheHitRatio * 100)}%` }}</strong></div></section></footer>
      </section>
    </section>

    <section v-show="activePage === 'settings'" :class="{ 'view-hidden': activePage !== 'settings' }" class="settings-page">
      <header class="settings-heading"><div><p class="panel-kicker">应用设置</p><h1>让助手更像你的工作方式</h1><p>设置通过 Voice Server 产品 API 保存；KWS、Persona、最终静默设置在安全边界后于下一次会话或 Episode 生效。</p></div><span class="settings-badge">{{ connected ? '实时后端已连接' : '后端离线 · 可重试' }}</span></header>
      <div v-if="settings.warnings.length" class="model-safety"><span>!</span><p>{{ settings.warnings.join(' ') }}</p></div>
      <div class="settings-layout">
        <nav class="settings-tabs"><button type="button" :class="{ active: activeSettingsTab === 'wake' }" @click="activeSettingsTab = 'wake'"><span>◌</span><div><strong>KWS 唤醒词</strong><small>启动与待机</small></div></button><button type="button" :class="{ active: activeSettingsTab === 'persona' }" @click="activeSettingsTab = 'persona'"><span>✦</span><div><strong>人格与指令</strong><small>主 LLM 与 Shadow</small></div></button><button type="button" :class="{ active: activeSettingsTab === 'silence' }" @click="activeSettingsTab = 'silence'"><span>◷</span><div><strong>静默等待</strong><small>最终检查与回待机</small></div></button><button type="button" :class="{ active: activeSettingsTab === 'speaker' }" @click="activeSettingsTab = 'speaker'"><span>♬</span><div><strong>声纹识别</strong><small>可选身份增强</small></div></button><button type="button" :class="{ active: activeSettingsTab === 'models' }" @click="activeSettingsTab = 'models'"><span>◇</span><div><strong>模型管理</strong><small>连接、配置与模型池</small></div></button></nav>

        <article class="settings-panel panel">
          <template v-if="activeSettingsTab === 'wake'">
            <header><p class="panel-kicker">KWS 唤醒词</p><h2>让助手在需要时醒来</h2><p>唤醒词由后端现有 sherpa text2token 流程生成并原子校验；支持中文和英文词条。可以给同一个唤醒词配置多个说法（最多 5 个），命中任意一个都算唤醒。生成失败会保留旧词条和旧设置，不打断当前回合。</p></header>
            <div class="settings-stack"><label class="switch-row"><div><strong>启用 KWS</strong><span>开启后本次语音会话持续监听唤醒词；关闭后直接进入语音识别</span></div><input v-model="wakeForm.enabled" type="checkbox"><i></i></label><div class="form-field"><span>唤醒词 <b>{{ wakeForm.keywords.length }} / {{ maxWakeKeywords }}</b></span><div v-for="(item, index) in wakeForm.keywords" :key="`wake-keyword-${index}`" class="wake-keyword-row"><input v-model="wakeForm.keywords[index]" type="text" autocomplete="off" :placeholder="index === 0 ? '例如：七三' : '例如：你好七三'"><button type="button" class="wake-keyword-remove" :disabled="wakeForm.keywords.length <= 1" :aria-label="`删除第 ${index + 1} 个唤醒词`" @click="removeWakeKeyword(index)">×</button></div><button type="button" class="wake-keyword-add" :disabled="wakeForm.keywords.length >= maxWakeKeywords" @click="addWakeKeyword">+ 添加唤醒词</button><div class="range-label"><small>命中任意一个即唤醒</small><small>最多 {{ maxWakeKeywords }} 个说法</small></div><small>建议把称呼说得长一点（如"你好七三""七三同学"），短词在噪声里更容易漏检；支持中文、英文和常见标点，后端会自动生成并验证每个标签。</small></div>
            <label class="form-field"><span>识别灵敏度 <b>{{ wakeForm.sensitivity }}%</b></span><input v-model.number="wakeForm.sensitivity" type="range" min="1" max="99"><div class="range-label"><small>更严格 · 少误触发</small><small>更容易唤醒</small></div><small>灵敏度越高越容易唤醒；实测 75% 以上已是最宽松，低于 50% 基本无法唤醒，建议保持最高。</small></label></div>
            <footer><button class="save-button" type="button" :disabled="saving" @click="saveWake">{{ saving ? '正在保存…' : '保存 KWS 设置' }}</button></footer>
          </template>

          <template v-else-if="activeSettingsTab === 'persona'">
            <header><p class="panel-kicker">人格与指令</p><h2>定义助手如何与你协作</h2><p>仅允许写入四个后端白名单 Markdown 文档。当前 Episode 继续使用已冻结内容，reset 或新 Episode 后读取新内容。</p></header>
            <div class="persona-layout"><nav class="persona-nav"><button v-for="document in personaItems" :key="document.id" type="button" :class="{ active: activePersona === document.id }" @click="activePersona = document.id"><strong>{{ document.label }}</strong><small>{{ document.owner }}</small></button></nav><label class="document-editor"><span>{{ currentDocument.path }}</span><textarea v-model="personaDocuments[currentDocument.id]" spellcheck="false"></textarea><small>UTF-8、无 NUL、单文档不超过 64 KB；保存后下一次 Episode 生效。</small></label></div>
            <label v-if="shadow.backend !== 'dsh'" class="switch-row"><div><strong>实验性 Shadow 上下文门禁</strong><span>只调用侧载模型判断新上下文的注入时机；不删除、不改写正文。关闭后立即恢复全部直通。</span></div><input v-model="settings.shadow.experimental_context_gate_enabled" type="checkbox" :disabled="saving" @change="saveShadowGate"><i></i></label>
            <div v-else class="config-callout"><span class="callout-icon">DSH</span><p>DSH Shadow 使用插件强制注入 Voice 权威上下文，不启用旧 context gate。根模型、provider、reasoning 和凭据由 DSH Harness 管理。</p></div>
            <div class="info-callout"><span>β</span><p>这是实验功能。一次暂缓仅对当次请求有效；下一次请求会重新判断，真正放行时会按原 revision 顺序带入全部正文。</p></div>
            <footer><button class="save-button" type="button" :disabled="saving" @click="savePersona">{{ saving ? '正在保存…' : '保存 Persona 文档' }}</button></footer>
          </template>

          <template v-else-if="activeSettingsTab === 'silence'">
            <header><p class="panel-kicker">最终静默检查</p><h2>留出恰当的等待时间</h2><p>只控制最终静默检查和回到 KWS 的窗口；Smart Turn 的约 1.4 秒短语结束判断保持只读，不会被设置页修改。</p></header>
            <div class="silence-control"><span>最终静默等待</span><div><input v-model.number="silenceSeconds" min="5" max="90" type="number"><b>秒</b></div><input v-model.number="silenceSeconds" min="5" max="90" step="1" type="range"><div class="silence-scale"><small>5 秒</small><small>默认 30 秒</small><small>90 秒</small></div></div><div class="info-callout"><span>i</span><p>Smart Turn 短语结束阈值：{{ settings.silence.smart_turn_hard_silence_ms }} ms（只读）。保存后在下一次 Episode 创建时使用新的最终静默窗口。</p></div>
            <footer><button class="save-button" type="button" :disabled="saving" @click="saveSilence">{{ saving ? '正在保存…' : '保存最终静默设置' }}</button></footer>
          </template>

          <template v-else-if="activeSettingsTab === 'speaker'">
            <header><p class="panel-kicker">可选声纹增强</p><h2>让连续对话更自然</h2><p>声纹只在本地旁路观察和路由，不会阻塞音频采集、VAD、ASR partial 或 final；组件关闭、未注册、超时或出错时自动直通。它不是付款、删除、门锁等高风险操作的身份认证。</p></header>

            <section class="speaker-status-card">
              <div><span class="section-label">STATUS</span><strong>{{ speakerServiceLabel }}</strong><small>{{ String(speakerStatus.model_id || 'WeSpeaker 中文 ResNet34') }} · CPU 独立 Worker</small><small v-if="speakerStatus.last_error" class="speaker-last-error">最近错误：{{ String(speakerStatus.last_error) }}</small></div>
              <div class="speaker-status-detail"><span>{{ speakerHasProfile ? '已注册主人声纹' : '尚未注册声纹' }}</span><span v-if="speakerStatus.worker_connected === true">Worker 已连接</span><span v-else>Worker 未连接</span></div>
            </section>

            <div v-if="speakerNeedsReenrollment" class="info-callout"><span>!</span><p><strong>需要重新注册声纹。</strong>{{ speakerStaleReason }} 特征管线更换后，旧模板与新的分数不再处于同一坐标，系统已停止用它们判定身份（不确定状态一律放行）。请重新录制三段。</p></div>

            <div class="speaker-settings-grid">
              <label class="form-field"><span>运行模式</span><select v-model="speakerForm.mode"><option value="off">关闭</option><option value="observe">测试（只记录，不拦截）</option><option value="filter" :disabled="!speakerFilterAllowed">筛选（只拦截明确非本人）</option></select><small v-if="!speakerFilterAllowed">完成声纹注册且 Worker 就绪后才能选择筛选；不确定状态始终放行。</small><small v-else>首次建议使用测试模式，确认样本表现后再手动切换筛选。</small></label>
              <details class="speaker-advanced"><summary>高级设置 · 阈值基线</summary><div class="speaker-thresholds"><label class="form-field"><span>普通确认阈值 <b>{{ Number(speakerForm.confirm_threshold).toFixed(2) }}</b></span><input v-model.number="speakerForm.confirm_threshold" type="range" min="0.01" max="1" step="0.01"><small>达到此分数并具备连续证据才确认是本人。</small></label><label class="form-field"><span>连续保持阈值 <b>{{ Number(speakerForm.continuity_threshold).toFixed(2) }}</b></span><input v-model.number="speakerForm.continuity_threshold" type="range" min="0" max="0.99" step="0.01"><small>已确认对话中的宽松保持阈值；必须低于普通确认阈值。</small></label><button class="ghost-action" type="button" @click="restoreSpeakerDefaults">恢复默认阈值（0.60 / 0.50）</button></div></details>
            </div>
            <div class="info-callout"><span>!</span><p>阈值只是当前测试基线，不代表安全等级。阈值越宽松越容易连续对话，也越可能误认；第一版不提供防伪和变声检测（anti-spoofing：不可用）。</p></div>

            <section class="speaker-enrollment-card"><div class="speaker-card-heading"><div><p class="section-label">VOICE PROFILE</p><h3>主人声纹模板</h3><p>固定一个逻辑用户，可保存多组带标签模板。原始注册录音在提取成功后立即删除；夹嗓子等特殊说法可以用自定义标签单独测试，但不能当作防伪保证。</p></div><div><button v-if="!speakerEnrollment && !speakerTestRecording" class="outline-button" type="button" @click="startSpeakerEnrollment">开始录制</button><button v-if="!speakerEnrollment && !speakerTestRecording" class="ghost-action" type="button" :disabled="!speakerHasProfile" @click="beginSpeakerTest">现场测试</button><button v-if="speakerTestRecording && speakerServerCapture" class="save-button compact-save" type="button" disabled>正在录制（{{ speakerRecordingSeconds }} 秒）</button><button v-else-if="speakerTestRecording" class="save-button compact-save" type="button" @click="stopSpeakerRecording">停止测试（{{ speakerRecordingSeconds }} 秒）</button></div></div><div v-if="speakerTestResult" class="speaker-test-result"><strong>{{ speakerStateLabel(speakerTestResult.state) }}</strong><span v-if="typeof speakerTestResult.similarity === 'number'">相似度 {{ Number(speakerTestResult.similarity).toFixed(2) }}</span><span v-if="typeof speakerTestResult.threshold_used === 'number'">判定阈值 {{ Number(speakerTestResult.threshold_used).toFixed(2) }}</span><small>与运行时同一口径（2 秒滑窗 + 整段聚合），本次测试不会改变运行模式。</small></div><div v-if="speakerEnrollment" class="speaker-recording-flow"><p>请依次朗读下方三段<strong>不同</strong>的句子，每段 5–10 秒，用平时对助手说话的速度与音量；三段之间保持同一设备与距离。不要反复念同一句，也不要用拖长音的持续元音。</p><p v-if="speakerEnrollment.guidance" class="speaker-guidance">{{ speakerEnrollment.guidance }}</p><div class="speaker-segment-list"><div v-for="(segment, index) in ['第 1 段', '第 2 段', '第 3 段']" :key="segment" class="speaker-segment-row"><span class="speaker-segment-index">0{{ index + 1 }}</span><div><strong>{{ segment }}</strong><small v-if="speakerPrompt(index)" class="speaker-prompt">「{{ speakerPrompt(index) }}」</small><small>{{ speakerEnrollment.segments.includes(index) ? '已接收' : '尚未录制' }}</small></div><input v-if="index === 2" :id="`speaker-label-${index}`" type="text" value="自定义" aria-label="自定义模板标签"><button v-if="speakerRecordingSegment === index && speakerServerCapture" class="save-button compact-save" type="button" disabled>正在录制（{{ speakerRecordingSeconds }} 秒）</button><button v-else-if="speakerRecordingSegment === index" class="save-button compact-save" type="button" @click="stopSpeakerRecording">停止录音（{{ speakerRecordingSeconds }} 秒）</button><button v-else class="outline-button" type="button" :disabled="speakerRecordingSegment !== null" @click="beginSpeakerRecording(index, speakerSegmentLabel(index, segment))">{{ speakerEnrollment.segments.includes(index) ? '重新录制' : '开始录制' }}</button></div></div><footer class="speaker-flow-footer"><span>{{ speakerEnrollment.segments.length }}/3 段已完成</span><div><button class="ghost-action" type="button" @click="cancelSpeakerEnrollment">取消本次录制</button><button class="save-button" type="button" :disabled="!speakerEnrollmentComplete || saving" @click="commitSpeakerEnrollment">提交声纹资料</button></div></footer></div><div v-else-if="speakerTemplates.length" class="speaker-template-list"><article v-for="template in speakerTemplates" :key="String(template.template_id)"><div><strong>{{ String(template.label || '自定义') }}</strong><small>{{ String(template.template_id).slice(0, 8) }} · {{ String(template.created_at || '').replace('T', ' ').slice(0, 16) }}</small></div><span>{{ template.include_in_centroid === false ? '未纳入主中心' : '已纳入主中心' }}</span><button class="ghost-action" type="button" @click="setSpeakerTemplateCentroid(String(template.template_id), template.include_in_centroid === false)">{{ template.include_in_centroid === false ? '纳入主中心' : '移出主中心' }}</button><button class="danger-action" type="button" @click="deleteSpeakerTemplate(String(template.template_id))">删除</button></article></div><div v-else class="pool-empty">还没有模板，请点击“开始录制”。</div></section>

            <section class="speaker-ambient-card"><div class="speaker-card-heading"><div><p class="section-label">AMBIENT CONTEXT</p><h3>当前会话环境记录</h3><p>筛选模式拦截的非本人文本只保存在内存中，不跨会话、不保存音频，也不会自动进入主 Agent。</p></div><div><button class="ghost-action" type="button" @click="refreshAmbientContext">刷新</button><button class="danger-action" type="button" :disabled="!ambientRecords.length" @click="clearAmbientContext">全部清空</button></div></div><div v-if="ambientRecords.length" class="ambient-record-list"><article v-for="record in ambientRecords" :key="String(record.turn_id) + String(record.timestamp)"><time>{{ formatTime(String(record.timestamp || '')) }}</time><p>{{ String(record.text || '') }}</p><small>{{ speakerStateLabel(record.speaker_state) }} · {{ String(record.reason || '路由到环境记录') }}</small></article></div><div v-else class="pool-empty">当前会话还没有被拦截的环境文本。</div></section>

            <footer><button class="save-button" type="button" :disabled="saving || Number(speakerForm.continuity_threshold) >= Number(speakerForm.confirm_threshold)" @click="saveSpeaker">{{ saving ? '正在保存…' : '保存声纹设置' }}</button></footer>
          </template>

          <template v-else>
            <header class="model-header"><div><p class="panel-kicker">模型管理</p><h2>把连接、分配与模型策略分开管理</h2><p>发现 → 选择 → 最小真实验证 → 写入模型池；模型编辑只改产品托管 API 条目的策略，角色只选择模型 ID。</p></div><button class="outline-button" type="button" :disabled="modelScanState === 'scanning'" @click="scanModels">{{ modelScanState === 'scanning' ? '正在扫描…' : '扫描现有配置与受管目录' }}</button></header>
            <div class="model-safety"><span>✓</span><p>API Key 只在本次验证/写入请求的内存生命周期内使用；成功或失败后立即清空，不写入浏览器存储。当前应用失败时旧模型、TTS 和 session 配置保持有效。</p></div>
            <div class="model-accordion-list">
              <section class="model-accordion-card" :class="{ open: expandedModelSections.add }"><button class="model-accordion-toggle" type="button" :aria-expanded="expandedModelSections.add" @click="toggleModelSection('add')"><span class="accordion-index">01</span><span class="accordion-copy"><strong>添加模型</strong><small>连接供应商并完成一次最小能力验证</small></span><span class="accordion-status">{{ providerStep === 'verified' ? '已验证' : '真实 API 流程' }}</span><span class="accordion-chevron" :class="{ open: expandedModelSections.add }">⌄</span></button><transition name="model-accordion"><div v-if="expandedModelSections.add" class="model-card-content"><div class="card-section-heading"><div><p class="section-label">ADD MODEL</p><h3>添加 API 供应商连接</h3></div><span>自动发现 + 自定义兜底</span></div><div class="provider-fields"><label><span>连接名称</span><input v-model="providerForm.name" type="text" autocomplete="off"></label><label><span>服务地址</span><input v-model="providerForm.baseUrl" type="url" autocomplete="url"></label><label><span>协议</span><select v-model="providerForm.protocol"><option value="openai_compatible">OpenAI 兼容</option><option value="deepseek">DeepSeek Responses</option><option value="opencode_go">OpenCode Go Responses</option><option value="responses">Responses 兼容</option><option value="minimax">MiniMax TTS</option><option value="manual">手动模型 ID</option></select></label><label><span>API Key</span><input v-model="providerForm.apiKey" autocomplete="off" placeholder="仅用于本次真实验证" type="password"></label></div><div v-if="providerStep === 'select-model'" class="custom-model-row"><label><span>已发现模型：选择后执行最小能力测试</span><select :value="providerForm.customModelId" @change="selectDiscoveredModel"><option value="">选择一个模型</option><option v-for="modelId in discoveredModels" :key="modelId" :value="modelId">{{ modelId }}</option></select></label><button class="outline-button" type="button" :disabled="!providerForm.customModelId" @click="verifyCustomModel()">最小能力测试</button></div><div v-else-if="providerStep === 'custom-id'" class="custom-model-row"><label><span>服务不支持模型列表：自定义模型 ID</span><input v-model="providerForm.customModelId" placeholder="例如 my-company-chat-01" type="text"></label><button class="outline-button" type="button" @click="verifyCustomModel()">最小能力测试</button></div><div v-else class="provider-actions"><button class="outline-button" type="button" :disabled="providerStep === 'discovering'" @click="discoverProvider">{{ providerStep === 'discovering' ? '正在请求真实服务…' : '发现模型' }}</button><p v-if="providerStep === 'verified'" class="verify-ok">已通过最小真实能力测试。</p></div><div v-if="providerStep === 'verified' && providerForm.protocol !== 'minimax'" class="provider-policy-fields"><label><span>LLM 最大上下文</span><select v-model.number="providerForm.maxContextTokens"><option v-for="value in modelContextOptions" :key="value" :value="value">{{ formatTokens(value) }}</option></select></label><div><span>允许思考等级</span><div class="reasoning-options"><button v-for="level in reasoningOptions" :key="level" type="button" :class="{ active: providerForm.allowedReasoning.includes(level) }" @click="providerForm.allowedReasoning = providerForm.allowedReasoning.includes(level) ? providerForm.allowedReasoning.filter(item => item !== level) : [...providerForm.allowedReasoning, level]">{{ level }}</button></div></div></div><button v-if="providerStep === 'verified'" class="save-button add-model-button" type="button" @click="addVerifiedProvider">验证成功，加入模型池</button></div></transition></section>

              <section class="model-accordion-card" :class="{ open: expandedModelSections.config }"><button class="model-accordion-toggle" type="button" :aria-expanded="expandedModelSections.config" @click="toggleModelSection('config')"><span class="accordion-index">02</span><span class="accordion-copy"><strong>模型配置</strong><small>角色只选择模型 ID，TTS 选择 profile</small></span><span class="accordion-status">{{ modelPoolSummary }}</span><span class="accordion-chevron" :class="{ open: expandedModelSections.config }">⌄</span></button><transition name="model-accordion"><div v-if="expandedModelSections.config" class="model-card-content"><div class="config-callout"><span class="callout-icon">i</span><p>Voice 必须支持 off 和 low；Shadow 使用所选模型允许的真实等级；status provider 始终强制 none。模型应用需要显式确认并可能重启受影响服务。</p></div><div class="assignment-grid assignment-grid-four"><article v-for="role in llmAssignmentSlots" :key="role" class="assignment-slot"><div class="slot-heading"><span class="slot-kind">{{ roleLabels[role].kind }}</span><span class="slot-check">{{ selectedModel(role) ? '✓ 已选择' : '待选择' }}</span></div><div class="slot-copy"><h3>{{ roleLabels[role].title }}</h3><p>{{ roleLabels[role].hint }}</p></div><label class="model-select-field"><span>选择模型</span><select v-model="modelAssignments[role]"><option value="">选择一个已验证模型</option><option v-for="item in availableLlmModels" :key="item.id" :value="item.id">{{ item.provider }} · {{ item.name }}（{{ item.modelId }}）</option></select></label><div class="slot-policy"><span>{{ modelPolicySummary(selectedModel(role)) }}</span><small>{{ modelStatusLabel(selectedModel(role)) }}</small></div></article><article class="assignment-slot assignment-slot-tts"><div class="slot-heading"><span class="slot-kind tts-kind">{{ roleLabels.tts.kind }}</span><span class="slot-check">{{ selectedTtsProfile ? '✓ 已选择' : '待选择' }}</span></div><div class="slot-copy"><h3>{{ roleLabels.tts.title }}</h3><p>{{ roleLabels.tts.hint }}</p></div><label class="model-select-field"><span>选择 TTS profile</span><select v-model="modelAssignments.tts"><option value="">选择一个 profile</option><option v-for="profile in registry.tts_profiles" :key="profile.id" :value="profile.id">{{ profile.name }}（{{ ttsModelId(profile) }}）</option></select></label><div class="slot-policy"><span>{{ selectedTtsProfile?.name || '尚未分配' }}</span><small>{{ selectedTtsProfile ? 'profile' : '待选择' }}</small></div></article></div></div></transition></section>

              <section class="model-accordion-card" :class="{ open: expandedModelSections.pool }"><button class="model-accordion-toggle" type="button" :aria-expanded="expandedModelSections.pool" @click="toggleModelSection('pool')"><span class="accordion-index">03</span><span class="accordion-copy"><strong>模型池</strong><small>编辑产品托管 API 的名称、上下文上限和允许思考等级</small></span><span class="accordion-status">{{ modelPoolSummary }}</span><span class="accordion-chevron" :class="{ open: expandedModelSections.pool }">⌄</span></button><transition name="model-accordion"><div v-if="expandedModelSections.pool" class="model-card-content"><div class="pool-intro"><div><p class="section-label">MODEL POOL</p><h3>已同步模型条目</h3><p>运行时继承模型和本地模型只读；隐藏不会删除本地文件。</p></div><span class="pool-total"><strong>{{ apiModelPool.length }}</strong><small>个 API 条目</small></span></div><section class="pool-subsection"><div class="pool-subsection-heading"><div><span class="pool-section-mark llm-mark">LLM</span><div><h4>文字模型</h4><p>模型级上下文和思考策略</p></div></div><span>{{ apiModelPool.filter(item => item.capability === 'llm').length }} 个</span></div><div v-if="apiModelPool.some(item => item.capability === 'llm')" class="pool-entry-list"><article v-for="item in apiModelPool.filter(entry => entry.capability === 'llm')" :key="item.id" class="pool-entry" :class="{ editing: editingModelId === item.id, 'delete-pending': deleteCandidateId === item.id }"><div class="pool-entry-main"><span class="provider-badge" :class="providerClass(item)">{{ item.provider.slice(0, 1) }}</span><div class="pool-entry-copy"><div class="pool-entry-title"><strong>{{ item.provider }} · {{ item.name }}</strong><span class="capability-tag">LLM</span><span class="entry-status" :class="item.status">{{ modelStatusLabel(item) }}</span></div><span class="pool-entry-id">{{ item.modelId }} · {{ item.origin }}</span></div></div><div class="pool-entry-policy"><span><small>最大上下文</small><strong>{{ formatContext(item.maxContextTokens) }}</strong></span><span><small>允许思考</small><strong>{{ reasoningSummary(item) }}</strong></span></div><div v-if="isEditableApiModel(item)" class="pool-entry-actions"><button type="button" @click="editingModelId === item.id ? editingModelId = null : startEditingModel(item)">{{ editingModelId === item.id ? '收起' : '编辑' }}</button><button class="danger-action" type="button" @click="requestDeleteModel(item)">删除</button></div><span v-else class="readonly-tag">运行时导入</span><div v-if="editingModelId === item.id" class="model-edit-panel"><div class="edit-fields"><label><span>显示名称</span><input v-model="modelEditDraft.name" type="text"></label><label><span>最大上下文</span><select v-model.number="modelEditDraft.maxContextTokens"><option v-for="value in modelContextOptions" :key="value" :value="value">{{ formatTokens(value) }}</option></select></label><fieldset class="reasoning-fieldset"><legend>允许的思考等级</legend><div class="reasoning-chip-list"><button v-for="level in reasoningOptions" :key="level" type="button" :class="{ selected: modelEditDraft.allowedReasoning.includes(level) }" @click="toggleReasoningLevel(level)"><strong>{{ level }}</strong><small>{{ level === 'off' ? '关闭思考' : '可用策略' }}</small></button></div></fieldset></div><div class="edit-panel-footer"><span>稳定模型 ID、供应商 ID 不通过普通编辑接口修改。</span><div><button class="ghost-action" type="button" @click="editingModelId = null">取消</button><button class="save-button compact-save" type="button" @click="saveModelEdit(item)">保存修改</button></div></div></div><div v-if="deleteCandidateId === item.id" class="inline-delete-confirm"><span>确认删除这个未分配的产品托管模型？</span><div><button class="ghost-action" type="button" @click="deleteCandidateId = null">取消</button><button class="confirm-delete-action" type="button" @click="confirmDeleteModel(item)">确认删除</button></div></div></article></div><div v-else class="pool-empty">暂无 LLM 模型条目。</div></section><section class="pool-subsection"><div class="pool-subsection-heading"><div><span class="pool-section-mark tts-mark">TTS</span><div><h4>语音模型</h4><p>使用 profile 进行 TTS 分配</p></div></div><span>{{ apiModelPool.filter(item => item.capability === 'tts').length }} 个</span></div><div v-if="apiModelPool.some(item => item.capability === 'tts')" class="pool-entry-list"><article v-for="item in apiModelPool.filter(entry => entry.capability === 'tts')" :key="item.id" class="pool-entry"><div class="pool-entry-main"><span class="provider-badge" :class="providerClass(item)">{{ item.provider.slice(0, 1) }}</span><div class="pool-entry-copy"><div class="pool-entry-title"><strong>{{ item.provider }} · {{ item.name }}</strong><span class="capability-tag tts-capability">TTS</span><span class="entry-status" :class="item.status">{{ modelStatusLabel(item) }}</span></div><span class="pool-entry-id">{{ item.modelId }} · {{ item.origin }}</span></div></div><div class="pool-entry-policy tts-policy"><span><small>用途</small><strong>语音合成</strong></span><span><small>profile</small><strong>{{ registry.tts_profiles.find(profile => profile.model_id === item.id)?.name || '—' }}</strong></span></div><span class="readonly-tag">{{ item.origin === 'product_managed' ? '产品托管' : '运行时导入' }}</span></article></div><div v-else class="pool-empty">暂无 TTS 模型条目。</div></section><details v-if="hiddenApiModelPool.length" class="local-models-disclosure"><summary><span><strong>已隐藏 API 条目</strong><small>可恢复，但不会自动重新应用</small></span><em>{{ hiddenApiModelPool.length }} 个</em></summary><div class="local-model-list"><article v-for="item in hiddenApiModelPool" :key="item.id" class="local-model-row hidden-row"><span class="provider-badge managed">API</span><div><strong>{{ item.provider }} · {{ item.name }}</strong><small>{{ item.capability }} · {{ item.modelId }}</small></div><button type="button" @click="toggleHidden(item)">恢复</button></article></div></details><details class="local-models-disclosure"><summary><span><strong>本地模型清单</strong><small>受管目录扫描结果，不参与 API 分配</small></span><em>{{ visibleLocalPool.length }} 个可用 · {{ hiddenPool.length }} 个已隐藏</em></summary><div class="local-model-list"><article v-for="item in [...visibleLocalPool, ...hiddenLocalPool]" :key="item.id" class="local-model-row" :class="{ 'hidden-row': item.hidden }"><span class="provider-badge managed">本</span><div><strong>{{ item.name }}</strong><small>{{ item.capability }} · {{ item.path || '受管目录' }}</small></div><button type="button" @click="toggleHidden(item)">{{ item.hidden ? '恢复' : '隐藏' }}</button></article></div></details></div></transition></section>
            </div>
            <footer class="model-settings-footer"><div class="restart-copy"><span v-if="restartState === 'restarting'">正在重启受影响服务…</span><span v-else-if="restartState === 'complete'">✓ 新模型配置已生效</span><span v-else-if="restartState === 'error'">应用失败，旧运行配置已保留</span><span v-else>应用配置会中止聆听/播报并受控重建相关服务。</span></div><button class="save-button" type="button" :disabled="restartState === 'restarting'" @click="openApplyDialog">应用模型配置</button></footer>
          </template>
        </article>
      </div>
    </section>

    <Transition name="save-notice">
      <div v-if="lastError" class="save-notice error-notice" role="alert"><span>!</span>{{ lastError }}<button type="button" aria-label="关闭错误" @click="lastError = ''">×</button></div>
    </Transition>
    <Transition name="save-notice">
      <div v-if="notice" class="save-notice" :class="{ 'is-fading': !noticeVisible }" role="status"><span>✓</span>{{ notice }}<button type="button" aria-label="关闭提示" @click="setNotice('')">×</button></div>
    </Transition>
    <div v-if="showApplyConfirm" class="modal-backdrop"><section class="apply-dialog" role="dialog" aria-modal="true"><p class="panel-kicker">立即应用</p><h2>重启受影响的模型服务？</h2><p>后端将停止当前聆听和 TTS 播放，统一重建 Voice、Shadow、status observer 和 TTS。当前配置只有在重启成功后才会提交。</p><ul><li v-for="item in applyImpact" :key="item">{{ item }}</li></ul><div><button class="outline-button" type="button" @click="showApplyConfirm = false">暂不应用</button><button class="save-button" type="button" @click="confirmApply">确认并立即重启</button></div></section></div>
  </main>
</template>

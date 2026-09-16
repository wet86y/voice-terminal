<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import type { PlaybackQueueItem, ReasoningMode, ShadowActivity, SpeechOutputRecord } from '../types'
import { useProductAdapter, type AssignmentSlot, type ProductSettingsTab, type ProductUiModel } from './useProductAdapter'

const adapter = useProductAdapter()
const {
  voice, page: activePage, settingsTab: activeSettingsTab, settings, loading, saving,
  connected, replayMode, replayMeta, runtimeState, current, conversation, shadow, latencyMs, latencyRunning, wakeForm,
  silenceSeconds, personaDocument: activePersona, personaDocuments, providerForm,
  providerStep, discoveredModels, modelAssignments, uiModels: modelEntries,
  availableLlmModels, availableTtsModels, visibleLocalPool, hiddenPool,
  managedApiModelPool, selectedModels, activeModels, selectedTtsProfile, selectedTtsModel, activeTtsModel, registry, shadowThinking, shadowThinkingCommitted, shadowThinkingPending, shadowThinkingOptions,
  shadowExpanded, thinkingExpanded, modelScanState, restartState, applyImpact,
  lastError, notice, noticeVisible, runtimeAction, resetSession, setReasoningMode, setShadowThinking,
  toggleProviderReasoning,
  saveWake, saveSilence, saveShadowGate, savePersona, scanModels, discoverProvider, verifyCustomModel,
  addVerifiedProvider,
  requestModelApply, confirmModelApply, retry, setNotice,
} = adapter
const reasoningMode = voice.reasoningMode
const listening = voice.listening
const speechOutputs = voice.speechOutputs
const playbackQueue = voice.playbackQueue
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
const hasConversation = computed(() => conversation.value.length > 0
  || speechOutputs.value.length > 0
  || Boolean(current.value.partial_text || current.value.final_text || current.value.assistant_text || current.value.tts_text))
const standaloneSpeechOutputs = computed(() => speechOutputs.value.filter(output => (
  typeof output.text === 'string' && output.text.trim()
  && !conversation.value.some(record => record.turn_id === output.turn_id)
)))
const standalonePlaybackItems = computed(() => playbackQueue.value.filter(item => (
  typeof item.text === 'string' && item.text.trim()
  && !speechOutputExists(item)
  && !conversation.value.some(record => record.turn_id === item.turn_id)
)))
const currentTurnHasSpeechTrack = computed(() => {
  const turnId = voice.currentTurnId.value
  return Boolean(turnId && hasSpeechTrackForTurn(turnId))
})
const currentThinkingModel = computed(() => selectedModels.value.shadow)
const thinkingOptionLabels: Record<string, { label: string; shortLabel: string }> = {
  off: { label: '关闭', shortLabel: 'Off' },
  low: { label: '低', shortLabel: 'Low' },
  medium: { label: '中', shortLabel: 'Medium' },
  high: { label: '高', shortLabel: 'High' },
  max: { label: '最大', shortLabel: 'Max' },
}
const currentThinkingOption = computed(() => {
  const options = shadowThinkingOptions.value.length ? shadowThinkingOptions.value : ['off']
  const value = options.includes(shadowThinking.value) ? shadowThinking.value : options[options.length - 1]
  return { value, ...(thinkingOptionLabels[value] || { label: value, shortLabel: value }) }
})
const currentThinkingIndex = computed(() => {
  const index = shadowThinkingOptions.value.indexOf(currentThinkingOption.value.value)
  return Math.max(0, index)
})
const thinkingProgress = computed(() => {
  const count = shadowThinkingOptions.value.length
  return count > 1 ? (currentThinkingIndex.value / (count - 1)) * 100 : 100
})
const thinkingSliderStyle = computed(() => ({ '--thinking-progress': `${thinkingProgress.value}%` }))
const shadowContext = computed(() => {
  const context = shadow.value.context
  if (!context || (context.used_tokens == null && context.limit_tokens == null && context.usage_percent == null)) return null
  return context
})
const shadowCacheHitRatio = computed(() => {
  const ratio = shadow.value.provider?.cache_hit_ratio
  return typeof ratio === 'number' && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1 ? ratio : null
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
const selectedShadowReasoning = computed(() => shadowThinking.value || shadowThinkingOptions.value[0] || 'off')

const shadowActivities = computed(() => shadow.value.activities || [])
const shadowScrollElement = ref<HTMLElement | null>(null)
let shadowFollowTail: boolean | null = null

function captureShadowScrollPosition() {
  const element = shadowScrollElement.value
  if (!element) return
  shadowFollowTail = element.scrollHeight - element.scrollTop - element.clientHeight < 24
}

watch(shadowActivities, async () => {
  const elementBeforeUpdate = shadowScrollElement.value
  if (shadowFollowTail === null && elementBeforeUpdate) {
    shadowFollowTail = elementBeforeUpdate.scrollHeight
      - elementBeforeUpdate.scrollTop - elementBeforeUpdate.clientHeight < 24
  }
  if (!shadowFollowTail) return
  await nextTick()
  if (!shadowFollowTail) return
  const element = shadowScrollElement.value
  if (element) element.scrollTop = element.scrollHeight
}, { deep: true })

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

const shadowThinkingActivities = computed(() => {
  const values = shadowActivities.value.filter(item => item.stream === 'thinking' || item.stream === 'lifecycle')
  return (values.length ? values : (shadow.value.active ? [] : [])).slice(shadowExpanded.value ? -96 : -8)
})
const shadowToolActivities = computed(() => shadowActivities.value
  .filter(item => item.stream === 'tool')
  .slice(shadowExpanded.value ? -96 : -8))
const shadowEventActivities = computed(() => {
  const values = shadowActivities.value.filter(item => item.stream === 'event')
  return (values.length ? values : legacyShadowEvents()).slice(shadowExpanded.value ? -96 : -8)
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

function setShadowThinkingFromSlider(event: Event, collapse = false) {
  const slider = event.target as HTMLInputElement
  const option = shadowThinkingOptions.value[Number(slider.value)]
  if (!option) return
  shadowThinking.value = option
  if (collapse) {
    setShadowThinking(option)
    thinkingExpanded.value = false
  }
}

function setShadowThinkingFromTrackClick(event: MouseEvent) {
  const slider = event.currentTarget as HTMLInputElement
  const bounds = slider.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width))
  const optionIndex = Math.round(ratio * (shadowThinkingOptions.value.length - 1))
  const option = shadowThinkingOptions.value[optionIndex]
  if (!option) return
  setShadowThinking(option)
  thinkingExpanded.value = false
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

function speechOutputsForTurn(turnId: string) {
  return speechOutputs.value.filter(output => output.turn_id === turnId
    && typeof output.text === 'string' && output.text.trim())
}

function speechOutputExists(item: Pick<PlaybackQueueItem, 'conversation_id' | 'speech_id'>) {
  return speechOutputs.value.some(output => output.speech_id === item.speech_id
    && output.conversation_id === item.conversation_id)
}

function playbackItemsForTurn(turnId: string) {
  return playbackQueue.value.filter(item => item.turn_id === turnId
    && typeof item.text === 'string' && item.text.trim() && !speechOutputExists(item))
}

function hasSpeechTrackForTurn(turnId: string) {
  return speechOutputsForTurn(turnId).length > 0 || playbackItemsForTurn(turnId).length > 0
}

function speechOutputStatusLabel(output: SpeechOutputRecord) {
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
  const status = typeof output.status === 'string' ? output.status : ''
  return labels[status.toLowerCase()] || status || '已记录'
}

function speechOutputStatusKey(output: SpeechOutputRecord) {
  return typeof output.status === 'string' && output.status ? output.status.toLowerCase() : 'recorded'
}

function speechOutputIsPlaying(output: SpeechOutputRecord) {
  return voice.playbackQueue.value.some(item => item.speech_id === output.speech_id
    && item.conversation_id === output.conversation_id && item.state === 'playing')
}

function playbackItemStatusLabel(item: PlaybackQueueItem) {
  return item.state === 'playing' ? '正在播报' : '等待播放'
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
        <div ref="shadowScrollElement" class="shadow-scroll" @scroll="captureShadowScrollPosition">
          <section class="shadow-summary-card" :class="`status-${shadowStatusClass}`">
            <div class="shadow-summary-heading">
              <div><p class="section-label">当前 Shadow 摘要</p><h3>{{ shadow.summary || (shadow.available ? '等待 Shadow 生成本轮可读摘要。' : 'Shadow Agent 当前不可用') }}</h3></div>
              <span class="shadow-status-pill" :class="shadowStatusClass"><i></i>{{ shadowDisplayStatus }}</span>
            </div>
            <p class="shadow-focus-line"><span>当前关注</span>{{ shadow.focus || (shadow.available ? '等待真实会话上下文' : '后端没有提供可用 Shadow 运行时') }}</p>
            <div class="shadow-summary-meta"><span>{{ shadow.active ? '运行中' : shadow.user_input_required ? '等待你的输入' : '最近更新' }}</span><time>{{ shadowLastActivityAt ? formatTime(shadowLastActivityAt) : '尚无活动记录' }}</time></div>
          </section>

          <section class="shadow-runtime-card" :class="`status-${shadowStatusClass}`">
            <header class="shadow-runtime-heading">
              <div><p class="section-label">Agent 运行轨迹</p><h3>{{ shadow.active ? 'Shadow 正在处理' : shadowDisplayStatus }}</h3></div>
              <span class="shadow-run-indicator" :class="shadowStatusClass"><i></i>{{ shadow.active ? 'LIVE' : shadowDisplayStatus }}</span>
            </header>
            <div class="shadow-runtime-sections">
              <section class="shadow-activity-section thinking-section">
                <header><span class="activity-section-icon thinking-icon">✦</span><div><strong>思考内容</strong><small>可读摘要与运行阶段</small></div><em>{{ shadowThinkingActivities.length }}</em></header>
                <ol class="shadow-activity-list">
                  <li v-for="activity in shadowThinkingActivities" :key="`${activity.run_seq}-${activity.item_id}`" class="shadow-activity-item" :class="shadowActivityClass(activity)">
                    <span class="activity-node"><i></i></span><div class="activity-copy"><div class="activity-line"><strong>{{ shadowActivityText(activity) }}</strong><span>{{ shadowActivityPhaseLabel(activity) }}</span></div><time>{{ formatTime(activity.timestamp) }}</time></div>
                  </li>
                  <li v-if="!shadowThinkingActivities.length" class="shadow-activity-empty"><span class="activity-node"><i></i></span><div><strong>{{ shadow.available ? (shadow.active ? '正在分析' : '暂无可读摘要') : '等待 Shadow 可用' }}</strong><span>{{ shadow.available ? '运行过程中会在这里显示阶段与摘要增量。' : '连接后会恢复当前运行活动。' }}</span></div></li>
                </ol>
              </section>

              <section class="shadow-activity-section tool-section">
                <header><span class="activity-section-icon tool-icon">⌘</span><div><strong>工具调用情况</strong><small>准备、执行与结果</small></div><em>{{ shadowToolActivities.length }}</em></header>
                <ol class="shadow-activity-list">
                  <li v-for="activity in shadowToolActivities" :key="`${activity.run_seq}-${activity.item_id}`" class="shadow-activity-item" :class="shadowActivityClass(activity)">
                    <span class="activity-node"><i></i></span><div class="activity-copy"><div class="activity-line"><strong>{{ activity.tool_name || '未知工具' }}</strong><span>{{ shadowActivityPhaseLabel(activity) }}</span></div><p>{{ shadowActivityText(activity) }}</p><time>{{ formatTime(activity.timestamp) }}</time></div>
                  </li>
                  <li v-if="!shadowToolActivities.length" class="shadow-activity-empty"><span class="activity-node"><i></i></span><div><strong>暂无工具调用</strong><span>工具开始准备后会保留调用顺序。</span></div></li>
                </ol>
              </section>

              <section class="shadow-activity-section event-section">
                <header><span class="activity-section-icon event-icon">↗</span><div><strong>传递到事件的输出</strong><small>ShadowEvent 公共事件</small></div><em>{{ shadowEventActivities.length }}</em></header>
                <ol class="shadow-activity-list">
                  <li v-for="activity in shadowEventActivities" :key="`${activity.run_seq}-${activity.item_id}`" class="shadow-activity-item" :class="shadowActivityClass(activity)">
                    <span class="activity-node"><i></i></span><div class="activity-copy"><div class="activity-line"><strong>{{ shadowActivityKindLabel(activity) }}</strong><span>{{ shadowActivityPhaseLabel(activity) }}</span></div><p>{{ shadowActivityText(activity) }}</p><time>{{ formatTime(activity.timestamp) }}</time></div>
                  </li>
                  <li v-if="!shadowEventActivities.length" class="shadow-activity-empty"><span class="activity-node"><i></i></span><div><strong>{{ shadow.user_input_required ? '等待你的输入' : '暂无事件输出' }}</strong><span>{{ shadow.user_input_required ? 'Shadow 会在收到输入后继续。' : '进度、观察和结果会按时间顺序出现在这里。' }}</span></div></li>
                </ol>
              </section>
            </div>
            <section v-if="shadowExpanded" class="shadow-details"><div class="detail-row"><span>运行状态</span><strong>{{ shadowDisplayStatus }}</strong></div><div class="detail-row"><span>当前激活</span><strong>{{ shadow.active ? '处理中' : '已结束' }}</strong></div><div v-if="shadow.user_input_required" class="detail-row"><span>需要操作</span><strong>等待用户输入</strong></div><p>这里仅显示 Shadow 的公开活动、可读摘要和事件状态；原始私有 reasoning、prompt、工具参数与内部 revision 不会进入产品页面。</p></section>
          </section>
        </div>
        <footer class="panel-resource-footer shadow-resource-footer">
          <section class="resource-metrics shadow-resource-metrics" aria-label="Shadow 模型资源与思考强度">
            <div class="resource-meter context-meter" tabindex="0" role="img" :aria-label="shadowContextTooltip" :data-tooltip="shadowContextTooltip">
              <span>上下文占用</span><strong>{{ formatTokens(shadowContext?.used_tokens) }}<small v-if="shadowContext?.limit_tokens != null"> / {{ formatTokens(shadowContext.limit_tokens) }}</small></strong><i :class="{ 'meter-empty': shadowContext?.usage_percent == null }"><b :style="{ width: `${contextPercent(shadowContext?.usage_percent)}%` }"></b></i><em>{{ shadowContext?.usage_percent ?? '—' }}<span v-if="shadowContext?.usage_percent != null">%</span></em>
            </div>
            <div class="thinking-meter">
              <div class="thinking-control">
                <button class="thinking-trigger" type="button" :aria-expanded="thinkingExpanded" :aria-label="`Shadow 思考等级：${currentThinkingOption.label}`" @click="thinkingExpanded = !thinkingExpanded">
                  <strong>{{ currentThinkingOption.shortLabel.toLowerCase() }}</strong><i aria-hidden="true">⌄</i>
                </button>
                <div v-if="thinkingExpanded" class="thinking-popover" role="dialog" aria-label="Shadow 思考等级设置">
                  <div class="thinking-popover-header">
                    <div><strong>{{ currentThinkingOption.label }}</strong><span>{{ currentThinkingModel?.name || '当前模型' }} · 已提交 {{ thinkingOptionLabels[shadowThinkingCommitted]?.shortLabel || shadowThinkingCommitted }}</span></div>
                    <button type="button" aria-label="收起思考强度设置" @click="thinkingExpanded = false">×</button>
                  </div>
                  <input class="thinking-slider" type="range" :min="0" :max="Math.max(shadowThinkingOptions.length - 1, 0)" step="1" :value="currentThinkingIndex" :style="thinkingSliderStyle" :aria-valuetext="currentThinkingOption.label" aria-label="选择思考强度" @input="setShadowThinkingFromSlider($event)" @change="setShadowThinkingFromSlider($event, true)" @click="setShadowThinkingFromTrackClick">
                  <div class="thinking-scale" aria-hidden="true"><span v-for="(level, index) in shadowThinkingOptions" :key="level" :class="{ active: index === currentThinkingIndex }">{{ thinkingOptionLabels[level]?.shortLabel || level }}</span></div>
                </div>
              </div>
              <span class="thinking-apply-state" :class="{ pending: shadowThinkingPending }">{{ shadowThinkingPending ? '待应用' : '已提交' }}</span>
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
        <div class="conversation-stream" :class="{ empty: !hasConversation }">
          <div v-if="!hasConversation" class="empty-conversation"><span class="empty-mark">✦</span><strong>{{ runtimeState === 'error' ? '语音服务需要检查' : runtimeState === 'offline' ? '后端离线，页面仍可重试' : '等待真实语音输入' }}</strong><p>{{ runtimeState === 'offline' ? (lastError || '确认 Voice Server 已启动后点击重试。') : '语音、转写、助手回复和 TTS 进度会从真实运行时进入这里。' }}</p><button v-if="runtimeState === 'offline'" class="outline-button" type="button" @click="retry">重试连接</button></div>
          <template v-else>
            <template v-for="record in conversation" :key="record.turn_id">
              <article class="message user-message"><div class="message-meta"><span class="avatar user-avatar">你</span><span>你</span><time>{{ formatTime(record.completed_at) }}</time></div><p>{{ record.transcript || '（没有可显示的转写）' }}</p><span class="message-status">{{ record.status || record.delivery?.status || '已记录' }}</span></article>
              <template v-if="hasSpeechTrackForTurn(record.turn_id)">
                <article v-for="output in speechOutputsForTurn(record.turn_id)" :key="`speech-${output.conversation_id}-${output.speech_id}`" class="message assistant-message speech-history" :class="[{ speaking: speechOutputIsPlaying(output) }, `speech-${speechOutputStatusKey(output)}`]">
                  <div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ formatTime(record.completed_at) }}</time></div>
                  <p>{{ output.text }}</p>
                  <div class="speech-queue"><span class="queue-dot" :class="{ playing: speechOutputIsPlaying(output) }"></span>{{ speechOutputStatusLabel(output) }}<span v-if="output.delivery?.status"> · {{ deliveryStatusLabel(output.delivery) }}</span></div>
                </article>
                <article v-for="item in playbackItemsForTurn(record.turn_id)" :key="`pending-speech-${item.conversation_id}-${item.speech_id}`" class="message assistant-message speech-history speech-pending" :class="{ speaking: item.state === 'playing' }">
                  <div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ playbackItemStatusLabel(item) }}</time></div>
                  <p>{{ item.text }}</p>
                  <div class="speech-queue"><span class="queue-dot" :class="{ playing: item.state === 'playing' }"></span>{{ playbackItemStatusLabel(item) }}</div>
                </article>
              </template>
              <article v-else-if="record.response" class="message assistant-message"><div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ formatTime(record.completed_at) }}</time></div><p>{{ record.response }}</p><div class="speech-queue"><span class="queue-dot"></span>已完成本轮播报结算</div></article>
            </template>
            <article v-for="output in standaloneSpeechOutputs" :key="`standalone-speech-${output.conversation_id}-${output.speech_id}`" class="message assistant-message speech-history" :class="[{ speaking: speechOutputIsPlaying(output) }, `speech-${speechOutputStatusKey(output)}`]">
              <div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>已结算</time></div>
              <p>{{ output.text }}</p>
              <div class="speech-queue"><span class="queue-dot" :class="{ playing: speechOutputIsPlaying(output) }"></span>{{ speechOutputStatusLabel(output) }}<span v-if="output.delivery?.status"> · {{ deliveryStatusLabel(output.delivery) }}</span></div>
            </article>
            <article v-for="item in standalonePlaybackItems" :key="`standalone-pending-speech-${item.conversation_id}-${item.speech_id}`" class="message assistant-message speech-history speech-pending" :class="{ speaking: item.state === 'playing' }">
              <div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ playbackItemStatusLabel(item) }}</time></div>
              <p>{{ item.text }}</p>
              <div class="speech-queue"><span class="queue-dot" :class="{ playing: item.state === 'playing' }"></span>{{ playbackItemStatusLabel(item) }}</div>
            </article>
            <article v-if="current.final_text && !conversation.some(item => item.transcript === current.final_text)" class="message user-message live-final"><div class="message-meta"><span class="avatar user-avatar">你</span><span>你</span><time>本轮</time></div><p>{{ current.final_text }}</p><span class="message-status">已完成识别 · 等待回复</span></article>
            <div v-if="current.partial_text" class="live-asr"><span class="live-dot"></span><span>实时识别</span><p>{{ current.partial_text }}</p><span class="typing-bars"><i></i><i></i><i></i></span></div>
            <div v-if="runtimeState === 'thinking' && !current.assistant_text" class="assistant-pending"><span><i></i><i></i><i></i></span>正在准备回复</div>
            <article v-if="current.assistant_text && !conversation.some(item => item.response === current.assistant_text) && !currentTurnHasSpeechTrack" class="message assistant-message" :class="{ speaking: runtimeState === 'speaking' }"><div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ runtimeState === 'speaking' ? '正在播报' : '本轮' }}</time></div><p>{{ current.assistant_text }}</p><div class="speech-queue"><span class="queue-dot" :class="{ playing: runtimeState === 'speaking' }"></span>{{ runtimeState === 'speaking' ? '正在播放真实 TTS · 随时可以打断' : current.tts_status || '等待播报结算' }}</div></article>
          </template>
        </div>
        <footer class="panel-resource-footer conversation-footer"><div><span class="footer-mic"><i></i>{{ waitingForWake ? 'KWS 正在监听唤醒词' : listening ? '正在聆听你的声音' : connected ? '语音由本机服务处理' : '等待本机 Voice Server' }}</span><span>无输入框 · 通过语音继续 · {{ pageLabel() }}</span></div><section class="resource-metrics conversation-resource-metrics" aria-label="模型资源概览"><div class="resource-meter context-meter" tabindex="0" role="img" :aria-label="conversationContextTooltip" :data-tooltip="conversationContextTooltip"><span>上下文占用</span><strong>{{ formatTokens(conversationContext?.usedTokens) }}<small v-if="conversationContext?.limitTokens != null"> / {{ formatTokens(conversationContext.limitTokens) }}</small></strong><i :class="{ 'meter-empty': conversationContext?.usagePercent == null }"><b :style="{ width: `${contextPercent(conversationContext?.usagePercent)}%` }"></b></i><em>{{ conversationContext?.usagePercent ?? '—' }}<span v-if="conversationContext?.usagePercent != null">%</span></em></div><div class="resource-meter cache-rate-number" tabindex="0" role="img" :aria-label="conversationCacheHitRatio === null ? '缓存命中率暂无数据' : `缓存命中率 ${Math.round(conversationCacheHitRatio * 100)}%`" :data-tooltip="conversationCacheHitRatio === null ? '缓存命中率 · 暂无数据' : `缓存命中率 · ${Math.round(conversationCacheHitRatio * 100)}%`"><strong>{{ conversationCacheHitRatio === null ? '—' : `${Math.round(conversationCacheHitRatio * 100)}%` }}</strong></div></section></footer>
      </section>
    </section>

    <section v-show="activePage === 'settings'" :class="{ 'view-hidden': activePage !== 'settings' }" class="settings-page">
      <header class="settings-heading"><div><p class="panel-kicker">应用设置</p><h1>让助手更像你的工作方式</h1><p>设置通过 Voice Server 产品 API 保存；KWS、Persona、最终静默设置在安全边界后于下一次会话或 Episode 生效。</p></div><span class="settings-badge">{{ connected ? '实时后端已连接' : '后端离线 · 可重试' }}</span></header>
      <div v-if="settings.warnings.length" class="model-safety"><span>!</span><p>{{ settings.warnings.join(' ') }}</p></div>
      <div class="settings-layout">
        <nav class="settings-tabs"><button type="button" :class="{ active: activeSettingsTab === 'wake' }" @click="activeSettingsTab = 'wake'"><span>◌</span><div><strong>KWS 唤醒词</strong><small>启动与待机</small></div></button><button type="button" :class="{ active: activeSettingsTab === 'persona' }" @click="activeSettingsTab = 'persona'"><span>✦</span><div><strong>人格与指令</strong><small>主 LLM 与 Shadow</small></div></button><button type="button" :class="{ active: activeSettingsTab === 'silence' }" @click="activeSettingsTab = 'silence'"><span>◷</span><div><strong>静默等待</strong><small>最终检查与回待机</small></div></button><button type="button" :class="{ active: activeSettingsTab === 'models' }" @click="activeSettingsTab = 'models'"><span>◇</span><div><strong>模型管理</strong><small>连接、配置与模型池</small></div></button></nav>

        <article class="settings-panel panel">
          <template v-if="activeSettingsTab === 'wake'">
            <header><p class="panel-kicker">KWS 唤醒词</p><h2>让助手在需要时醒来</h2><p>唤醒词由后端现有 sherpa text2token 流程生成并原子校验；支持中文和英文词条。生成失败会保留旧词条和旧设置，不打断当前回合。</p></header>
            <div class="settings-stack"><label class="switch-row"><div><strong>启用 KWS</strong><span>开启后本次语音会话持续监听唤醒词；关闭后直接进入语音识别</span></div><input v-model="wakeForm.enabled" type="checkbox"><i></i></label><label class="form-field"><span>唤醒词</span><input v-model="wakeForm.keyword" type="text" autocomplete="off"><small>支持中文、英文和常见标点；后端会自动生成并验证标签。</small></label><label class="form-field"><span>识别灵敏度 <b>{{ wakeForm.sensitivity }}%</b></span><input v-model.number="wakeForm.sensitivity" type="range" min="1" max="99"><div class="range-label"><small>更严格 · 少误触发</small><small>更容易唤醒</small></div><small>灵敏度越高越容易唤醒；建议先使用 60–80%。</small></label></div>
            <footer><button class="save-button" type="button" :disabled="saving" @click="saveWake">{{ saving ? '正在保存…' : '保存 KWS 设置' }}</button></footer>
          </template>

          <template v-else-if="activeSettingsTab === 'persona'">
            <header><p class="panel-kicker">人格与指令</p><h2>定义助手如何与你协作</h2><p>仅允许写入四个后端白名单 Markdown 文档。当前 Episode 继续使用已冻结内容，reset 或新 Episode 后读取新内容。</p></header>
            <div class="persona-layout"><nav class="persona-nav"><button v-for="document in personaItems" :key="document.id" type="button" :class="{ active: activePersona === document.id }" @click="activePersona = document.id"><strong>{{ document.label }}</strong><small>{{ document.owner }}</small></button></nav><label class="document-editor"><span>{{ currentDocument.path }}</span><textarea v-model="personaDocuments[currentDocument.id]" spellcheck="false"></textarea><small>UTF-8、无 NUL、单文档不超过 64 KB；保存后下一次 Episode 生效。</small></label></div>
            <label class="switch-row"><div><strong>实验性 Shadow 上下文门禁</strong><span>只调用侧载模型判断新上下文的注入时机；不删除、不改写正文。关闭后立即恢复全部直通。</span></div><input v-model="settings.shadow.experimental_context_gate_enabled" type="checkbox" :disabled="saving" @change="saveShadowGate"><i></i></label>
            <div class="info-callout"><span>β</span><p>这是实验功能。一次暂缓仅对当次请求有效；下一次请求会重新判断，真正放行时会按原 revision 顺序带入全部正文。</p></div>
            <footer><button class="save-button" type="button" :disabled="saving" @click="savePersona">{{ saving ? '正在保存…' : '保存 Persona 文档' }}</button></footer>
          </template>

          <template v-else-if="activeSettingsTab === 'silence'">
            <header><p class="panel-kicker">最终静默检查</p><h2>留出恰当的等待时间</h2><p>只控制最终静默检查和回到 KWS 的窗口；Smart Turn 的约 1.4 秒短语结束判断保持只读，不会被设置页修改。</p></header>
            <div class="silence-control"><span>最终静默等待</span><div><input v-model.number="silenceSeconds" min="5" max="90" type="number"><b>秒</b></div><input v-model.number="silenceSeconds" min="5" max="90" step="1" type="range"><div class="silence-scale"><small>5 秒</small><small>默认 30 秒</small><small>90 秒</small></div></div><div class="info-callout"><span>i</span><p>Smart Turn 短语结束阈值：{{ settings.silence.smart_turn_hard_silence_ms }} ms（只读）。保存后在下一次 Episode 创建时使用新的最终静默窗口。</p></div>
            <footer><button class="save-button" type="button" :disabled="saving" @click="saveSilence">{{ saving ? '正在保存…' : '保存最终静默设置' }}</button></footer>
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

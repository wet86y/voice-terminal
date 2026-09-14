<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import {
  capabilityLabels,
  demoModelEntries,
  modelContextOptions,
  originLabels,
  primaryPersonaDocuments,
  reasoningEffortOptions,
  runtimePresentation,
  stateChoices,
  type DemoContextTokens,
  type DemoModelEntry,
  type DemoModelStatus,
  type DemoReasoningEffort,
  type DemoRuntimeState,
  type ReasoningMode,
} from './demo-data'

type PageName = 'home' | 'settings'
type SettingsTab = 'wake' | 'persona' | 'silence' | 'models'
type AssignmentSlot = 'voice' | 'shadow' | 'status' | 'tts'
type LlmAssignmentSlot = Exclude<AssignmentSlot, 'tts'>
type ModelSection = 'add' | 'config' | 'pool'

const activePage = ref<PageName>('home')
const runtimeState = ref<DemoRuntimeState>('standby')
const reasoningMode = ref<ReasoningMode>('balanced')
const shadowThinking = ref<DemoReasoningEffort>('high')
const shadowExpanded = ref(false)
const thinkingExpanded = ref(false)
const activeSettingsTab = ref<SettingsTab>('wake')
const activePersona = ref('instructions')
const saveNotice = ref('')
const showApplyConfirm = ref(false)
const restartState = ref<'idle' | 'restarting' | 'complete'>('idle')

const wakeSettings = reactive({ enabled: true, alwaysOn: true, keyword: '嘿，小爪', threshold: 72 })
const silenceSeconds = ref(30)
const personaDocuments = reactive(
  Object.fromEntries(primaryPersonaDocuments.map(item => [item.id, item.value])) as Record<string, string>,
)

const modelEntries = reactive(demoModelEntries.map(item => ({ ...item })) as DemoModelEntry[])
const modelScanState = ref<'idle' | 'scanning' | 'complete'>('complete')
const providerStep = ref<'idle' | 'discovering' | 'custom-id' | 'verified'>('idle')
const providerForm = reactive({
  name: '我的 OpenAI 兼容服务',
  baseUrl: 'https://api.example.com/v1',
  protocol: 'OpenAI 兼容',
  apiKey: '',
  customModelId: '',
})

const modelAssignments = reactive<Record<AssignmentSlot, string>>({
  voice: 'runtime-voice-llm',
  shadow: 'runtime-shadow-llm',
  status: 'managed-reasoning',
  tts: 'runtime-tts',
})

const expandedModelSections = reactive<Record<ModelSection, boolean>>({
  add: true,
  config: true,
  pool: false,
})

const editingModelId = ref<string | null>(null)
const deleteCandidateId = ref<string | null>(null)
const modelEditDraft = reactive<{
  name: string
  modelId: string
  maxContextTokens: DemoContextTokens
  allowedReasoning: DemoReasoningEffort[]
}>({
  name: '',
  modelId: '',
  maxContextTokens: 131072,
  allowedReasoning: ['off'],
})

const currentRuntime = computed(() => runtimePresentation[runtimeState.value])
const currentDocument = computed(
  () => primaryPersonaDocuments.find(item => item.id === activePersona.value) ?? primaryPersonaDocuments[0],
)
const hasConversation = computed(() => !['offline', 'standby', 'error'].includes(runtimeState.value))
const isListening = computed(() => runtimeState.value === 'listening')
const assistantIsSpeaking = computed(() => runtimeState.value === 'speaking')
const currentThinkingModel = computed(() => selectedModel('shadow'))
const thinkingOptions = computed(() => {
  const allowed = currentThinkingModel.value?.allowedReasoning
  if (!allowed?.length) return reasoningEffortOptions
  const filtered = reasoningEffortOptions.filter(option => allowed.includes(option.value))
  return filtered.length ? filtered : reasoningEffortOptions
})
const currentThinkingOption = computed(
  () => thinkingOptions.value.find(option => option.value === shadowThinking.value) ?? thinkingOptions.value[thinkingOptions.value.length - 1] ?? reasoningEffortOptions[0],
)
const currentThinkingIndex = computed(() => Math.max(0, thinkingOptions.value.findIndex(option => option.value === currentThinkingOption.value.value)))
const thinkingProgress = computed(() => thinkingOptions.value.length > 1 ? (currentThinkingIndex.value / (thinkingOptions.value.length - 1)) * 100 : 100)
const thinkingSliderStyle = computed(() => ({ '--thinking-progress': `${thinkingProgress.value}%` }))

const availableLlmModels = computed(() =>
  modelEntries.filter(item => item.capability === 'llm' && !item.hidden),
)
const availableTtsModels = computed(() =>
  modelEntries.filter(item => item.capability === 'tts' && !item.hidden),
)
const apiModelPool = computed(() =>
  modelEntries.filter(
    item => (item.capability === 'llm' || item.capability === 'tts') && !item.hidden,
  ),
)
const managedApiModelPool = computed(() => apiModelPool.value.filter(isEditableApiModel))
const visibleLocalPool = computed(() =>
  modelEntries.filter(
    item => item.capability !== 'llm' && item.capability !== 'tts' && !item.hidden,
  ),
)
const hiddenPool = computed(() => modelEntries.filter(item => item.hidden))
const modelPoolSummary = computed(
  () => `${managedApiModelPool.value.length} 个可编辑 API · ${availableLlmModels.value.length} LLM · ${availableTtsModels.value.length} TTS`,
)
const roleLabels: Record<AssignmentSlot, { title: string; hint: string; kind: string }> = {
  voice: { title: '语音交互主模型', hint: '正式回答与 speak 队列', kind: 'LLM 01' },
  shadow: { title: 'Shadow Agent 模型', hint: '后台理解、工具与任务推进', kind: 'LLM 02' },
  status: { title: '状态汇报模型', hint: 'Shadow 的可读实时工作简报', kind: 'LLM 03' },
  tts: { title: '语音合成模型', hint: '将回复转换为可播放语音', kind: 'TTS' },
}
const llmAssignmentSlots: LlmAssignmentSlot[] = ['voice', 'shadow', 'status']
const modelStatusLabels: Record<DemoModelStatus, string> = {
  verified: '已验证',
  inherited: '运行时导入',
  healthy: '健康',
  needs_check: '待检查',
}

function saveDemo(name: string) {
  saveNotice.value = `${name}已保存，将在下一次对话时生效。`
}

function scanModels() {
  modelScanState.value = 'scanning'
  window.setTimeout(() => {
    modelScanState.value = 'complete'
    saveNotice.value = '已完成模拟扫描：现有配置和受管模型目录已同步。'
  }, 520)
}

function discoverProvider() {
  providerStep.value = 'discovering'
  window.setTimeout(() => {
    providerStep.value = providerForm.protocol === '手动模型 ID' ? 'custom-id' : 'verified'
  }, 620)
}

function verifyCustomModel() {
  if (!providerForm.customModelId.trim()) {
    saveNotice.value = '请输入自定义模型 ID 后再进行最小能力测试。'
    return
  }
  providerStep.value = 'discovering'
  window.setTimeout(() => {
    providerStep.value = 'verified'
  }, 620)
}

function addVerifiedProvider() {
  const isTts = providerForm.protocol === 'MiniMax TTS'
  const modelId = providerForm.customModelId.trim() || (
    isTts ? 'speech-2.8-hd' : providerForm.protocol === 'DeepSeek' ? 'deepseek-chat' : 'gpt-4o-mini'
  )
  const entry: DemoModelEntry = {
    id: `managed-${Date.now()}`,
    name: modelId,
    modelId,
    capability: isTts ? 'tts' : 'llm',
    origin: 'product_managed',
    provider: providerForm.name.trim() || '新的 API 服务',
    status: 'verified',
    ...(isTts
      ? {}
      : { maxContextTokens: 131072 as DemoContextTokens, allowedReasoning: ['off'] as DemoReasoningEffort[] }),
  }
  modelEntries.push(entry)
  providerForm.apiKey = ''
  providerForm.customModelId = ''
  providerStep.value = 'idle'
  expandedModelSections.pool = true
  saveNotice.value = `${entry.provider} · ${entry.name} 已验证并加入 ${isTts ? 'TTS' : 'LLM'} 模型池。`
}

function toggleModelSection(section: ModelSection) {
  expandedModelSections[section] = !expandedModelSections[section]
}

function modelById(id: string) {
  return modelEntries.find(item => item.id === id)
}

function selectedModel(slot: AssignmentSlot) {
  return modelById(modelAssignments[slot])
}

function providerClass(item: DemoModelEntry | undefined) {
  return (
    item?.provider
      ?.toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/-+$/, '') || 'managed'
  )
}

function modelTitle(item: DemoModelEntry | undefined) {
  return item ? `${item.provider} · ${item.name}` : '未选择模型'
}

function setThinkingFromSlider(event: Event, collapse = false) {
  const slider = event.target as HTMLInputElement
  const option = thinkingOptions.value[Number(slider.value)]
  if (!option) return
  shadowThinking.value = option.value
  if (collapse) thinkingExpanded.value = false
}

function setThinkingFromTrackClick(event: MouseEvent) {
  const slider = event.currentTarget as HTMLInputElement
  const bounds = slider.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width))
  const optionIndex = Math.round(ratio * (thinkingOptions.value.length - 1))
  const option = thinkingOptions.value[optionIndex]
  if (!option) return
  shadowThinking.value = option.value
  thinkingExpanded.value = false
}

function formatContextTokens(value: number | undefined) {
  return modelContextOptions.find(option => option.value === value)?.label ?? '未设置'
}

function reasoningLabel(value: DemoReasoningEffort) {
  return reasoningEffortOptions.find(option => option.value === value)?.shortLabel ?? value
}

function reasoningSummary(item: DemoModelEntry | undefined) {
  if (!item || item.capability !== 'llm') return '不适用'
  const values = item.allowedReasoning ?? ['off']
  const selected = reasoningEffortOptions
    .filter(option => values.includes(option.value))
    .map(option => option.shortLabel)
  if (selected.length === reasoningEffortOptions.length) return 'Off–Max'
  return selected.length ? selected.join(' / ') : 'Off'
}

function modelPolicySummary(item: DemoModelEntry | undefined) {
  if (!item) return '尚未分配'
  if (item.capability === 'tts') return '语音合成 · 无思考参数'
  return `${formatContextTokens(item.maxContextTokens)} · ${reasoningSummary(item)}`
}

function modelStatusLabel(status: DemoModelStatus) {
  return modelStatusLabels[status] ?? status
}

function isEditableApiModel(item: DemoModelEntry) {
  return item.origin === 'product_managed' && (item.capability === 'llm' || item.capability === 'tts')
}

function isAssignedModel(item: DemoModelEntry) {
  return Object.values(modelAssignments).includes(item.id)
}

function startEditingModel(item: DemoModelEntry) {
  if (!isEditableApiModel(item)) return
  editingModelId.value = item.id
  deleteCandidateId.value = null
  modelEditDraft.name = item.name
  modelEditDraft.modelId = item.modelId
  modelEditDraft.maxContextTokens = item.maxContextTokens ?? 131072
  modelEditDraft.allowedReasoning = [...(item.allowedReasoning ?? ['off'])]
}

function cancelEditingModel() {
  editingModelId.value = null
}

function toggleReasoningLevel(level: DemoReasoningEffort) {
  if (level === 'off') {
    modelEditDraft.allowedReasoning = ['off']
    return
  }
  const withoutOff = modelEditDraft.allowedReasoning.filter(value => value !== 'off')
  const next = withoutOff.includes(level)
    ? withoutOff.filter(value => value !== level)
    : [...withoutOff, level]
  modelEditDraft.allowedReasoning = next.length ? next : ['off']
}

function saveModelEdit(item: DemoModelEntry) {
  const name = modelEditDraft.name.trim()
  const modelId = modelEditDraft.modelId.trim()
  if (!name || !modelId) {
    saveNotice.value = '显示名称和模型 ID 不能为空。'
    return
  }
  item.name = name
  item.modelId = modelId
  if (item.capability === 'llm') {
    item.maxContextTokens = modelEditDraft.maxContextTokens
    item.allowedReasoning = [...modelEditDraft.allowedReasoning]
  }
  editingModelId.value = null
  saveNotice.value = `${item.provider} · ${item.name} 的模型策略已保存。`
}

function requestDeleteModel(item: DemoModelEntry) {
  if (!isEditableApiModel(item)) return
  if (isAssignedModel(item)) {
    saveNotice.value = '该 API 模型正在被系统使用；请先在“模型配置”中改用其他模型。'
    return
  }
  editingModelId.value = null
  deleteCandidateId.value = deleteCandidateId.value === item.id ? null : item.id
}

function deleteApiModel(item: DemoModelEntry) {
  if (deleteCandidateId.value !== item.id || isAssignedModel(item)) return
  const index = modelEntries.findIndex(entry => entry.id === item.id)
  if (index >= 0) modelEntries.splice(index, 1)
  deleteCandidateId.value = null
  saveNotice.value = `${modelTitle(item)} 已从 API 模型池删除。`
}

function hideModel(item: DemoModelEntry) {
  if (isAssignedModel(item)) {
    saveNotice.value = '该模型正在被分配使用；请先改用其他条目或解除分配。'
    return
  }
  item.hidden = true
  saveNotice.value = `${modelTitle(item)} 已隐藏；本地文件不会删除。`
}

function restoreModel(item: DemoModelEntry) {
  item.hidden = false
  saveNotice.value = `${modelTitle(item)} 已恢复。`
}

function confirmApply() {
  showApplyConfirm.value = false
  restartState.value = 'restarting'
  runtimeState.value = 'offline'
  window.setTimeout(() => {
    restartState.value = 'complete'
    runtimeState.value = 'standby'
    saveNotice.value = '模拟模型服务已重启，新的系统配置已生效。'
  }, 820)
}
</script>

<template>
  <main class="prototype-shell" :class="`runtime-${runtimeState}`">
    <header class="prototype-topbar">
      <button class="brand" type="button" aria-label="回到首页" @click="activePage = 'home'">
        <span class="brand-symbol"><i></i><i></i><i></i></span>
        <span><strong>Voice Terminal</strong><small>全天候桌面语音助手</small></span>
      </button>
      <nav class="page-nav">
        <button type="button" :class="{ active: activePage === 'home' }" @click="activePage = 'home'">工作台</button>
        <button type="button" :class="{ active: activePage === 'settings' }" @click="activePage = 'settings'">设置</button>
      </nav>
      <div class="topbar-status">
        <span class="prototype-tag"><i></i> UX PROTOTYPE P0.1</span>
        <span class="mock-tag">模拟数据</span>
      </div>
    </header>

    <section v-if="activePage === 'home'" class="workbench workbench-reordered" aria-label="Voice Terminal 工作台">
      <aside class="shadow-column panel">
        <header class="shadow-header">
          <div><p class="panel-kicker"><span class="shadow-spark">✦</span> Shadow Agent</p><h2>后台协作</h2></div>
          <button class="expand-button" type="button" :aria-expanded="shadowExpanded" @click="shadowExpanded = !shadowExpanded">{{ shadowExpanded ? '收起' : '展开' }} <span>{{ shadowExpanded ? '−' : '+' }}</span></button>
        </header>
        <section class="shadow-state-card"><div><span class="shadow-live-dot"></span>随时可协作</div><strong>Context ready</strong></section>
        <section class="shadow-task"><p class="section-label">当前关注</p><h3>整理下午安排并保留专注时间</h3><p>根据本轮对话识别到的时间偏好，等待主助手给出可确认的安排建议。</p></section>
        <section class="shadow-summary"><div class="section-row"><span>实时工作简报</span><small>状态汇报模型</small></div><p>已识别“下午安排”和“三点后不排会议”两个约束；建议在回复中确认现有日历冲突。</p></section>
        <section class="shadow-timeline"><div class="section-row"><span>运行轨迹</span><small>刚刚</small></div><ol><li class="done"><i></i><div><strong>读取会话上下文</strong><span>已确认用户偏好</span></div></li><li class="active"><i></i><div><strong>重新判断当前意图</strong><span>等待下次模型响应</span></div></li><li><i></i><div><strong>发布可读观察</strong><span>尚未需要</span></div></li></ol></section>
        <footer class="panel-resource-footer shadow-resource-footer">
          <section class="resource-metrics shadow-resource-metrics" aria-label="Shadow 模型资源与思考强度">
            <div class="resource-meter context-meter" tabindex="0" role="img" aria-label="上下文占用 86K / 256K，使用率 34%" data-tooltip="上下文占用 · 86K / 256K · 34%">
              <span>上下文占用</span>
              <strong>86K <small>/ 256K</small></strong>
              <i><b style="width: 34%"></b></i>
              <em>34%</em>
            </div>
            <div class="thinking-meter">
              <div class="thinking-control">
                <button class="thinking-trigger" type="button" :aria-expanded="thinkingExpanded" :aria-label="`思考强度：${currentThinkingOption.label}`" @click="thinkingExpanded = !thinkingExpanded">
                  <strong>{{ currentThinkingOption.shortLabel.toLowerCase() }}</strong>
                  <i aria-hidden="true">⌄</i>
                </button>
                <div v-if="thinkingExpanded" class="thinking-popover" role="dialog" aria-label="思考强度设置">
                  <div class="thinking-popover-header">
                    <div>
                      <strong>{{ currentThinkingOption.label }}</strong>
                      <span>{{ currentThinkingModel?.name ?? '当前模型' }}</span>
                    </div>
                    <button type="button" aria-label="收起思考强度设置" @click="thinkingExpanded = false">×</button>
                  </div>
                  <input class="thinking-slider" type="range" :min="0" :max="Math.max(thinkingOptions.length - 1, 0)" step="1" :value="currentThinkingIndex" :style="thinkingSliderStyle" :aria-valuetext="currentThinkingOption.label" aria-label="选择思考强度" @input="setThinkingFromSlider($event)" @change="setThinkingFromSlider($event, true)" @click="setThinkingFromTrackClick">
                  <div class="thinking-scale" aria-hidden="true">
                    <span v-for="(option, index) in thinkingOptions" :key="option.value" :class="{ active: index === currentThinkingIndex }">{{ option.shortLabel }}</span>
                  </div>
                </div>
              </div>
            </div>
            <div class="resource-meter cache-rate-number shadow-cache-rate-number" tabindex="0" role="img" aria-label="缓存命中率 95%" data-tooltip="缓存命中率 · 95%">
              <strong>95%</strong>
            </div>
          </section>
        </footer>
      </aside>

      <section class="system-column system-focus-column panel">
        <p class="panel-kicker"><span class="eyebrow-dot"></span>语音运行时</p>
        <div class="system-heading"><div><h2 class="system-state">{{ currentRuntime.label }}</h2><p class="system-subtitle">{{ currentRuntime.timestamp }}</p></div><span class="state-chip" :class="runtimeState">{{ runtimeState === 'offline' ? 'OFF' : 'LIVE' }}</span></div>
        <div class="orb-stage" :class="`orb-${currentRuntime.pulse}`"><span class="orb-halo halo-one"></span><span class="orb-halo halo-two"></span><span class="orb-halo halo-three"></span><span class="voice-orb"><i></i><i></i><i></i><i></i><i></i></span></div>
        <p class="orb-caption">{{ currentRuntime.description }}</p>
        <button class="runtime-button" type="button" @click="runtimeState = runtimeState === 'offline' ? 'standby' : 'offline'">{{ runtimeState === 'offline' ? '启动语音助手' : '关闭语音助手' }}</button>
        <section class="metric-card"><div class="metric-label"><span>本轮响应</span><button type="button">i</button></div><strong>{{ currentRuntime.latency }}</strong><p>用户说完 → AI 开口</p><div class="metric-spark"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div></section>
        <section class="mode-picker"><div class="section-row"><span>运行模式</span><small>下一轮生效</small></div><div class="mode-options"><button v-for="mode in [{ value: 'fast', label: '快速' }, { value: 'balanced', label: '均衡' }, { value: 'deep', label: '深度' }]" :key="mode.value" type="button" :class="{ selected: reasoningMode === mode.value }" @click="reasoningMode = mode.value as ReasoningMode">{{ mode.label }}</button></div></section>
        <section class="state-preview"><p>状态预览 <span>仅 Demo</span></p><div><button v-for="choice in stateChoices" :key="choice.value" type="button" :class="{ selected: runtimeState === choice.value }" @click="runtimeState = choice.value">{{ choice.label }}</button></div></section>
      </section>

      <section class="conversation-column panel">
        <header class="conversation-header"><div><p class="panel-kicker">实时会话</p><h1>今天想一起处理什么？</h1></div><div class="session-security"><span class="lock-icon">⌁</span><span>本机处理</span></div></header>
        <div class="conversation-stream" :class="{ empty: !hasConversation }">
          <div v-if="!hasConversation" class="empty-conversation"><span class="empty-mark">✦</span><strong>{{ runtimeState === 'error' ? '语音服务暂时不可用' : runtimeState === 'offline' ? '启动后即可开始对话' : '说出唤醒词，或等待助手准备好' }}</strong><p>你的语音、转写和助手回复会在这里自然地形成对话。</p></div>
          <template v-else>
            <div class="message user-message"><div class="message-meta"><span class="avatar user-avatar">你</span><span>你</span><time>09:42</time></div><p>帮我把下午的安排理一理，留出一段专注写方案的时间。</p><span class="message-status">已确认</span></div>
            <div v-if="isListening" class="live-asr"><span class="live-dot"></span><span>实时识别</span><p>还有，三点之后我不想排会议…</p><span class="typing-bars"><i></i><i></i><i></i></span></div>
            <div v-if="reasoningMode === 'deep' && (runtimeState === 'thinking' || runtimeState === 'speaking')" class="reasoning-card"><div><span class="reasoning-icon">✦</span><strong>正在推演安排</strong><small>深度模式 · 可读摘要</small></div><p>正在整理已有日程，优先保留连续的深度工作时间。</p></div>
            <div class="message assistant-message" :class="{ speaking: assistantIsSpeaking }"><div class="message-meta"><span class="avatar assistant-avatar"><i></i><i></i><i></i></span><span>Voice Terminal</span><time>{{ assistantIsSpeaking ? '正在播报' : '09:42' }}</time></div><p>可以。建议你把 13:30 到 15:00 留作方案专注时间，15:00 后先处理待确认事项，再安排沟通。</p><div class="speech-queue"><span class="queue-dot" :class="{ playing: assistantIsSpeaking }"></span>{{ assistantIsSpeaking ? '正在播放此段回复 · 随时可以打断' : '已加入 speak 队列 · 等待实际播报' }}</div></div>
          </template>
        </div>
        <footer class="panel-resource-footer conversation-footer"><div><span class="footer-mic"><i></i>{{ runtimeState === 'listening' ? '正在聆听你的声音' : '语音由本机服务处理' }}</span><span>无输入框 · 通过语音继续</span></div><section class="resource-metrics conversation-resource-metrics" aria-label="模型资源概览"><div class="resource-meter context-meter" tabindex="0" role="img" aria-label="上下文占用 48K / 128K，使用率 38%" data-tooltip="上下文占用 · 48K / 128K · 38%"><span>上下文占用</span><strong>48K <small>/ 128K</small></strong><i><b style="width: 38%"></b></i><em>38%</em></div><div class="resource-meter cache-rate-number" tabindex="0" role="img" aria-label="缓存命中率 75%，命中量 36K / 48K" data-tooltip="缓存命中率 · 75% · 36K / 48K"><strong>75%</strong></div></section></footer>
      </section>
    </section>

    <section v-else class="settings-page">
      <header class="settings-heading"><div><p class="panel-kicker">应用设置</p><h1>让助手更像你的工作方式</h1><p>所有内容均为 P0.1 模拟数据，不会修改本机配置、人格文件或模型服务。</p></div><span class="settings-badge">下一次对话生效</span></header>
      <div class="settings-layout">
        <nav class="settings-tabs">
          <button type="button" :class="{ active: activeSettingsTab === 'wake' }" @click="activeSettingsTab = 'wake'"><span>◌</span><div><strong>KWS 唤醒词</strong><small>启动与待机</small></div></button>
          <button type="button" :class="{ active: activeSettingsTab === 'persona' }" @click="activeSettingsTab = 'persona'"><span>✦</span><div><strong>人格与指令</strong><small>主 LLM 与 Shadow</small></div></button>
          <button type="button" :class="{ active: activeSettingsTab === 'silence' }" @click="activeSettingsTab = 'silence'"><span>◷</span><div><strong>静默等待</strong><small>最终检查与回待机</small></div></button>
          <button type="button" :class="{ active: activeSettingsTab === 'models' }" @click="activeSettingsTab = 'models'"><span>◇</span><div><strong>模型管理</strong><small>连接、配置与模型池</small></div></button>
        </nav>

        <article class="settings-panel panel">
          <template v-if="activeSettingsTab === 'wake'">
            <header><p class="panel-kicker">KWS 唤醒词</p><h2>让助手在需要时醒来</h2><p>设置全天候待机和唤醒词。正式产品将通过受控设置接口校验设备与模型状态。</p></header>
            <div class="settings-stack"><label class="switch-row"><div><strong>启用唤醒词</strong><span>在后台持续检测本机音频</span></div><input v-model="wakeSettings.enabled" type="checkbox"><i></i></label><label class="switch-row"><div><strong>全天候待机</strong><span>播报和最终静默检查结束后自动回到待机</span></div><input v-model="wakeSettings.alwaysOn" type="checkbox"><i></i></label><label class="form-field"><span>唤醒词</span><input v-model="wakeSettings.keyword" type="text"><small>建议使用自然、容易区分的短语。</small></label><label class="form-field"><span>识别灵敏度 <b>{{ wakeSettings.threshold }}%</b></span><input v-model="wakeSettings.threshold" type="range" min="20" max="95"><div class="range-label"><small>更少误触发</small><small>更容易唤醒</small></div></label></div>
            <footer><button class="save-button" type="button" @click="saveDemo('KWS 设置')">保存 KWS 设置</button></footer>
          </template>

          <template v-else-if="activeSettingsTab === 'persona'">
            <header><p class="panel-kicker">人格与指令</p><h2>定义助手如何与你协作</h2><p>Markdown 文件会在下一次新会话建立时重新加载；当前正在进行的会话不受影响。</p></header>
            <div class="persona-layout"><nav class="persona-nav"><button v-for="document in primaryPersonaDocuments" :key="document.id" type="button" :class="{ active: activePersona === document.id }" @click="activePersona = document.id"><strong>{{ document.label }}</strong><small>{{ document.id === 'shadow' ? 'Shadow Agent' : '主 LLM' }}</small></button></nav><label class="document-editor"><span>{{ currentDocument.path }}</span><textarea v-model="personaDocuments[currentDocument.id]" spellcheck="false"></textarea><small>Demo 保存只存在于当前页面内存中。</small></label></div>
            <footer><button class="save-button" type="button" @click="saveDemo(`${currentDocument.label}文档`)">保存文档</button></footer>
          </template>

          <template v-else-if="activeSettingsTab === 'silence'">
            <header><p class="panel-kicker">静默等待事件</p><h2>留出恰当的等待时间</h2><p>这是唤醒或会话后没有新的有效用户事件时的最终检查时间，不会改变正常语句结束速度。</p></header>
            <div class="silence-control"><span>最终静默等待</span><div><input v-model.number="silenceSeconds" min="1" max="120" type="number"><b>秒</b></div><input v-model="silenceSeconds" min="5" max="90" step="1" type="range"><div class="silence-scale"><small>快速回待机</small><small>默认 30 秒</small><small>长时间等待</small></div></div>
            <div class="info-callout"><span>i</span><p>正常一句话结束仍由 Smart Turn 处理，当前约为 1.4 秒；这里仅管理长期无有效事件后的最终静默检查与回待机。</p></div>
            <footer><button class="save-button" type="button" @click="saveDemo('静默等待时间')">保存等待时间</button></footer>
          </template>

          <template v-else>
            <header class="model-header">
              <div><p class="panel-kicker">模型管理</p><h2>把模型连接、分配与策略分开管理</h2><p>连接负责把模型带进来，配置负责分配运行时角色，模型池负责维护每个 API 条目的上下文和思考能力。</p></div>
              <button class="outline-button" type="button" :disabled="modelScanState === 'scanning'" @click="scanModels">{{ modelScanState === 'scanning' ? '正在扫描…' : '扫描现有配置与受管目录' }}</button>
            </header>
            <div class="model-safety"><span>✓</span><p>实际产品中，API Key 仅在验证请求生命周期内使用；成功后由 Voice Server 写入 Windows Credential Manager，页面不保存密钥。</p></div>

            <div class="model-accordion-list">
              <section class="model-accordion-card" :class="{ open: expandedModelSections.add }">
                <button class="model-accordion-toggle" type="button" :aria-expanded="expandedModelSections.add" aria-controls="model-section-add" @click="toggleModelSection('add')">
                  <span class="accordion-index">01</span><span class="accordion-copy"><strong>添加模型</strong><small>连接供应商并完成一次最小能力验证</small></span><span class="accordion-status">连接向导</span><span class="accordion-chevron" :class="{ open: expandedModelSections.add }">⌄</span>
                </button>
                <transition name="model-accordion"><div v-if="expandedModelSections.add" id="model-section-add" class="model-card-content">
                  <div class="card-section-heading"><div><p class="section-label">ADD MODEL</p><h3>添加 API 供应商连接</h3></div><span>自动发现 + 自定义兜底</span></div>
                  <div class="provider-fields"><label><span>连接名称</span><input v-model="providerForm.name" type="text"></label><label><span>服务地址</span><input v-model="providerForm.baseUrl" type="url"></label><label><span>协议</span><select v-model="providerForm.protocol"><option>OpenAI 兼容</option><option>DeepSeek</option><option>MiniMax TTS</option><option>手动模型 ID</option></select></label><label><span>API Key</span><input v-model="providerForm.apiKey" autocomplete="off" placeholder="仅用于本次模拟验证" type="password"></label></div>
                  <div v-if="providerStep === 'custom-id'" class="custom-model-row"><label><span>服务不支持模型列表：自定义模型 ID</span><input v-model="providerForm.customModelId" placeholder="例如 my-company-chat-01" type="text"></label><button class="outline-button" type="button" @click="verifyCustomModel">最小能力测试</button></div>
                  <div v-else class="provider-actions"><button class="outline-button" type="button" :disabled="providerStep === 'discovering'" @click="discoverProvider">{{ providerStep === 'discovering' ? '正在发现并验证…' : '自动发现并验证连接' }}</button><p v-if="providerStep === 'verified'" class="verify-ok">已通过最小真实能力测试（Demo 模拟）。</p></div>
                  <button v-if="providerStep === 'verified'" class="save-button add-model-button" type="button" @click="addVerifiedProvider">验证成功，加入模型池</button>
                </div></transition>
              </section>

              <section class="model-accordion-card" :class="{ open: expandedModelSections.config }">
                <button class="model-accordion-toggle" type="button" :aria-expanded="expandedModelSections.config" aria-controls="model-section-config" @click="toggleModelSection('config')">
                  <span class="accordion-index">02</span><span class="accordion-copy"><strong>模型配置</strong><small>把 3 个 LLM 与 1 个 TTS 分配到运行时角色</small></span><span class="accordion-status">4 个运行槽位</span><span class="accordion-chevron" :class="{ open: expandedModelSections.config }">⌄</span>
                </button>
                <transition name="model-accordion"><div v-if="expandedModelSections.config" id="model-section-config" class="model-card-content">
                  <div class="config-callout"><span class="callout-icon">i</span><p>这里仅选择“谁负责什么”。上下文上限和思考能力属于模型本身的策略，请在模型池中编辑；同一个模型被多个角色使用时会保持一致。</p></div>
                  <div class="assignment-grid assignment-grid-four">
                    <article v-for="role in llmAssignmentSlots" :key="role" class="assignment-slot">
                      <div class="slot-heading"><span class="slot-kind">{{ roleLabels[role].kind }}</span><span class="slot-check">✓ 已分配</span></div>
                      <div class="slot-copy"><h3>{{ roleLabels[role].title }}</h3><p>{{ roleLabels[role].hint }}</p></div>
                      <label class="model-select-field"><span>选择模型</span><select v-model="modelAssignments[role]"><option v-for="item in availableLlmModels" :key="item.id" :value="item.id">{{ item.provider }} · {{ item.name }}（{{ item.modelId }}）</option></select></label>
                      <div class="slot-policy"><span>{{ modelPolicySummary(selectedModel(role)) }}</span><small>{{ modelStatusLabel(selectedModel(role)?.status ?? 'needs_check') }}</small></div>
                    </article>
                    <article class="assignment-slot assignment-slot-tts">
                      <div class="slot-heading"><span class="slot-kind tts-kind">{{ roleLabels.tts.kind }}</span><span class="slot-check">✓ 已分配</span></div>
                      <div class="slot-copy"><h3>{{ roleLabels.tts.title }}</h3><p>{{ roleLabels.tts.hint }}</p></div>
                      <label class="model-select-field"><span>选择模型</span><select v-model="modelAssignments.tts"><option v-for="item in availableTtsModels" :key="item.id" :value="item.id">{{ item.provider }} · {{ item.name }}（{{ item.modelId }}）</option></select></label>
                      <div class="slot-policy"><span>{{ modelPolicySummary(selectedModel('tts')) }}</span><small>{{ modelStatusLabel(selectedModel('tts')?.status ?? 'needs_check') }}</small></div>
                    </article>
                  </div>
                </div></transition>
              </section>

              <section class="model-accordion-card" :class="{ open: expandedModelSections.pool }">
                <button class="model-accordion-toggle" type="button" :aria-expanded="expandedModelSections.pool" aria-controls="model-section-pool" @click="toggleModelSection('pool')">
                  <span class="accordion-index">03</span><span class="accordion-copy"><strong>模型池</strong><small>在这里编辑已添加的 API 模型，维护上下文与思考能力</small></span><span class="accordion-status">{{ modelPoolSummary }}</span><span class="accordion-chevron" :class="{ open: expandedModelSections.pool }">⌄</span>
                </button>
                <transition name="model-accordion"><div v-if="expandedModelSections.pool" id="model-section-pool" class="model-card-content">
                  <div class="pool-intro"><div><p class="section-label">MODEL POOL</p><h3>已添加的 API 模型</h3><p>产品托管的条目可编辑、删除；从现有运行时配置导入的条目保持只读。</p></div><span class="pool-total"><strong>{{ apiModelPool.length }}</strong><small>个 API 条目</small></span></div>
                  <section class="pool-subsection">
                    <div class="pool-subsection-heading"><div><span class="pool-section-mark llm-mark">LLM</span><div><h4>文字模型</h4><p>上下文和思考能力会随条目保存</p></div></div><span>{{ apiModelPool.filter(item => item.capability === 'llm').length }} 个</span></div>
                    <div v-if="apiModelPool.filter(item => item.capability === 'llm').length" class="pool-entry-list">
                      <article v-for="item in apiModelPool.filter(entry => entry.capability === 'llm')" :key="item.id" class="pool-entry" :class="{ editing: editingModelId === item.id, 'delete-pending': deleteCandidateId === item.id }">
                        <div class="pool-entry-main"><span class="provider-badge" :class="providerClass(item)">{{ item.provider.slice(0, 1) }}</span><div class="pool-entry-copy"><div class="pool-entry-title"><strong>{{ item.provider }} · {{ item.name }}</strong><span class="capability-tag">LLM</span><span class="entry-status" :class="item.status">{{ modelStatusLabel(item.status) }}</span></div><span class="pool-entry-id">{{ item.modelId }} · {{ originLabels[item.origin] }}</span></div></div>
                        <div class="pool-entry-policy"><span><small>最大上下文</small><strong>{{ formatContextTokens(item.maxContextTokens) }}</strong></span><span><small>允许思考</small><strong>{{ reasoningSummary(item) }}</strong></span></div>
                        <div v-if="isEditableApiModel(item)" class="pool-entry-actions"><button type="button" @click="startEditingModel(item)">{{ editingModelId === item.id ? '收起' : '编辑' }}</button><button class="danger-action" type="button" @click="requestDeleteModel(item)">删除</button></div><span v-else class="readonly-tag">运行时导入</span>
                        <div v-if="editingModelId === item.id" class="model-edit-panel"><div class="edit-panel-heading"><div><strong>编辑模型策略</strong><small>修改只作用于这个 API 条目</small></div><span>已验证</span></div><div class="edit-fields"><label><span>显示名称</span><input v-model="modelEditDraft.name" type="text"></label><label><span>模型 ID</span><input v-model="modelEditDraft.modelId" type="text"></label><label><span>最大上下文</span><select v-model="modelEditDraft.maxContextTokens"><option v-for="option in modelContextOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label><fieldset class="reasoning-fieldset"><legend>允许的思考强度 <small>可多选</small></legend><div class="reasoning-chip-list"><button v-for="option in reasoningEffortOptions" :key="option.value" type="button" :class="{ selected: modelEditDraft.allowedReasoning.includes(option.value) }" @click="toggleReasoningLevel(option.value)"><strong>{{ option.shortLabel }}</strong><small>{{ option.label }}</small></button></div></fieldset></div><div class="edit-panel-footer"><span>Off 表示关闭思考；选择其他等级会自动移除 Off。</span><div><button class="ghost-action" type="button" @click="cancelEditingModel">取消</button><button class="save-button compact-save" type="button" @click="saveModelEdit(item)">保存修改</button></div></div></div>
                        <div v-if="deleteCandidateId === item.id" class="inline-delete-confirm"><span>确定从模型池删除这个 API 条目？</span><div><button class="ghost-action" type="button" @click="deleteCandidateId = null">取消</button><button class="confirm-delete-action" type="button" @click="deleteApiModel(item)">确认删除</button></div></div>
                      </article>
                    </div>
                    <div v-else class="pool-empty">还没有添加 LLM。先在“添加模型”中完成连接验证。</div>
                  </section>
                  <section class="pool-subsection">
                    <div class="pool-subsection-heading"><div><span class="pool-section-mark tts-mark">TTS</span><div><h4>语音模型</h4><p>语音模型不需要上下文或思考参数</p></div></div><span>{{ apiModelPool.filter(item => item.capability === 'tts').length }} 个</span></div>
                    <div v-if="apiModelPool.filter(item => item.capability === 'tts').length" class="pool-entry-list">
                      <article v-for="item in apiModelPool.filter(entry => entry.capability === 'tts')" :key="item.id" class="pool-entry" :class="{ editing: editingModelId === item.id, 'delete-pending': deleteCandidateId === item.id }">
                        <div class="pool-entry-main"><span class="provider-badge" :class="providerClass(item)">{{ item.provider.slice(0, 1) }}</span><div class="pool-entry-copy"><div class="pool-entry-title"><strong>{{ item.provider }} · {{ item.name }}</strong><span class="capability-tag tts-capability">TTS</span><span class="entry-status" :class="item.status">{{ modelStatusLabel(item.status) }}</span></div><span class="pool-entry-id">{{ item.modelId }} · {{ originLabels[item.origin] }}</span></div></div>
                        <div class="pool-entry-policy tts-policy"><span><small>模型用途</small><strong>语音合成</strong></span><span><small>策略</small><strong>由 TTS 配置控制</strong></span></div>
                        <div v-if="isEditableApiModel(item)" class="pool-entry-actions"><button type="button" @click="startEditingModel(item)">{{ editingModelId === item.id ? '收起' : '编辑' }}</button><button class="danger-action" type="button" @click="requestDeleteModel(item)">删除</button></div><span v-else class="readonly-tag">运行时导入</span>
                        <div v-if="editingModelId === item.id" class="model-edit-panel"><div class="edit-panel-heading"><div><strong>编辑 TTS 条目</strong><small>修改名称或模型 ID，不涉及 LLM 策略</small></div><span>已验证</span></div><div class="edit-fields tts-edit-fields"><label><span>显示名称</span><input v-model="modelEditDraft.name" type="text"></label><label><span>模型 ID</span><input v-model="modelEditDraft.modelId" type="text"></label><div class="tts-edit-note"><span>♪</span><p>上下文和思考强度只适用于 LLM；TTS 的音色、格式等设置会在独立的 TTS 配置中管理。</p></div></div><div class="edit-panel-footer"><span>此条目当前作为语音合成模型使用。</span><div><button class="ghost-action" type="button" @click="cancelEditingModel">取消</button><button class="save-button compact-save" type="button" @click="saveModelEdit(item)">保存修改</button></div></div></div>
                        <div v-if="deleteCandidateId === item.id" class="inline-delete-confirm"><span>确定从模型池删除这个 API 条目？</span><div><button class="ghost-action" type="button" @click="deleteCandidateId = null">取消</button><button class="confirm-delete-action" type="button" @click="deleteApiModel(item)">确认删除</button></div></div>
                      </article>
                    </div>
                    <div v-else class="pool-empty">还没有添加 TTS。可以在“添加模型”中选择 MiniMax TTS 协议。</div>
                  </section>
                  <details class="local-models-disclosure"><summary><span><strong>本地模型清单</strong><small>受管目录扫描结果，不参与 API 策略配置</small></span><em>{{ visibleLocalPool.length }} 个可用 · {{ hiddenPool.length }} 个已隐藏</em></summary><div class="local-model-list"><article v-for="item in visibleLocalPool" :key="item.id" class="local-model-row"><span class="provider-badge managed">本</span><div><strong>{{ item.name }}</strong><small>{{ capabilityLabels[item.capability] }} · {{ item.path }}</small></div><button type="button" @click="hideModel(item)">隐藏</button></article><article v-for="item in hiddenPool" :key="`hidden-${item.id}`" class="local-model-row hidden-row"><span class="provider-badge managed">本</span><div><strong>{{ item.name }}</strong><small>仅从模型池隐藏；不删除本地文件</small></div><button type="button" @click="restoreModel(item)">恢复</button></article></div></details>
                </div></transition>
              </section>
            </div>

            <footer class="model-settings-footer"><div class="restart-copy"><span v-if="restartState === 'restarting'">正在模拟重启受影响服务…</span><span v-else-if="restartState === 'complete'">✓ 模拟配置已生效</span><span v-else>应用配置会中止聆听/播报并重启相关服务。</span></div><button class="save-button" type="button" @click="showApplyConfirm = true">应用模型配置</button></footer>
          </template>
        </article>
      </div>
    </section>

    <div v-if="saveNotice" class="save-notice" role="status"><span>✓</span>{{ saveNotice }}<button type="button" aria-label="关闭提示" @click="saveNotice = ''">×</button></div>
    <div v-if="showApplyConfirm" class="modal-backdrop"><section class="apply-dialog" role="dialog" aria-modal="true"><p class="panel-kicker">立即应用</p><h2>重启受影响的模型服务？</h2><p>这会停止当前聆听、终止正在播报的内容，并重建语音主模型、Shadow Agent、状态汇报模型和 TTS。</p><div><button class="outline-button" type="button" @click="showApplyConfirm = false">暂不应用</button><button class="save-button" type="button" @click="confirmApply">确认并立即重启</button></div></section></div>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue'
import TtsVoiceLibrary from '../../interface/TtsVoiceLibrary.vue'
import TtsSynthesisPreferences from '../../interface/TtsSynthesisPreferences.vue'
import OnlineTtsVoiceSettings from '../../interface/OnlineTtsVoiceSettings.vue'
import { productApi } from '../api'
import { desktopPort } from '../desktop-port'
import { useProductContext } from '../product-context'
import { isRecord } from '../../record'
import type { ProductLocalTtsModelId, ProductLocalTtsStatus, ProductOnlineTtsModelId, ProductTtsSettings } from '../types'
import { useLocalTtsStatus } from '../useLocalTtsStatus'

const props = withDefaults(defineProps<{ demo?: boolean }>(), { demo: false })
const context = props.demo ? undefined : useProductContext()
const desktop = desktopPort()
const settings = context?.settings ?? reactive({ tts: { backend: 'minimax', local_model: 'minimax-female-shaonv-zh' } as ProductTtsSettings })
type Choice = 'minimax' | ProductLocalTtsModelId // Choice（播放目标）。
const officialVoiceLibraryUrl = 'https://solutions.minimaxi.com/voices'
const selected = ref<Choice>(settings.tts.backend === 'minimax' ? 'minimax' : settings.tts.local_model || 'minimax-female-shaonv-zh')
const saving = ref(false)
const savingOnline = ref(false)
const previewingOnline = ref(false)
const savingControls = ref(false) // savingControls（全局语音调节开关保存状态）。
const savingQuality = ref(false) // savingQuality（本地合成质量保存状态）。
const onlineModelDraft = ref<ProductOnlineTtsModelId>(settings.tts.online_model ?? 'speech-2.8-hd')
const onlineVoiceIdDraft = ref(settings.tts.online_voice_id ?? 'female-shaonv')
let onlinePreviewOperationId: string | undefined
let selectionGeneration = 0 // selectionGeneration（选择请求代次）：连续操作保留最后目标。
const error = ref('')
const demoStatus = reactive<ProductLocalTtsStatus>({ backend: 'minimax', local_model: 'minimax-female-shaonv-zh', loaded_model: null, load_state: 'disabled', error: null,
  models: [{ id: 'minimax-female-shaonv-zh', name: '中文少女声（通用基底）', available: true, loaded: false, native_sample_rate: 32000 }, { id: 'tangyuge-v2pp-d78cf598', name: '唐语歌（中文微调）', available: true, loaded: false, native_sample_rate: 32000 }] })
const live = useLocalTtsStatus(() => !props.demo)
const status = computed(() => props.demo ? demoStatus : live.status.value)
const readyModels = computed(() => status.value?.models.filter(item => item.available === true) ?? []) // readyModels（可启动的本地模型）。
const selectedReady = computed(() => selected.value === 'minimax' || readyModels.value.some(item => item.id === selected.value))
const model = computed(() => selected.value)
const options = reactive({ gpu_max_performance: settings.tts.gpu_max_performance ?? false, aniflive_chunk_optimization: settings.tts.aniflive_chunk_optimization ?? true, aniflive_first_audio_wait_ms: settings.tts.aniflive_first_audio_wait_ms ?? 0 })
const effective = computed(() => status.value?.backend === 'aniflive' ? status.value.local_model : 'minimax')
const unavailable = computed(() => saving.value || !!status.value?.pending_selection)
const targetSaveDisabled = computed(() => saving.value || !!status.value?.pending_selection || !selectedReady.value ||
  (selected.value === effective.value && (selected.value === 'minimax' || status.value?.load_state === 'ready')))
const names = computed<Record<string, string>>(() => ({ minimax: '在线 API（网络语音接口）', ...Object.fromEntries((status.value?.models ?? []).map(item => [item.id, item.name])) }))
const states: Record<string, string> = { disabled: '在线模式', unloaded: '尚未加载', loading: '正在加载', ready: '已就绪', error: '加载失败' }
watch(() => [settings.tts.online_model, settings.tts.online_voice_id] as const, ([modelId, voiceId]) => {
  if (modelId === 'speech-2.8-turbo' || modelId === 'speech-2.8-hd') onlineModelDraft.value = modelId
  if (typeof voiceId === 'string') onlineVoiceIdDraft.value = voiceId
}, { immediate: true })
let previousEffective: Choice = effective.value
watch(() => status.value, value => {
  if (!value) return
  const nextEffective: Choice = value.backend === 'minimax' ? 'minimax' : value.local_model
  const draftFollowedPreviousEffective = selected.value === previousEffective
  previousEffective = nextEffective
  if (!value.pending_selection) {
    settings.tts.backend = value.backend; settings.tts.local_model = value.local_model
    if (!saving.value && draftFollowedPreviousEffective) selected.value = nextEffective
  }
})
onBeforeUnmount(() => {
  if (onlinePreviewOperationId) void productApi.cancelTtsOperation(onlinePreviewOperationId).catch(() => {})
})
onMounted(() => { if (!props.demo) void live.refresh() })
function refreshStatus() { if (!props.demo) void live.refresh() }
async function setSpeechControl(enabled: boolean) {
  const previous = settings.tts.speech_control_enabled ?? true
  savingControls.value = true; error.value = ''
  settings.tts.speech_control_enabled = enabled
  try {
    if (!props.demo) {
      const result = await productApi.saveTts({ speech_control_enabled: enabled })
      if (isRecord(result.settings) && isRecord(result.settings.tts)) Object.assign(settings.tts, result.settings.tts)
    }
    settings.tts.speech_control_enabled = enabled
  } catch (failure) {
    settings.tts.speech_control_enabled = previous
    error.value = failure instanceof Error ? failure.message : '保存语音调节失败。'
  } finally { savingControls.value = false }
}
function selectTarget(event: Event) {
  const choice = (event.target as HTMLSelectElement).value as Choice
  if (choice !== 'minimax' && !readyModels.value.some(item => item.id === choice)) return
  selected.value = choice
  error.value = ''
}
async function applyTarget(choice: Choice) {
  if (choice !== 'minimax' && !readyModels.value.some(item => item.id === choice)) {
    throw new Error('所选本地语音模型尚未具备运行条件。')
  }
  const generation = ++selectionGeneration; saving.value = true; error.value = ''
  try {
    if (props.demo) {
      demoStatus.backend = choice === 'minimax' ? 'minimax' : 'aniflive'
      if (choice !== 'minimax') demoStatus.local_model = choice
      demoStatus.loaded_model = choice === 'minimax' ? null : choice
      demoStatus.load_state = choice === 'minimax' ? 'disabled' : 'ready'
      return
    }
    const result = await productApi.saveTts(choice === 'minimax' ? { backend: 'minimax' } : { backend: 'aniflive', local_model: choice })
    if (generation !== selectionGeneration) return
    if (isRecord(result.settings) && isRecord(result.settings.tts)) Object.assign(settings.tts, result.settings.tts)
    await live.refresh(true) // 切换结果不能被先前仍在途的状态读取覆盖。
  } catch (failure) {
    if (generation !== selectionGeneration) return
    error.value = failure instanceof Error ? failure.message : '语音目标切换失败。'
    if (!props.demo) await live.refresh(true)
    throw failure
  } finally { if (generation === selectionGeneration) saving.value = false }
}
async function saveTarget() {
  try { await applyTarget(selected.value) } catch { /* error（界面错误）：已在 applyTarget 中展示。 */ }
}
async function ensureTargetSupport(_engine: ProductLocalTtsModelId) {
  const target = selected.value
  if (props.demo) return
  const current = status.value
  if (current?.backend === 'aniflive' && current.local_model === target && current.load_state === 'ready') return
  await applyTarget(target)
}
async function saveOption(key: keyof typeof options, value: boolean | number) {
  if (savingQuality.value) return
  const previous = { ...options }
  savingQuality.value = true; error.value = ''
  Object.assign(options, { [key]: value })
  try {
    if (!props.demo) {
      const result = await productApi.saveTts({ [key]: value })
      if (isRecord(result.settings) && isRecord(result.settings.tts)) Object.assign(settings.tts, result.settings.tts)
    }
    Object.assign(settings.tts, options)
  } catch (failure) {
    Object.assign(options, previous)
    error.value = failure instanceof Error ? failure.message : '保存本地合成选项失败。'
  } finally { savingQuality.value = false }
}
async function saveOnlineSettings() {
  const voiceId = onlineVoiceIdDraft.value.trim()
  error.value = ''
  if (!voiceId) { error.value = '请输入音色 ID（音色标识）。'; return }
  savingOnline.value = true
  try {
    if (props.demo) {
      settings.tts.online_model = onlineModelDraft.value
      settings.tts.online_voice_id = voiceId
      return
    }
    const result = await productApi.saveTts({ online_model: onlineModelDraft.value, online_voice_id: voiceId })
    if (isRecord(result.settings) && isRecord(result.settings.tts)) Object.assign(settings.tts, result.settings.tts)
    await live.refresh(true)
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '保存 MiniMax 在线模型与音色失败。'
  } finally { savingOnline.value = false }
}
async function previewOnlineVoice() {
  const voiceId = onlineVoiceIdDraft.value.trim()
  error.value = ''
  if (!voiceId) { error.value = '请输入音色 ID（音色标识）。'; return }
  if (props.demo) { error.value = '演示界面未连接在线语音接口，无法试听。'; return }
  previewingOnline.value = true
  try {
    let operation = await productApi.startMinimaxTtsPreview(onlineModelDraft.value, voiceId)
    onlinePreviewOperationId = operation.operation_id
    while (operation.state === 'running') {
      await new Promise(resolve => window.setTimeout(resolve, 300))
      operation = await productApi.getTtsOperation(operation.operation_id)
    }
    if (operation.state !== 'succeeded') throw new Error(operation.error || 'MiniMax 在线音色试听失败。')
    context?.setNotice('MiniMax 在线音色试听完成。')
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : 'MiniMax 在线音色试听失败。'
  } finally {
    onlinePreviewOperationId = undefined
    previewingOnline.value = false
  }
}
async function openOfficialVoiceLibrary() {
  error.value = ''
  try {
    if (desktop.available) await desktop.openMinimaxVoiceLibrary()
    else window.open(officialVoiceLibraryUrl, '_blank', 'noopener,noreferrer')
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '无法打开 MiniMax 官方音色库。'
  }
}
</script>

<template>
  <SettingsPageHeader section="tts" />
  <div class="tts-settings">
    <section class="tts-target preference-card">
      <div class="tts-target-row">
        <label class="form-field"><span>语音播放目标</span><select :value="selectedReady ? selected : ''" :disabled="saving || !!status?.pending_selection" @change="selectTarget"><option value="minimax">{{ names.minimax }}</option><option v-if="!selectedReady" value="" disabled>当前目标未就绪，请在组件设置补齐资源</option><option v-for="item in readyModels" :key="item.id" :value="item.id">{{ names[item.id] }}</option></select></label>
        <button class="save-button tts-target-save" type="button" aria-label="保存播放目标" :disabled="targetSaveDisabled" @click="saveTarget">{{ saving ? '正在切换…' : '保存播放目标' }}</button>
      </div>
      <p class="instruction-hint" role="status">当前生效：{{ names[effective] }} · {{ saving ? '正在切换…' : states[status?.load_state || 'unloaded'] || '等待状态' }}<template v-if="status?.pending_selection"> · 等待当前事务结束后切换</template></p>
      <p v-if="selected !== effective" class="instruction-hint">当前仅切换设置展示；保存播放目标后才会切换实际语音后端。</p>
      <p v-if="selected === 'minimax'" class="instruction-hint">在线接口密钥与账号用量可在“组件设置 → 语音合成”中管理。</p>
      <p v-if="error || status?.error || live.error.value" class="instruction-error" role="alert">{{ error || status?.error || live.error.value }}</p>
    </section>
    <OnlineTtsVoiceSettings v-if="selected === 'minimax'" v-model:model="onlineModelDraft" v-model:voice-id="onlineVoiceIdDraft" :saving="savingOnline" :previewing="previewingOnline" :disabled="saving" :demo="demo" @save="saveOnlineSettings" @preview="previewOnlineVoice" @open-library="openOfficialVoiceLibrary" />
    <TtsVoiceLibrary v-if="selected !== 'minimax' && selectedReady" :port="productApi" model="aniflive" :unavailable="unavailable" :ensure-support="ensureTargetSupport" :demo="demo" @changed="refreshStatus" />
    <section v-if="selected !== 'minimax'" class="aniflive-options" aria-label="AnifLive 音色选项">
      <label class="switch-row"><div><strong>显卡高性能</strong><span>开启后暂停并取消事件预热。下一回答生效。</span></div><input type="checkbox" role="switch" aria-label="显卡高性能" :checked="options.gpu_max_performance" :disabled="savingQuality" @change="saveOption('gpu_max_performance', ($event.target as HTMLInputElement).checked)"><i aria-hidden="true"></i></label>
      <label class="switch-row"><div><strong>后续分块优化</strong><span>开启使用 48 个语义标记，关闭使用 96 个。下一回答生效。</span></div><input type="checkbox" role="switch" aria-label="后续分块优化" :checked="options.aniflive_chunk_optimization" :disabled="savingQuality" @change="saveOption('aniflive_chunk_optimization', ($event.target as HTMLInputElement).checked)"><i aria-hidden="true"></i></label>
      <label class="form-field"><span>首音等待 · {{ options.aniflive_first_audio_wait_ms }} 毫秒</span><input type="range" min="0" max="200" step="1" aria-label="首音等待" :value="options.aniflive_first_audio_wait_ms" :disabled="savingQuality" @change="saveOption('aniflive_first_audio_wait_ms', Number(($event.target as HTMLInputElement).value))"><small>从网络首块实际到达开始等待，期间继续接收音频。下一回答生效。</small></label>
    </section>
    <TtsSynthesisPreferences
      :speech-control-enabled="settings.tts.speech_control_enabled ?? true"
      :speech-disabled="savingControls || saving || savingOnline || savingQuality"
      @speech-control="setSpeechControl"
    />
    <template v-if="selected !== 'minimax' && selectedReady">
      <p class="instruction-hint">试音会自动暂停监听并等待当前播报结束，完成后恢复原先的监听状态。参考录音保留原始采样率；试听完成后恢复正式参考。</p>
    </template>
  </div>
</template>

<style scoped>
.aniflive-options { display: grid; gap: 12px; }
.tts-settings { display: grid; gap: 22px; }
.tts-target { display: grid; gap: 12px; padding: 20px; border: 1px solid var(--line-strong); border-radius: 18px; background: var(--panel); }
.tts-target-row { display: grid; grid-template-columns: minmax(0,520px) auto; align-items: end; gap: 12px; }
.tts-target-save { min-height: 42px; white-space: nowrap; }
@media(max-width:600px) { .tts-target-row { grid-template-columns: minmax(0,1fr); } .tts-target-save { justify-self: end; } }
</style>

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
const settings = context?.settings ?? reactive({ tts: { backend: 'minimax', local_model: 'voxcpm2', inference_steps_by_model: { voxcpm1: 10, voxcpm2: 20 } } as ProductTtsSettings })
type Choice = 'minimax' | ProductLocalTtsModelId // Choice（播放目标）。
const officialVoiceLibraryUrl = 'https://solutions.minimaxi.com/voices'
const selected = ref<Choice>(settings.tts.backend === 'minimax' ? 'minimax' : settings.tts.local_model || 'voxcpm2')
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
const demoStatus = reactive<ProductLocalTtsStatus>({ backend: 'minimax', local_model: 'voxcpm2', loaded_model: null, load_state: 'disabled', error: null,
  models: [{ id: 'voxcpm1', name: 'VoxCPM1', available: true, loaded: false, native_sample_rate: 16000 }, { id: 'voxcpm2', name: 'VoxCPM2', available: true, loaded: false, native_sample_rate: 48000 }] })
const live = useLocalTtsStatus(() => !props.demo)
const status = computed(() => props.demo ? demoStatus : live.status.value)
const readyModels = computed(() => status.value?.models.filter(item => item.available === true) ?? []) // readyModels（可启动的本地模型）。
const selectedReady = computed(() => selected.value === 'minimax' || readyModels.value.some(item => item.id === selected.value))
const model = computed(() => selected.value === 'voxcpm1' ? 'voxcpm1' : 'voxcpm2')
const steps = reactive({ voxcpm1: 10, voxcpm2: 20, ...settings.tts.inference_steps_by_model })
const effective = computed(() => status.value?.backend === 'voxcpm1' ? status.value.local_model : 'minimax')
const unavailable = computed(() => saving.value || !!status.value?.pending_selection || (selected.value !== 'minimax' && (effective.value !== selected.value || status.value?.load_state !== 'ready')))
const names: Record<Choice, string> = { minimax: '在线 API（网络语音接口）', voxcpm1: 'VoxCPM1（第一代本地模型）', voxcpm2: 'VoxCPM2（第二代本地模型）' }
const states: Record<string, string> = { disabled: '在线模式', unloaded: '尚未加载', loading: '正在加载', ready: '已就绪', error: '加载失败' }
watch(() => [settings.tts.online_model, settings.tts.online_voice_id] as const, ([modelId, voiceId]) => {
  if (modelId === 'speech-2.8-turbo' || modelId === 'speech-2.8-hd') onlineModelDraft.value = modelId
  if (typeof voiceId === 'string') onlineVoiceIdDraft.value = voiceId
}, { immediate: true })
watch(() => status.value, value => {
  if (value && !value.pending_selection && !saving.value) {
    settings.tts.backend = value.backend; settings.tts.local_model = value.local_model
    if (value.load_state !== 'loading') selected.value = value.backend === 'minimax' ? 'minimax' : value.local_model
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
  void choose()
}
async function choose() {
  const choice = selected.value; const generation = ++selectionGeneration; saving.value = true; error.value = ''
  try {
    if (props.demo) {
      demoStatus.backend = choice === 'minimax' ? 'minimax' : 'voxcpm1'
      if (choice !== 'minimax') demoStatus.local_model = choice
      demoStatus.loaded_model = choice === 'minimax' ? null : choice
      demoStatus.load_state = choice === 'minimax' ? 'disabled' : 'ready'
      return
    }
    const result = await productApi.saveTts(choice === 'minimax' ? { backend: 'minimax' } : { backend: 'voxcpm1', local_model: choice })
    if (generation !== selectionGeneration) return
    if (isRecord(result.settings) && isRecord(result.settings.tts)) Object.assign(settings.tts, result.settings.tts)
    await live.refresh(true) // 切换结果不能被先前仍在途的状态读取覆盖。
  } catch (failure) {
    if (generation !== selectionGeneration) return
    error.value = failure instanceof Error ? failure.message : '语音目标切换失败。'
    await live.refresh(true)
    if (generation !== selectionGeneration) return
    selected.value = effective.value
  } finally { if (generation === selectionGeneration) saving.value = false }
}
async function setQuality(value: number) {
  if (savingQuality.value || steps[model.value] === value) return
  const targetModel = model.value // targetModel（本次保存的模型）：切换播放目标不改变在途请求。
  const previous = steps[targetModel] // previous（保存前步数）：失败时恢复原档位。
  const nextSteps = { ...steps, [targetModel]: value } // nextSteps（逐模型新步数）：保留另一模型的独立设置。
  savingQuality.value = true
  error.value = ''
  steps[targetModel] = value
  try {
    if (!props.demo) await productApi.saveTts({ inference_steps_by_model: nextSteps })
    settings.tts.inference_steps_by_model = nextSteps
  } catch (failure) {
    steps[targetModel] = previous
    error.value = failure instanceof Error ? failure.message : '保存合成质量失败。'
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
    <section class="tts-target preference-card"><label class="form-field"><span>语音播放目标</span><select :value="selectedReady ? selected : ''" @change="selectTarget"><option value="minimax">{{ names.minimax }}</option><option v-if="!selectedReady" value="" disabled>当前目标未就绪，请在组件设置补齐资源</option><option v-for="item in readyModels" :key="item.id" :value="item.id">{{ names[item.id] }}</option></select></label>
      <p class="instruction-hint" role="status">当前生效：{{ names[effective] }} · {{ saving ? '正在切换…' : states[status?.load_state || 'unloaded'] || '等待状态' }}<template v-if="status?.pending_selection"> · 等待当前事务结束后切换</template></p>
      <p v-if="selected === 'minimax'" class="instruction-hint">在线接口密钥与账号用量可在“组件设置 → 语音合成”中管理。</p>
      <p v-if="error || status?.error || live.error.value" class="instruction-error" role="alert">{{ error || status?.error || live.error.value }}</p>
    </section>
    <OnlineTtsVoiceSettings v-if="selected === 'minimax'" v-model:model="onlineModelDraft" v-model:voice-id="onlineVoiceIdDraft" :saving="savingOnline" :previewing="previewingOnline" :disabled="saving" :demo="demo" @save="saveOnlineSettings" @preview="previewOnlineVoice" @open-library="openOfficialVoiceLibrary" />
    <TtsVoiceLibrary v-if="selected !== 'minimax' && selectedReady" :port="productApi" :model="model" :steps="steps[model]" :unavailable="unavailable" :demo="demo" @changed="refreshStatus" />
    <TtsSynthesisPreferences
      :speech-control-enabled="settings.tts.speech_control_enabled ?? true"
      :speech-disabled="savingControls || saving || savingOnline || savingQuality"
      :quality-steps="selected !== 'minimax' && selectedReady ? steps[model] : undefined"
      :quality-disabled="savingQuality || saving || savingControls"
      :saving-quality="savingQuality"
      @speech-control="setSpeechControl"
      @quality="setQuality"
    />
    <template v-if="selected !== 'minimax' && selectedReady">
      <p class="instruction-hint">试音会自动暂停监听并等待当前播报结束，完成后恢复原先的监听状态。设计音色保存生成的样音，正式回答使用这段样音克隆。</p>
    </template>
  </div>
</template>

<style scoped>
.tts-settings { display: grid; gap: 22px; }
.tts-target { display: grid; gap: 12px; padding: 20px; border: 1px solid var(--line-strong); border-radius: 18px; background: var(--panel); }
.tts-target .form-field { max-width: 520px; }
</style>

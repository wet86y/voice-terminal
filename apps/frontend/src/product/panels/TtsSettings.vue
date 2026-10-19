<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue'
import TtsVoiceLibrary from '../../interface/TtsVoiceLibrary.vue'
import { productApi } from '../api'
import { useProductContext } from '../product-context'
import { isRecord } from '../../record'
import type { ProductLocalTtsModelId, ProductLocalTtsStatus, ProductTtsSettings } from '../types'
import { useLocalTtsStatus } from '../useLocalTtsStatus'

const props = withDefaults(defineProps<{ demo?: boolean }>(), { demo: false })
const context = props.demo ? undefined : useProductContext()
const settings = context?.settings ?? reactive({ tts: { backend: 'minimax', local_model: 'voxcpm2', inference_steps_by_model: { voxcpm1: 10, voxcpm2: 20 } } as ProductTtsSettings })
type Choice = 'minimax' | ProductLocalTtsModelId // Choice（播放目标）。
const selected = ref<Choice>(settings.tts.backend === 'minimax' ? 'minimax' : settings.tts.local_model || 'voxcpm2')
const saving = ref(false)
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
watch(() => status.value, value => {
  if (value && !value.pending_selection && !saving.value) {
    settings.tts.backend = value.backend; settings.tts.local_model = value.local_model
    if (value.load_state !== 'loading') selected.value = value.backend === 'minimax' ? 'minimax' : value.local_model
  }
})
onMounted(() => { if (!props.demo) void live.refresh() })
function refreshStatus() { if (!props.demo) void live.refresh() }
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
    context?.setNotice(result.message || '语音目标已提交。')
    await live.refresh(true) // 切换结果不能被先前仍在途的状态读取覆盖。
  } catch (failure) {
    if (generation !== selectionGeneration) return
    error.value = failure instanceof Error ? failure.message : '语音目标切换失败。'
    await live.refresh(true)
    if (generation !== selectionGeneration) return
    selected.value = effective.value
  } finally { if (generation === selectionGeneration) saving.value = false }
}
async function saveSteps() {
  error.value = ''
  if (!Number.isInteger(steps[model.value]) || steps[model.value] < 1 || steps[model.value] > 30) { error.value = '推理步数必须为 1–30 的整数。'; return }
  try {
    if (!props.demo) await productApi.saveTts({ inference_steps_by_model: { ...steps } })
    settings.tts.inference_steps_by_model = { ...steps }
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '保存推理步数失败。' }
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
    <template v-if="selected !== 'minimax' && selectedReady">
      <TtsVoiceLibrary :port="productApi" :model="model" :steps="steps[model]" :unavailable="unavailable" :demo="demo" @changed="refreshStatus" />
      <details class="tts-quality"><summary>合成质量</summary><label class="form-field"><span>{{ names[model] }} · 推理步数</span><input v-model.number="steps[model]" type="number" min="1" max="30" step="1" :disabled="saving" @change="saveSteps"><small>默认一代 10 步、二代 20 步。保存后用于下一次回答。</small></label></details>
      <p class="instruction-hint">试音前请先结束监听和当前播报。设计音色保存生成的样音，正式回答使用这段样音克隆。</p>
    </template>
  </div>
</template>

<style scoped>
.tts-settings { display: grid; gap: 22px; }.tts-target { display: grid; gap: 12px; padding: 20px; border: 1px solid var(--line-strong); border-radius: 18px; background: var(--panel); }.tts-target .form-field { max-width: 520px; }.tts-quality { padding: 16px 20px; border-radius: 14px; border: 1px solid var(--line); color: var(--muted); }.tts-quality summary { cursor: pointer; }.tts-quality .form-field { margin-top: 16px; max-width: 320px; }
</style>

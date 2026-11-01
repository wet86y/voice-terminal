<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { ProductOnlineTtsModelId } from './tts-voice-types'
import { productApi } from '../product/api'

const model = defineModel<ProductOnlineTtsModelId>('model', { required: true }) // model（在线模型草稿）。
const voiceId = defineModel<string>('voiceId', { required: true }) // voiceId（在线音色标识草稿）。
const props = withDefaults(defineProps<{
  saving?: boolean // saving（正在验证与保存）。
  previewing?: boolean // previewing（正在试听）。
  disabled?: boolean // disabled（目标切换期间暂不可操作）。
  demo?: boolean // demo（仅演示界面，不提交真实配置）。
}>(), { demo: false })
const emit = defineEmits<{
  save: [] // save（保存在线模型与音色）。
  preview: [] // preview（试听当前在线音色）。
  openLibrary: [] // openLibrary（打开官方音色库）。
}>()
type VoiceLookupState = 'idle' | 'checking' | 'found' | 'error' | 'demo' // VoiceLookupState（在线音色名称查询状态）。
const voiceName = ref('') // voiceName（MiniMax 在线返回的音色名称）。
const voiceLookupState = ref<VoiceLookupState>('idle') // voiceLookupState（在线查询状态）。
let voiceLookupTimer: ReturnType<typeof setTimeout> | undefined
let voiceLookupGeneration = 0 // voiceLookupGeneration（查询代次）：丢弃过期回包。
const voiceNameDisplay = computed(() => {
  if (voiceLookupState.value === 'found') return voiceName.value
  if (voiceLookupState.value === 'checking') return '查询中…'
  if (voiceLookupState.value === 'demo') return '演示模式'
  if (voiceLookupState.value === 'idle') return '输入音色 ID'
  return '音色错误'
})
watch(voiceId, value => {
  if (voiceLookupTimer !== undefined) clearTimeout(voiceLookupTimer)
  const generation = ++voiceLookupGeneration
  const id = value.trim()
  voiceName.value = ''
  if (props.demo) { voiceLookupState.value = 'demo'; return }
  if (!id) { voiceLookupState.value = 'idle'; return }
  voiceLookupState.value = 'checking'
  voiceLookupTimer = setTimeout(async () => {
    try {
      const result = await productApi.minimaxVoiceName(id)
      if (generation !== voiceLookupGeneration) return
      if (result.voice_id === id && typeof result.voice_name === 'string' && result.voice_name.trim()) {
        voiceName.value = result.voice_name.trim()
        voiceLookupState.value = 'found'
      } else {
        voiceLookupState.value = 'error'
      }
    } catch {
      if (generation === voiceLookupGeneration) voiceLookupState.value = 'error'
    }
  }, 300)
}, { immediate: true })
onBeforeUnmount(() => {
  voiceLookupGeneration++
  if (voiceLookupTimer !== undefined) clearTimeout(voiceLookupTimer)
})
const onlineModels: Array<{ id: ProductOnlineTtsModelId; label: string; hint: string }> = [ // onlineModels（可选在线模型）。
  { id: 'speech-2.8-turbo', label: 'Turbo', hint: '低时延' },
  { id: 'speech-2.8-hd', label: 'HD', hint: '高音质' },
]
</script>

<template>
  <section class="online-tts-settings" aria-label="在线音色设置">
    <header class="online-library-heading"><h3>在线音色</h3><p>选择在线模型与音色，保存后用于后续播报。</p></header>
    <div class="online-tts-form">
      <fieldset class="online-model-field" :disabled="saving || previewing || disabled">
        <legend>在线模型</legend>
        <div class="online-model-choices" role="group" aria-label="在线模型">
          <button v-for="item in onlineModels" :key="item.id" class="online-model-choice" type="button" :aria-pressed="model === item.id" @click="model = item.id"><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></button>
        </div>
      </fieldset>
      <div class="online-voice-row">
        <label class="form-field"><span>音色 ID（音色标识）</span><input v-model="voiceId" type="text" autocomplete="off" spellcheck="false" placeholder="例如：female-shaonv" :disabled="saving || previewing || disabled" @keydown.enter.prevent="emit('save')"></label>
        <div class="online-voice-name" :class="`state-${voiceLookupState}`" role="status" aria-live="polite" aria-label="MiniMax 音色名称">
          <span>音色名称</span><strong>{{ voiceNameDisplay }}</strong>
        </div>
        <button class="outline-button online-voice-link" type="button" :disabled="saving || previewing || disabled" @click="emit('openLibrary')">官方音色库 ↗</button>
      </div>
      <div class="online-tts-actions">
        <p class="instruction-hint">{{ demo ? '演示模式：可调整模型与音色标识；名称窗口不连接在线查询，保存仅用于当前展示。' : '输入音色 ID（音色标识）后自动查询 MiniMax（在线语音服务）名称；保存时会再次验证，验证通过后用于后续播报。' }}</p>
        <div class="online-tts-buttons"><button class="outline-button" type="button" :disabled="previewing || saving || disabled || !voiceId.trim()" @click="emit('preview')">{{ previewing ? '正在试听…' : '试音' }}</button><button class="save-button" type="button" :disabled="previewing || saving || disabled || !voiceId.trim()" @click="emit('save')">{{ saving ? '正在验证并保存…' : '保存' }}</button></div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.online-tts-settings { display: grid; gap: 16px; min-width: 0; }
.online-library-heading h3 { font-size: var(--font-section); }
.online-library-heading p { margin-top: 6px; color: var(--muted); font-size: var(--font-body); }
.online-tts-form { display: grid; gap: 16px; padding: 20px; border: 1px solid var(--line-strong); border-radius: 18px; background: #ffffff03; }
.online-model-field { display: grid; gap: 8px; min-width: 0; padding: 0; border: 0; }
.online-model-field legend { margin-bottom: 8px; font-size: var(--font-caption); color: var(--text); }
.online-model-choices { display: flex; gap: 8px; }
.online-model-choice { display: flex; align-items: center; gap: 8px; min-height: 38px; padding: 7px 14px; border: 1px solid var(--line-strong); border-radius: 10px; background: var(--panel-soft); color: var(--text); cursor: pointer; }
.online-model-choice[aria-pressed="true"] { border-color: var(--accent); background: #b2baff18; color: var(--accent); }
.online-model-choice small { color: var(--muted); }
.online-voice-row { display: grid; grid-template-columns: minmax(180px,1.25fr) minmax(125px,.8fr) auto; align-items: end; gap: 12px; }
.online-voice-name { display: grid; align-content: center; min-width: 0; min-height: 42px; padding: 5px 10px; border: 1px solid var(--line-strong); border-radius: 10px; background: var(--panel-soft); }
.online-voice-name span { color: var(--muted); font-size: var(--font-caption); line-height: 1.2; }
.online-voice-name strong { overflow: hidden; color: var(--text); font-size: var(--font-body); text-overflow: ellipsis; white-space: nowrap; }
.online-voice-name.state-error strong { color: var(--danger); }
.online-voice-name.state-checking strong { color: var(--muted); }
.online-voice-link { min-height: 42px; white-space: nowrap; }
.online-tts-actions { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: center; gap: 12px; }
.online-tts-actions .instruction-hint { margin: 0; }
.online-tts-buttons { display: flex; gap: 8px; }
.online-tts-buttons .save-button { min-width: 92px; }
@media(max-width:600px) {
  .online-voice-row, .online-tts-actions { grid-template-columns: minmax(0,1fr); }
  .online-voice-name { min-height: 42px; }
  .online-voice-link { justify-self: start; }
  .online-tts-buttons { justify-content: flex-end; }
}
</style>

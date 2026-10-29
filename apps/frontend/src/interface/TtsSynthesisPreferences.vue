<script setup lang="ts">
defineProps<{
  speechControlEnabled: boolean // speechControlEnabled（语音调节开关）。
  speechDisabled?: boolean // speechDisabled（语音调节暂不可操作）。
  qualitySteps?: number // qualitySteps（当前本地推理步数）：在线语音不传入，隐藏质量选项。
  qualityDisabled?: boolean // qualityDisabled（质量档位暂不可操作）。
  savingQuality?: boolean // savingQuality（合成质量正在保存）。
}>()
const emit = defineEmits<{
  speechControl: [enabled: boolean] // speechControl（切换语音调节）。
  quality: [steps: number] // quality（切换本地合成步数）。
}>()
const qualityOptions = [ // qualityOptions（合成质量档位）：低、中、高分别使用固定推理步数。
  { label: '低', steps: 10 },
  { label: '中', steps: 15 },
  { label: '高', steps: 20 },
]
</script>

<template>
  <div class="tts-preferences">
    <label class="switch-row speech-control-row">
      <div><strong>语音调节</strong><span>按对话需要调节语气、语速和音量，适用于全部语音播放目标。</span></div>
      <input type="checkbox" role="switch" aria-label="语音调节" :checked="speechControlEnabled" :disabled="speechDisabled" @change="emit('speechControl', ($event.target as HTMLInputElement).checked)">
      <i aria-hidden="true"></i>
    </label>
    <section v-if="qualitySteps !== undefined" class="switch-row tts-quality" aria-label="合成质量" :aria-busy="savingQuality">
      <div><strong>合成质量</strong><span>低 10 步 · 中 15 步 · 高 20 步推理</span><span role="status">{{ savingQuality ? '正在保存…' : `当前 ${qualitySteps} 步 · 下次回答生效` }}</span></div>
      <div class="mode-options tts-quality-options" role="group" aria-label="合成质量档位">
        <button v-for="item in qualityOptions" :key="item.steps" type="button" :class="{ selected: qualitySteps === item.steps }" :aria-pressed="qualitySteps === item.steps" :title="`${item.label} · ${item.steps} 步推理`" :disabled="qualityDisabled" @click="emit('quality', item.steps)">{{ item.label }}</button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.tts-preferences { display: grid; gap: 12px; }
.tts-preferences .switch-row > div:first-child { min-width: 0; }
.speech-control-row { cursor: pointer; }
.speech-control-row input:disabled + i { opacity: .45; }
.tts-quality-options { flex: 0 0 216px; margin-top: 0; }
@media(max-width:600px) { .tts-quality-options { flex-basis: 156px; } }
</style>

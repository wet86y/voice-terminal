<script setup lang="ts">
defineProps<{
  speechControlEnabled: boolean // speechControlEnabled（语音调节开关）。
  speechDisabled?: boolean // speechDisabled（暂不可操作）。
}>()
const emit = defineEmits<{ speechControl: [enabled: boolean] }>()
</script>
<template>
  <label class="switch-row speech-control-row">
    <div><strong>语音调节</strong><span>按对话需要调节语音，实际能力由播放目标决定；AnifLive 支持原生语速。</span></div>
    <input type="checkbox" role="switch" aria-label="语音调节" :checked="speechControlEnabled" :disabled="speechDisabled" @change="emit('speechControl', ($event.target as HTMLInputElement).checked)">
    <i aria-hidden="true"></i>
  </label>
</template>
<style scoped>
.speech-control-row { cursor: pointer; }
.speech-control-row > div:first-child { min-width: 0; }
.speech-control-row input:disabled + i { opacity: .45; }
</style>

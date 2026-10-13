<script setup lang="ts">
import { computed } from 'vue'
import type { VoiceOrbModel } from './voice-orb'

const props = defineProps<{
  model: VoiceOrbModel // model（共享的语音动效数据）。
}>()

const waveformLevels = computed(() => Array.from({ length: 5 }, (_, index) => {
  const level = props.model.audioLevels[index]
  return props.model.sessionActive && ['listening', 'speaking'].includes(props.model.state)
    && typeof level === 'number' && Number.isFinite(level)
    ? Math.max(0, Math.min(1, level)) : 0
}))
const voiceEnergy = computed(() => waveformLevels.value.reduce((total, level) => total + level, 0) / 5)
</script>

<template>
  <div
    class="orb-stage"
    :class="[`orb-${model.state}`, { 'session-active': model.sessionActive }]"
    :style="{ '--voice-energy': voiceEnergy }"
    aria-hidden="true"
  >
    <span class="orb-ring"></span><span class="orb-ring second"></span>
    <!-- data-home-logo（主页图标定位标记）：启动动画读取中央主体的实际位置。 -->
    <div class="voice-orb" data-home-logo>
      <span class="voice-waveform">
        <i v-for="(level, index) in waveformLevels" :key="index" :style="{ '--voice-level': level }"></i>
      </span>
    </div>
  </div>
</template>

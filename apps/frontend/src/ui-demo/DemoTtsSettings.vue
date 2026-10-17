<script setup lang="ts">
import { ref, reactive } from 'vue'
import SettingsPageHeader from '../interface/SettingsPageHeader.vue'
import TtsVoiceLibrary from '../interface/TtsVoiceLibrary.vue'
import type { ProductLocalTtsModelId } from '../interface/tts-voice-types'
const selected = ref<'minimax' | ProductLocalTtsModelId>('voxcpm2') // selected（演示播放目标）。
const steps = reactive({ voxcpm1: 10, voxcpm2: 20 }) // steps（逐模型演示步数）。
</script>
<template>
  <SettingsPageHeader section="tts" />
  <div class="demo-tts">
    <section class="preference-card"><label class="form-field"><span>语音播放目标</span><select v-model="selected"><option value="minimax">在线 API（网络语音接口）</option><option value="voxcpm1">VoxCPM1（第一代本地模型）</option><option value="voxcpm2">VoxCPM2（第二代本地模型）</option></select></label><p class="instruction-hint">当前生效：{{ selected === 'minimax' ? '在线 API（网络语音接口）' : selected === 'voxcpm1' ? 'VoxCPM1（第一代本地模型）' : 'VoxCPM2（第二代本地模型）' }} · 演示已就绪</p></section>
    <template v-if="selected !== 'minimax'"><TtsVoiceLibrary :model="selected" :steps="steps[selected]" demo /><details><summary>合成质量</summary><label class="form-field"><span>推理步数</span><input v-model.number="steps[selected]" type="number" min="1" max="30"></label></details></template>
    <p v-else class="instruction-hint">使用组件设置中配置的 MiniMax（在线语音服务）。</p>
  </div>
</template>
<style scoped>.demo-tts { display: grid; gap: 22px; }.preference-card { padding: 20px; border: 1px solid var(--line-strong); border-radius: 18px; }.form-field { max-width: 520px; }details { padding: 16px 20px; border: 1px solid var(--line); border-radius: 14px; }</style>

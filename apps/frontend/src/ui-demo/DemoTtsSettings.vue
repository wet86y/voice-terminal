<script setup lang="ts">
import { ref, reactive } from 'vue'
import SettingsPageHeader from '../interface/SettingsPageHeader.vue'
import TtsVoiceLibrary from '../interface/TtsVoiceLibrary.vue'
import TtsSynthesisPreferences from '../interface/TtsSynthesisPreferences.vue'
import OnlineTtsVoiceSettings from '../interface/OnlineTtsVoiceSettings.vue'
import type { ProductLocalTtsModelId, ProductOnlineTtsModelId } from '../interface/tts-voice-types'
const selected = ref<'minimax' | ProductLocalTtsModelId>('voxcpm2') // selected（演示播放目标）。
const steps = reactive({ voxcpm1: 10, voxcpm2: 20 }) // steps（逐模型演示步数）。
const speechControlEnabled = ref(true) // speechControlEnabled（演示语音调节开关）。
const onlineModel = ref<ProductOnlineTtsModelId>('speech-2.8-hd') // onlineModel（演示在线模型）。
const onlineVoiceId = ref('female-shaonv') // onlineVoiceId（演示在线音色标识）。
const onlineNotice = ref('') // onlineNotice（演示保存与试听说明）。
function setQuality(value: number) { if (selected.value !== 'minimax') steps[selected.value] = value }
function saveOnlineSettings() {
  onlineVoiceId.value = onlineVoiceId.value.trim()
  onlineNotice.value = onlineVoiceId.value ? '演示设置已保存，仅用于当前展示。' : '请输入音色 ID（音色标识）。'
}
function previewOnlineVoice() { onlineNotice.value = '演示界面未连接在线语音接口，无法试听。' }
function openOfficialVoiceLibrary() { window.open('https://solutions.minimaxi.com/voices', '_blank', 'noopener,noreferrer') }
</script>
<template>
  <SettingsPageHeader section="tts" />
  <div class="demo-tts">
    <section class="preference-card"><label class="form-field"><span>语音播放目标</span><select v-model="selected"><option value="minimax">在线 API（网络语音接口）</option><option value="voxcpm1">VoxCPM1（第一代本地模型）</option><option value="voxcpm2">VoxCPM2（第二代本地模型）</option></select></label><p class="instruction-hint">当前生效：{{ selected === 'minimax' ? '在线 API（网络语音接口）' : selected === 'voxcpm1' ? 'VoxCPM1（第一代本地模型）' : 'VoxCPM2（第二代本地模型）' }} · 演示已就绪</p></section>
    <OnlineTtsVoiceSettings v-if="selected === 'minimax'" v-model:model="onlineModel" v-model:voice-id="onlineVoiceId" demo @save="saveOnlineSettings" @preview="previewOnlineVoice" @open-library="openOfficialVoiceLibrary" />
    <TtsVoiceLibrary v-else :model="selected" :steps="steps[selected]" demo />
    <p v-if="selected === 'minimax' && onlineNotice" class="instruction-hint" role="status">{{ onlineNotice }}</p>
    <TtsSynthesisPreferences :speech-control-enabled="speechControlEnabled" :quality-steps="selected !== 'minimax' ? steps[selected] : undefined" @speech-control="speechControlEnabled = $event" @quality="setQuality" />
  </div>
</template>
<style scoped>.demo-tts { display: grid; gap: 22px; }.preference-card { padding: 20px; border: 1px solid var(--line-strong); border-radius: 18px; }.form-field { max-width: 520px; }</style>

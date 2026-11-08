<script setup lang="ts">
import { ref } from 'vue'
import SettingsPageHeader from '../interface/SettingsPageHeader.vue'
import OnlineTtsVoiceSettings from '../interface/OnlineTtsVoiceSettings.vue'
import TtsVoiceLibrary from '../interface/TtsVoiceLibrary.vue'
import TtsSynthesisPreferences from '../interface/TtsSynthesisPreferences.vue'
const selected = ref<'online' | 'local'>('online') // 演示草稿。
const effective = ref<'online' | 'local'>('online') // 演示中已保存的目标。
const model = ref('minimax-female-shaonv-zh') // 演示本地模型。
const onlineModel = ref<'speech-2.8-turbo' | 'speech-2.8-hd'>('speech-2.8-hd')
const voiceId = ref('female-shaonv')
const speechEnabled = ref(true)
const message = ref('')
</script>
<template>
  <SettingsPageHeader section="tts" />
  <div class="tts-settings">
    <section class="preference-card demo-target-row">
      <span>语音播放目标</span>
      <div class="mode-options" role="group" aria-label="语音播放目标">
        <button type="button" :class="{ selected: selected === 'online' }" :aria-pressed="selected === 'online'" @click="selected = 'online'">在线</button>
        <button type="button" :class="{ selected: selected === 'local' }" :aria-pressed="selected === 'local'" @click="selected = 'local'">本地</button>
      </div>
      <button type="button" class="save-button" aria-label="保存播放目标" @click="effective = selected">保存播放目标</button>
      <p class="instruction-hint">当前生效：{{ effective === 'online' ? '在线语音' : '本地语音' }}</p>
    </section>
    <OnlineTtsVoiceSettings v-if="selected === 'online'" v-model:model="onlineModel" v-model:voice-id="voiceId" demo @save="voiceId = voiceId.trim(); message = '演示设置已保存，仅用于当前展示。'" @preview="message = '演示界面未连接在线语音接口，无法试听。'" />
    <template v-else>
      <section class="preference-card demo-model-row"><label class="form-field"><span>基础模型引擎</span><select v-model="model"><option value="minimax-female-shaonv-zh">中文少女声（通用基底）</option><option value="tangyuge-v2pp-d78cf598">唐语歌（中文微调）</option></select></label><button type="button" class="outline-button" disabled>导入</button><button type="button" class="save-button" @click="message = '演示模型选择已保存。'">保存</button></section>
      <TtsVoiceLibrary :model="model" demo />
    </template>
    <p v-if="message" class="instruction-hint">{{ message }}</p>
    <TtsSynthesisPreferences :speech-control-enabled="speechEnabled" @speech-control="speechEnabled = $event" />
  </div>
</template>
<style scoped>
.tts-settings { display: grid; gap: 22px; }
.demo-target-row { display: grid; gap: 12px; padding: 20px; }
.demo-model-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: end; gap: 12px; padding: 20px; }
</style>

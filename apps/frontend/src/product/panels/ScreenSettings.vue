<script setup lang="ts">
import { reactive, watch } from 'vue'
import { useProductContext } from '../product-context'
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue'
import type { ScreenCaptureSettings } from '../types'
const { settings, saveScreenCapture, saving, screenCaptureAvailable } = useProductContext()
const form = reactive<ScreenCaptureSettings>({ enabled: true, crop_width: 960, crop_height: 640, global_max_edge: 1920, app_overrides: [] })
watch(() => JSON.stringify(settings.screen_capture), () => {
  const value = settings.screen_capture
  if (value) Object.assign(form, { ...value, app_overrides: value.app_overrides.map(r => ({ ...r })) })
}, { immediate: true })
</script>
<template>
  <SettingsPageHeader section="screen" />
  <section class="preference-card settings-stack">
    <label class="switch-row"><div><strong>允许按需观察屏幕</strong><span>语音模型或 DSH（后台执行宿主）依据当前任务获取前台屏幕的全局图与局部图；截图随对应会话以编码保存。</span></div><input v-model="form.enabled" type="checkbox" role="switch" /><i></i></label>
    <p role="status">{{ screenCaptureAvailable ? '语音链路截图动作可用' : '语音链路暂不可用：检查启用开关、语音模型图像能力与捕获组件；DSH 可用性还取决于所选模型' }}</p>
    <label class="form-field"><span>局部宽度（物理像素）</span><input v-model.number="form.crop_width" type="number" min="256" max="2048" /></label>
    <label class="form-field"><span>局部高度（物理像素）</span><input v-model.number="form.crop_height" type="number" min="256" max="2048" /></label>
    <label class="form-field"><span>全局图最长边（像素）</span><input v-model.number="form.global_max_edge" type="number" min="1280" max="4096" /></label>
    <h3>应用中心规则</h3><p>匹配可执行文件名；全屏中心模式使用屏幕中心，窗口模式使用应用客户区中心。</p>
    <div v-for="(rule, index) in form.app_overrides" :key="index" class="screen-rule">
      <input v-model="rule.executable" :aria-label="`应用文件名 ${index + 1}`" placeholder="例如 game.exe" />
      <select v-model="rule.mode" :aria-label="`中心模式 ${index + 1}`"><option value="auto">自动判断</option><option value="cursor">鼠标中心</option><option value="center">中心模式</option></select>
      <button class="outline-button" @click="form.app_overrides.splice(index, 1)">移除</button>
    </div>
    <button class="outline-button" @click="form.app_overrides.push({ executable: '', mode: 'auto' })">添加应用规则</button>
    <button class="primary-button" :disabled="saving" @click="saveScreenCapture(form)">保存屏幕图像设置</button>
  </section>
</template>
<style scoped>.screen-rule{display:flex;gap:10px;flex-wrap:wrap}.screen-rule input{flex:1;min-width:150px}</style>

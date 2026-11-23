<script setup lang="ts">
import { ref } from 'vue'
import UiButton from '../../interface/UiButton.vue'
import type { FloatingDock } from '../../interface/floating/floating-window'
import { locateNativeInvoker } from '../../runtime/desktop-preflight'
import { useFloatingPreview, type PreviewVoice } from './preview-port'

const { snapshot, error, run } = useFloatingPreview('attach')
const voice = ref<PreviewVoice>('listening') // voice（模拟语音状态）。
const caption = ref('这是一段测试用的语音识别字幕，用于检查贴边宽度和位置。')
const examples = {
  text: '# 测试文字\n- 单击切换**静默**\n- 双击打开测试台',
  image: '![](fixture:icon)',
  mixed: '# 图文展示\n![语音终端](fixture:icon)\n鼠标停留可放大阅读。',
}
const content = ref(examples.mixed) // content（固定版式图文正文）。
const duration = ref<number | ''>(10) // duration（初始展示秒数）。

async function enter() {
  if (await run({ kind: 'mode', enabled: true })) await run({ kind: 'voice', state: voice.value, caption: caption.value })
}
function submit() {
  if (typeof duration.value === 'number') {
    void run({ kind: 'show', content: content.value, duration_seconds: duration.value })
  }
}
function chooseDock(event: Event) {
  const dock = (event.target as HTMLSelectElement).value as FloatingDock
  void run({ kind: 'place', dock })
}
</script>

<template>
  <main class="floating-preview-console">
    <header>
      <h1>悬浮窗测试台</h1>
      <div class="floating-preview-actions">
        <UiButton v-if="!snapshot?.active" :disabled="!snapshot" @click="enter">开启悬浮窗</UiButton>
        <UiButton v-else variant="quiet" @click="run({ kind: 'mode', enabled: false })">收起悬浮窗</UiButton>
        <UiButton variant="quiet" @click="locateNativeInvoker()?.invoke('exit_application')">退出测试台</UiButton>
      </div>
    </header>

    <section class="panel floating-preview-panel" aria-label="悬浮球与字幕">
      <h2>悬浮球与字幕</h2>
      <div class="floating-preview-row">
        <label class="form-field">吸附位置
          <select :value="snapshot?.dock ?? 'free'" @change="chooseDock">
            <option value="free">自由</option><option value="left">左边</option><option value="right">右边</option>
            <option value="top">上边</option><option value="bottom">下边</option>
          </select>
        </label>
        <label class="form-field">模拟语音状态
          <select v-model="voice">
            <option value="standby">静默</option><option value="listening">识别</option>
            <option value="thinking">思考</option><option value="speaking">播报</option>
          </select>
        </label>
      </div>
      <label class="form-field">测试字幕<input v-model="caption" type="text" maxlength="500"></label>
      <UiButton variant="quiet" :disabled="!snapshot" @click="run({ kind: 'voice', state: voice, caption })">应用字幕与状态</UiButton>
    </section>

    <section class="panel floating-preview-panel" aria-label="图文展示">
      <h2>图文展示</h2>
      <div class="floating-preview-actions" aria-label="图文样例">
        <UiButton variant="quiet" @click="content = examples.text">文字样例</UiButton>
        <UiButton variant="quiet" @click="content = examples.image">图片样例</UiButton>
        <UiButton variant="quiet" @click="content = examples.mixed">图文样例</UiButton>
      </div>
      <form class="floating-preview-panel" @submit.prevent="submit">
        <label class="form-field">图文正文<textarea v-model="content" maxlength="4096" rows="3" required></textarea></label>
        <div class="floating-preview-row">
          <label class="form-field">初始展示秒数<input v-model.number="duration" type="number" required min="5" max="60" step="1"></label>
          <div class="floating-preview-actions">
            <button type="submit" class="ui-button button-primary" :disabled="!snapshot?.active">提交展示</button>
            <UiButton variant="quiet" :disabled="!snapshot?.active" @click="run({ kind: 'clear' })">取消展示</UiButton>
          </div>
        </div>
      </form>
      <p role="status" class="floating-preview-notice">{{ error || snapshot?.notice || ' ' }}</p>
    </section>
  </main>
</template>

<style>
html, body, #app { min-width: 0; min-height: 0; }
body { overflow: auto; }
.floating-preview-console { display: grid; gap: 10px; padding: 16px; max-width: 760px; margin: auto; }
.floating-preview-console header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
.floating-preview-console h1, .floating-preview-console h2 { margin: 0; }
.floating-preview-console h1 { font-size: var(--font-heading); }
.floating-preview-console h2 { font-size: var(--font-section); }
.floating-preview-panel { display: grid; gap: 8px; align-items: start; justify-items: start; }
.floating-preview-panel.panel { padding: 14px; }
.floating-preview-panel form, .floating-preview-panel .form-field, .floating-preview-row { width: 100%; min-width: 0; }
.floating-preview-panel .form-field { font-size: var(--font-section); }
.floating-preview-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: end; }
.floating-preview-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.floating-preview-notice { margin: 0; min-height: 18px; color: var(--muted); font-size: var(--font-body); }
@media (max-width: 450px) { .floating-preview-row { grid-template-columns: 1fr; } }
</style>

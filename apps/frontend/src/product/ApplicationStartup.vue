<script setup lang="ts">
import { computed } from 'vue'
import { useApplicationHost, applicationStateLabel } from './application-host'
const application = useApplicationHost()
const { snapshot, overlay, error, now, retryingAll, businessReady, sessionInvalid } = application
const emit = defineEmits<{ system: []; models: [] }>()
const elapsed = computed(() => snapshot.value ? Math.max(0, Math.floor(now.value / 1000 - snapshot.value.started_at)) : 0)
const canRetry = computed(() => snapshot.value?.components.some(c => c.retry_allowed))
function inspect() { application.inspect(); emit('system') }
function configure() { application.inspect(); emit('models') }
</script>

<template>
  <div v-if="overlay" class="application-startup" role="dialog" aria-modal="true" aria-label="应用启动与运行反馈">
    <section class="application-startup-card">
      <p class="application-brand">VOICE TERMINAL · 语音终端</p>
      <h1>{{ sessionInvalid ? '会话已失效' : snapshot?.state === 'stopped' ? '应用已退出' : snapshot?.state === 'stopping' ? '正在退出应用' : snapshot?.startup_complete ? '启动需要检查' : '正在启动系统' }}</h1>
      <p aria-live="polite">{{ sessionInvalid ? '当前登录会话已被桌面宿主拒绝。' : snapshot?.phase || '正在连接独立启动程序' }}</p>
      <div v-if="!sessionInvalid" class="application-startup-progress"><span>已完成 {{ snapshot?.completed_components || 0 }} / {{ snapshot?.total_components || 0 }} 项启动检查</span><span>已耗时 {{ elapsed }} 秒</span></div>
      <progress v-if="!sessionInvalid" :value="snapshot?.completed_components || 0" :max="snapshot?.total_components || 1" />
      <ul v-if="!sessionInvalid"><li v-for="component in snapshot?.components || []" :key="component.id"><div><strong>{{ component.name }}</strong><span :class="component.state">{{ applicationStateLabel(component.state) }}</span></div><small>{{ component.phase }}</small><p v-if="component.error" class="application-error">{{ component.error }}</p></li></ul>
      <p v-if="error" class="application-error">{{ error }}</p>
      <div v-if="sessionInvalid" class="application-startup-actions"><button type="button" @click="application.exit">退出应用并重新启动</button></div>
      <div v-else-if="snapshot?.state !== 'stopped' && !snapshot?.stopping" class="application-startup-actions"><button type="button" @click="inspect">查看本地组件</button><button v-if="businessReady" type="button" @click="configure">打开模型设置</button><button v-if="canRetry" type="button" :disabled="retryingAll" @click="application.retryFailed">{{ retryingAll ? '正在恢复组件' : '重试失败组件' }}</button><button type="button" @click="application.exit">退出应用</button></div>
    </section>
  </div>
</template>

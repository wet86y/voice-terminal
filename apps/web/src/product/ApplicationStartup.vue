<script setup lang="ts">
import { computed } from 'vue'
import { useApplicationHost, applicationStateLabel } from './application-host'
const application = useApplicationHost()
const { snapshot, overlay, error, now, retryingAll } = application
const emit = defineEmits<{ system: [] }>()
const elapsed = computed(() => snapshot.value ? Math.max(0, Math.floor(now.value / 1000 - snapshot.value.started_at)) : 0)
const canRetry = computed(() => snapshot.value?.components.some(c => c.retry_allowed))
function inspect() { application.inspect(); emit('system') }
</script>

<template>
  <div v-if="overlay" class="application-startup" role="dialog" aria-modal="true" aria-label="应用启动与运行反馈">
    <section class="application-startup-card">
      <p class="application-brand">VOICE TERMINAL · 语音终端</p>
      <h1>{{ snapshot?.state === 'stopped' ? '应用已退出' : snapshot?.state === 'stopping' ? '正在退出应用' : snapshot?.startup_complete ? '启动需要检查' : '正在启动系统' }}</h1>
      <p aria-live="polite">{{ snapshot?.phase || '正在连接独立启动程序' }}</p>
      <div class="application-startup-progress"><span>已完成 {{ snapshot?.completed_components || 0 }} / {{ snapshot?.total_components || 0 }} 项启动检查</span><span>已耗时 {{ elapsed }} 秒</span></div>
      <progress :value="snapshot?.completed_components || 0" :max="snapshot?.total_components || 1" />
      <ul><li v-for="component in snapshot?.components || []" :key="component.id"><div><strong>{{ component.name }}</strong><span :class="component.state">{{ applicationStateLabel(component.state) }}</span></div><small>{{ component.phase }}</small><p v-if="component.error" class="application-error">{{ component.error }}</p></li></ul>
      <p v-if="error" class="application-error">{{ error }}</p>
      <div v-if="snapshot?.state !== 'stopped' && !snapshot?.stopping" class="application-startup-actions"><button type="button" @click="inspect">查看系统运行</button><button v-if="canRetry" type="button" :disabled="retryingAll" @click="application.retryFailed">{{ retryingAll ? '正在恢复组件' : '重试失败组件' }}</button><button type="button" @click="application.exit">退出应用</button></div>
    </section>
  </div>
</template>

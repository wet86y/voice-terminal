<script setup lang="ts">
import { computed } from 'vue'
import { useApplicationHost } from './application-host'
import { startupIcon } from '../runtime/startup-screen'
import { startupActivity, startupProgress } from '../runtime/startup-state'
const application = useApplicationHost()
const { snapshot, overlay, error, retryingAll, sessionInvalid } = application
const emit = defineEmits<{ system: [] }>()
const invalid = computed(() => Boolean(sessionInvalid?.value))
const failed = computed(() => invalid.value || Boolean(error.value) || snapshot.value?.state === 'failed')
const title = computed(() => invalid.value ? '会话已失效' : snapshot.value?.state === 'stopped' ? '应用已退出' : snapshot.value?.stopping ? '正在退出应用' : failed.value ? '启动遇到问题' : '应用启动中')
const detail = computed(() => invalid.value ? '请退出应用后重新启动' : error.value || startupActivity(snapshot.value))
const progress = computed(() => startupProgress(snapshot.value))
const canSkip = computed(() => !invalid.value && snapshot.value?.state !== 'stopped' && !snapshot.value?.stopping)
const canRetry = computed(() => snapshot.value?.components.some(c => c.retry_allowed))
function inspect() { application.inspect(); emit('system') }
</script>

<template>
  <div v-if="overlay" class="startup-screen application-startup" role="dialog" aria-modal="true" aria-label="应用启动与运行反馈">
    <section class="startup-content">
      <img class="startup-icon" :src="startupIcon" alt="语音终端" width="72" height="72">
      <h1 class="startup-title">{{ title }}</h1>
      <p class="startup-status" :class="{ 'is-error': failed }" aria-live="polite">{{ detail }}</p>
      <div v-if="!failed && snapshot?.state !== 'stopped'" class="startup-progress" :data-indeterminate="!snapshot || snapshot.stopping" role="progressbar" aria-label="应用启动进度" :aria-valuenow="snapshot && !snapshot.stopping ? progress : undefined" aria-valuemin="0" aria-valuemax="100"><span class="startup-progress-fill" :style="snapshot && !snapshot.stopping ? { width: progress + '%' } : undefined"></span></div>
      <div class="startup-actions">
        <button v-if="canSkip" class="startup-button" type="button" @click="inspect">跳过</button>
        <button v-if="failed && canRetry" class="startup-button" type="button" :disabled="retryingAll" @click="application.retryFailed">{{ retryingAll ? '正在重试' : '重试' }}</button>
        <button v-if="failed" class="startup-button" type="button" @click="application.exit">退出应用</button>
      </div>
    </section>
  </div>
</template>

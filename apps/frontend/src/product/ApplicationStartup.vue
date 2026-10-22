<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useApplicationHost } from './application-host'
import { startupIcon } from '../runtime/startup-screen'
import { startupActivity, startupComponents, startupProgress } from '../runtime/startup-state'
const application = useApplicationHost()
const props = defineProps<{
  entering?: boolean // entering（加载完成后正在交接主页图标）。
  exiting?: boolean // exiting（原生窗口或宿主已请求退出）。
}>()
const { snapshot, overlay, error, retryingAll, sessionInvalid } = application
const invalid = computed(() => Boolean(sessionInvalid?.value))
const failed = computed(() => !props.exiting && (invalid.value || Boolean(error.value) || snapshot.value?.state === 'failed'))
const title = computed(() => props.exiting ? '正在退出应用' : invalid.value ? '会话已失效' : snapshot.value?.state === 'stopped' ? '应用已退出' : snapshot.value?.stopping ? '正在退出应用' : failed.value ? '启动遇到问题' : '应用启动中')
const detail = computed(() => props.exiting ? snapshot.value?.stopping ? startupActivity(snapshot.value) : '正在关闭本地组件' : invalid.value ? '请退出应用后重新启动' : error.value || startupActivity(snapshot.value))
const startupItems = computed(() => !props.exiting && !failed.value ? startupComponents(snapshot.value) : null) // startupItems（当前滚动显示的组件名称）。
const progress = computed(() => startupProgress(snapshot.value))
const canSkip = computed(() => !props.exiting && !invalid.value && snapshot.value?.state !== 'stopped' && !snapshot.value?.stopping)
const canRetry = computed(() => snapshot.value?.components.some(c => c.retry_allowed))
const statusViewport = ref<HTMLElement | null>(null) // statusViewport（组件名称滚动区域）。
const statusText = ref<HTMLElement | null>(null) // statusText（未复制的组件名称，用于测量长度）。
const statusOverflowing = ref(false) // statusOverflowing（组件名称是否超过可见区域）。
const statusScrollDuration = ref('8s') // statusScrollDuration（滚动一轮所需时间）。
function measureStatus() {
  const viewport = statusViewport.value
  const text = statusText.value
  if (!viewport || !text) { statusOverflowing.value = false; return }
  const width = text.getBoundingClientRect().width // width（组件名称当前显示宽度）。
  statusOverflowing.value = width > viewport.clientWidth + 1
  statusScrollDuration.value = `${Math.max(8, (width + 32) / 24)}s`
}
watch(startupItems, async () => { await nextTick(); measureStatus() }, { flush: 'post' })
onMounted(() => { window.addEventListener('resize', measureStatus); measureStatus() })
onUnmounted(() => window.removeEventListener('resize', measureStatus))
function inspect() { application.inspect() }
</script>

<template>
  <div v-if="overlay || entering || exiting" class="startup-screen application-startup" :class="{ 'is-exiting': exiting }" role="dialog" aria-modal="true" :aria-hidden="entering || undefined" aria-label="应用启动与运行反馈">
    <section class="startup-content">
      <img class="startup-icon" :src="startupIcon" alt="语音终端" width="72" height="72">
      <h1 class="startup-title">{{ title }}</h1>
      <p v-if="startupItems" class="startup-status startup-status-components" :aria-label="`正在启动：${startupItems}`" aria-live="polite">
        <span class="startup-status-prefix">正在启动：</span>
        <span ref="statusViewport" class="startup-status-marquee">
          <span class="startup-status-track" :class="{ 'is-overflowing': statusOverflowing }" :style="{ '--startup-marquee-duration': statusScrollDuration }">
            <span ref="statusText" class="startup-status-item">{{ startupItems }}</span>
            <span v-if="statusOverflowing" class="startup-status-item" aria-hidden="true">{{ startupItems }}</span>
          </span>
        </span>
      </p>
      <p v-else class="startup-status" :class="{ 'is-error': failed }" aria-live="polite">{{ detail }}</p>
      <div v-if="exiting || (!failed && snapshot?.state !== 'stopped')" class="startup-progress" :data-indeterminate="exiting || !snapshot || snapshot.stopping" role="progressbar" :aria-label="exiting ? '应用退出进度' : '应用启动进度'" :aria-valuenow="snapshot && !snapshot.stopping && !exiting ? progress : undefined" aria-valuemin="0" aria-valuemax="100"><span class="startup-progress-fill" :style="snapshot && !snapshot.stopping && !exiting ? { width: progress + '%' } : undefined"></span></div>
      <div class="startup-actions">
        <button v-if="canSkip" class="startup-button" type="button" :disabled="entering" @click="inspect">跳过</button>
        <button v-if="failed && canRetry" class="startup-button" type="button" :disabled="retryingAll" @click="application.retryFailed">{{ retryingAll ? '正在重试' : '重试' }}</button>
        <button v-if="failed" class="startup-button" type="button" @click="application.exit">退出应用</button>
      </div>
    </section>
  </div>
</template>

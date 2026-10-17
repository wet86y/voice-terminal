<script setup lang="ts">
import { computed } from 'vue'
import type { ManagedResourceJob } from './local-components'

const props = defineProps<{
  /** job（当前资源任务）与 label（对应组件的中文名称）。 */
  job: ManagedResourceJob
  label: string
}>()
const active = computed(() => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(props.job.state))
function progressPercent(done: number | null | undefined, total: number | null | undefined) {
  if (typeof total !== 'number' || !Number.isFinite(total) || total <= 0
    || typeof done !== 'number' || !Number.isFinite(done)) return undefined
  return Math.max(0, Math.min(100, Math.round(done / total * 100)))
}
const percent = computed(() => {
  return progressPercent(props.job.completedBytes, props.job.totalBytes)
})
const downloadPercent = computed(() => progressPercent(props.job.downloadCompletedBytes, props.job.downloadTotalBytes))
const verificationPercent = computed(() => progressPercent(props.job.verificationCompletedBytes, props.job.verificationTotalBytes))
const hasBatchProgress = computed(() => downloadPercent.value !== undefined && verificationPercent.value !== undefined)
function formatBytes(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return ''
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GiB`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${Math.round(value)} B`
}
</script>

<template>
  <section class="managed-resource-progress" :data-job="job.id" :aria-label="`${label}任务进度`">
    <p class="managed-job-phase" role="status">{{ job.phase }}</p>
    <div v-if="hasBatchProgress" class="managed-job-progresses">
      <div class="managed-job-progress-wrap">
        <div class="managed-job-progress" role="progressbar" :aria-label="`${label}下载进度`" :aria-valuemin="0" :aria-valuemax="100" :aria-valuenow="downloadPercent"><span :style="{ width: `${downloadPercent}%` }"></span></div>
        <small>下载：{{ formatBytes(job.downloadCompletedBytes) }} / {{ formatBytes(job.downloadTotalBytes) }} · {{ downloadPercent }}%</small>
      </div>
      <div class="managed-job-progress-wrap">
        <div class="managed-job-progress" role="progressbar" :aria-label="`${label}摘要校验进度`" :aria-valuemin="0" :aria-valuemax="100" :aria-valuenow="verificationPercent"><span :style="{ width: `${verificationPercent}%` }"></span></div>
        <small>摘要校验：{{ formatBytes(job.verificationCompletedBytes) }} / {{ formatBytes(job.verificationTotalBytes) }} · {{ verificationPercent }}%</small>
      </div>
    </div>
    <div v-else-if="active || percent !== undefined" class="managed-job-progress-wrap">
      <div class="managed-job-progress" :class="{ indeterminate: percent === undefined }" role="progressbar" :aria-label="`${label}进度`" :aria-valuemin="0" :aria-valuemax="100" :aria-valuenow="percent">
        <span :style="percent !== undefined ? { width: `${percent}%` } : undefined"></span>
      </div>
      <small v-if="percent !== undefined">{{ formatBytes(job.completedBytes) }} / {{ formatBytes(job.totalBytes) }} · {{ percent }}%</small>
      <small v-else>请稍候…</small>
    </div>
    <p v-if="job.error" class="managed-job-error" role="alert">{{ job.error }}</p>
  </section>
</template>

<style scoped>
.managed-resource-progress{display:grid;gap:8px;min-width:0}
.managed-job-phase,.managed-job-error{margin:0;font-size:var(--font-caption);color:var(--muted);overflow-wrap:anywhere;white-space:pre-wrap}
.managed-job-error{color:var(--danger)}
.managed-job-progresses{display:grid;gap:7px;min-width:0}
.managed-job-progress-wrap{display:grid;grid-template-columns:minmax(60px,1fr) auto;align-items:center;gap:10px}
.managed-job-progress-wrap small{white-space:nowrap;font-size:var(--font-caption);font-variant-numeric:tabular-nums;color:var(--muted)}
.managed-job-progress{height:7px;border-radius:999px;background:var(--line-strong);overflow:hidden}
.managed-job-progress span{display:block;height:100%;background:var(--accent);transition:width 150ms ease}
.managed-job-progress.indeterminate span{width:30%;animation:resource-progress 1.5s ease-in-out infinite alternate}
@keyframes resource-progress{from{transform:translateX(0)}to{transform:translateX(230%)}}
@container (max-width:440px){.managed-job-progress-wrap{grid-template-columns:1fr;gap:6px}.managed-job-progress-wrap small{text-align:right}}
@media (prefers-reduced-motion:reduce){.managed-job-progress span{transition:none;animation:none}}
</style>

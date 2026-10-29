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
const kind = computed(() => props.job.progressKind ?? (/校验|核验/.test(props.job.phase) ? 'verify' : /下载/.test(props.job.phase) ? 'download' : undefined))
const done = computed(() => kind.value === 'download' ? props.job.downloadCompletedBytes ?? props.job.completedBytes
  : kind.value === 'verify' ? props.job.verificationCompletedBytes ?? props.job.completedBytes : props.job.completedBytes)
const total = computed(() => kind.value === 'download' ? props.job.downloadTotalBytes ?? props.job.totalBytes
  : kind.value === 'verify' ? props.job.verificationTotalBytes ?? props.job.totalBytes : props.job.totalBytes)
const percent = computed(() => props.job.progressKind && ['install', 'waiting'].includes(props.job.progressKind)
  ? undefined : progressPercent(done.value, total.value))
const stageLabel = computed(() => kind.value === 'download' ? '下载：' : kind.value === 'verify' ? '校验：' : '')
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
    <p class="managed-job-phase" role="status">{{ job.attempt && job.attempt > 1 ? `第 ${job.attempt} / 3 轮 · ` : '' }}{{ job.phase }}</p>
    <div v-if="active || percent !== undefined" class="managed-job-progress-wrap">
      <div class="managed-job-progress" :class="{ indeterminate: percent === undefined }" role="progressbar" :aria-label="`${label}进度`" :aria-valuemin="0" :aria-valuemax="100" :aria-valuenow="percent">
        <span :style="percent !== undefined ? { width: `${percent}%` } : undefined"></span>
      </div>
      <small v-if="percent !== undefined">{{ stageLabel }}{{ formatBytes(done) }} / {{ formatBytes(total) }} · {{ percent }}%</small>
      <small v-else>请稍候…</small>
    </div>
    <p v-if="job.error" class="managed-job-error" role="alert">{{ job.error }}</p>
    <p v-if="job.recovery?.message" class="managed-job-phase" role="status">{{ job.recovery.message }}</p>
  </section>
</template>

<style scoped>
.managed-resource-progress{display:grid;gap:8px;min-width:0}
.managed-job-phase,.managed-job-error{margin:0;font-size:var(--font-caption);color:var(--muted);overflow-wrap:anywhere;white-space:pre-wrap}
.managed-job-error{color:var(--danger)}
.managed-job-progresses{display:grid;gap:7px;min-width:0}
.managed-job-progress-wrap{display:grid;grid-template-columns:minmax(0,1fr);align-items:stretch;gap:6px;min-width:0}
.managed-job-progress-wrap small{justify-self:end;max-width:100%;white-space:normal;text-align:right;overflow-wrap:anywhere;font-size:var(--font-caption);font-variant-numeric:tabular-nums;color:var(--muted)}
.managed-job-progress{position:relative;width:100%;min-width:0;height:7px;border-radius:999px;background:var(--line-strong);overflow:hidden}
.managed-job-progress span{position:absolute;inset:0 auto 0 0;display:block;border-radius:inherit;background:var(--accent);transition:width 150ms ease}
.managed-job-progress.indeterminate span{width:30%;animation:resource-progress 1.5s ease-in-out infinite alternate}
@keyframes resource-progress{from{transform:translateX(0)}to{transform:translateX(230%)}}
@media (prefers-reduced-motion:reduce){.managed-job-progress span{transition:none;animation:none}}
</style>

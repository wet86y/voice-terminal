<script setup lang="ts">
import { computed, ref } from 'vue'
import UiDialog from '../../interface/UiDialog.vue'
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue'
import AppUpdateCard from '../../interface/AppUpdateCard.vue'
import WorkspaceLocationCard from '../../interface/WorkspaceLocationCard.vue'
import type { useWorkspaceResources } from '../useWorkspaceResources'

const props = defineProps<{ controller: ReturnType<typeof useWorkspaceResources> }>()
const candidateRoot = ref('')
const pickerBusy = ref(false)
const migrationConfirm = ref(false)
const canManage = computed(() => props.controller.canManage.value)
const workspace = computed(() => props.controller.workspace.value)
const activeJob = computed(() => props.controller.activeWorkspaceJob.value)
const latestJob = computed(() => props.controller.latestWorkspaceJob.value)
const isMigration = computed(() => latestJob.value?.phase.toLowerCase().includes('迁移')
  || latestJob.value?.phase.toLowerCase().includes('copy')
  || latestJob.value?.phase.toLowerCase().includes('复制'))
const progress = computed(() => {
  const job = latestJob.value
  if (!job?.total_bytes || job.total_bytes <= 0 || job.completed_bytes == null) return undefined
  return Math.max(0, Math.min(100, Math.round(job.completed_bytes / job.total_bytes * 100)))
})
const controlsBusy = computed(() => pickerBusy.value || props.controller.workspaceOperationStarting.value || Boolean(activeJob.value))
const updateStatus = computed(() => props.controller.appUpdate.value)

async function chooseTarget() {
  if (controlsBusy.value || !canManage.value) return
  pickerBusy.value = true
  candidateRoot.value = ''
  try {
    const choice = await props.controller.chooseWorkspace()
    if (choice) {
      candidateRoot.value = choice.root
      migrationConfirm.value = true
    }
  } finally {
    pickerBusy.value = false
  }
}

async function confirmMigration() {
  if (!candidateRoot.value || controlsBusy.value) return
  migrationConfirm.value = false
  await props.controller.migrateWorkspace(candidateRoot.value)
}

function cancelJob() {
  const job = activeJob.value
  if (job?.can_cancel === false) return
  if (job) void props.controller.cancelJob(job.job_id)
}

function formatBytes(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return ''
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB（吉字节）`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB（兆字节）`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB（千字节）`
  return `${Math.round(value)} B（字节）`
}
</script>

<template>
  <section class="workspace-settings" aria-labelledby="workspace-settings-title">
    <SettingsPageHeader section="workspace" title-id="workspace-settings-title" />

    <AppUpdateCard :status="updateStatus" :current-version="workspace?.version" :disabled="controlsBusy || !canManage" @check="controller.checkAppUpdate()" @download="controller.downloadAppUpdate()" @cancel="controller.cancelAppUpdate()" @repository="controller.openProjectRepository()" />

    <div v-if="controller.loading.value && !workspace" class="workspace-empty" role="status">正在读取工作区信息…</div>
    <p v-if="controller.workspaceError.value" class="workspace-error" role="alert">{{ controller.workspaceError.value }}</p>

    <WorkspaceLocationCard v-if="workspace" :root="workspace.root" :can-manage="canManage" :disabled="controlsBusy" :choosing="pickerBusy" @migrate="chooseTarget" />

    <section v-if="latestJob" class="workspace-job-card" :class="{ failed: latestJob.state === 'failed' }" aria-live="polite">
      <div class="workspace-job-heading"><div><strong>{{ isMigration ? '工作区迁移' : '工作区切换' }}</strong><span>{{ latestJob.state === 'failed' ? '失败' : ['complete', 'completed'].includes(latestJob.state) ? '已完成' : ['cancelled', 'canceled'].includes(latestJob.state) ? '已取消' : '处理中' }}</span></div><button v-if="activeJob && latestJob.can_cancel !== false" class="text-action" type="button" @click="cancelJob">取消迁移</button></div>
      <p>{{ latestJob.phase }}</p>
      <div v-if="progress !== undefined" class="workspace-progress-wrap"><div class="workspace-progress" role="progressbar" aria-label="工作区迁移进度" :aria-valuemin="0" :aria-valuemax="100" :aria-valuenow="progress"><span :style="{ width: `${progress}%` }"></span></div><small>{{ formatBytes(latestJob.completed_bytes) }} / {{ formatBytes(latestJob.total_bytes) }} · {{ progress }}%</small></div>
      <p v-if="latestJob.error" class="workspace-error" role="alert">{{ latestJob.error }}</p>
    </section>

    <p v-if="controller.eventError.value" class="workspace-error" role="status">{{ controller.eventError.value }}</p>

    <UiDialog :open="migrationConfirm" title="迁移并清理旧工作区？" @close="migrationConfirm = false">
      <div class="workspace-confirm-copy"><p>将工作区数据从当前目录复制到所选位置。</p><dl><div><dt>当前目录</dt><dd>{{ workspace?.root }}</dd></div><div><dt>目标目录</dt><dd>{{ candidateRoot }}</dd></div></dl><p>只有复制和校验成功后才会切换，并清理旧位置。</p></div>
      <template #actions><button class="outline-button" type="button" @click="migrationConfirm = false">返回</button><button class="save-button workspace-confirm-button" type="button" @click="confirmMigration">开始迁移</button></template>
    </UiDialog>
  </section>
</template>

<style scoped>
.workspace-settings{display:grid;gap:18px;min-width:0}
.workspace-job-card{min-width:0;border:1px solid var(--line);border-radius:14px;background:#ffffff02;padding:18px}
.workspace-job-heading,.workspace-job-heading>div{display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0}
.workspace-confirm-copy dl{display:grid;gap:12px;margin:0}
.workspace-confirm-copy dl>div{display:grid;gap:5px;min-width:0}
.workspace-confirm-copy dt{color:var(--muted);font-size:var(--font-caption)}
.workspace-confirm-copy dd{margin:0;overflow-wrap:anywhere;word-break:break-word;font-size:var(--font-body);line-height:1.55}
.workspace-empty{padding:16px;border:1px dashed var(--line-strong);border-radius:12px;color:var(--muted)}
.workspace-error{margin:0;color:var(--danger);font-size:var(--font-caption);overflow-wrap:anywhere;white-space:pre-wrap}
.workspace-job-card{display:grid;gap:8px}
.workspace-job-card.failed{border-color:color-mix(in srgb,var(--danger) 35%,transparent)}
.workspace-job-heading>div{justify-content:flex-start;flex-wrap:wrap}
.workspace-job-heading strong{font-size:var(--font-body)}
.workspace-job-heading span,.workspace-job-card>p{font-size:var(--font-caption);color:var(--muted)}
.workspace-job-card>p{overflow-wrap:anywhere;line-height:1.5}
.workspace-progress-wrap{display:grid;grid-template-columns:minmax(80px,1fr) auto;align-items:center;gap:10px}
.workspace-progress-wrap small{font-size:var(--font-caption);color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.workspace-progress{height:7px;overflow:hidden;border-radius:999px;background:var(--line-strong)}
.workspace-progress span{display:block;height:100%;background:var(--accent);transition:width 150ms ease}
.workspace-confirm-copy{display:grid;gap:14px;margin:16px 0;color:var(--muted);font-size:var(--font-body);line-height:1.55}
.workspace-confirm-copy dl{padding:12px;border:1px solid var(--line);border-radius:10px}
.workspace-confirm-button{min-height:var(--compact-control-height);padding:7px 14px}
@media(max-width:560px){.workspace-job-card{padding:14px}.workspace-job-heading{align-items:flex-start}.workspace-progress-wrap{grid-template-columns:1fr;gap:6px}.workspace-progress-wrap small{text-align:right}}
</style>

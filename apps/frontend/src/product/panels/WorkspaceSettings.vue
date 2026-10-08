<script setup lang="ts">
import { computed, ref } from 'vue'
import UiDialog from '../../interface/UiDialog.vue'
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue'
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

async function chooseTarget() {
  if (controlsBusy.value || !canManage.value) return
  pickerBusy.value = true
  candidateRoot.value = ''
  try {
    const choice = await props.controller.chooseWorkspace()
    if (choice) candidateRoot.value = choice.root
  } finally {
    pickerBusy.value = false
  }
}

async function linkTarget() {
  if (!candidateRoot.value || controlsBusy.value) return
  await props.controller.linkWorkspace(candidateRoot.value)
}

function requestMigration() {
  if (!candidateRoot.value || controlsBusy.value) return
  migrationConfirm.value = true
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
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${Math.round(value)} B`
}
</script>

<template>
  <section class="workspace-settings" aria-labelledby="workspace-settings-title">
    <SettingsPageHeader section="workspace" title-id="workspace-settings-title" />

    <div v-if="controller.loading.value && !workspace" class="workspace-empty" role="status">正在读取工作区信息…</div>
    <p v-if="controller.workspaceError.value" class="workspace-error" role="alert">{{ controller.workspaceError.value }}</p>

    <section v-if="workspace" class="workspace-card" aria-labelledby="workspace-location-title">
      <div class="workspace-card-heading"><div><p class="section-label">WORKSPACE（工作区）</p><h3 id="workspace-location-title">当前存储位置</h3></div><span class="workspace-channel">{{ workspace.channel === 'development' ? '开发运行' : '分发版本' }}</span></div>
      <dl class="workspace-paths">
        <div><dt>{{ workspace.channel === 'development' ? '项目根目录' : '工作区根目录' }}</dt><dd :title="workspace.channel === 'development' ? (workspace.project_root || workspace.root) : workspace.root">{{ workspace.channel === 'development' ? (workspace.project_root || workspace.root) : workspace.root }}</dd></div>
        <div><dt>数据目录</dt><dd :title="workspace.data_dir">{{ workspace.data_dir }}</dd></div>
      </dl>
      <p v-if="workspace.channel === 'development'" class="workspace-hint">开发运行固定使用项目的 .local（本机资源）目录；工作区选择与迁移仅适用于分发版本。</p>
      <small class="workspace-version">应用版本：{{ workspace.version || '未知' }}</small>

      <template v-if="canManage">
        <div class="workspace-actions">
          <button class="outline-button" type="button" :disabled="controlsBusy" @click="chooseTarget">{{ pickerBusy ? '正在选择…' : '选择工作区位置' }}</button>
        </div>
        <div v-if="candidateRoot" class="workspace-target">
          <span>目标位置</span><strong :title="candidateRoot">{{ candidateRoot }}</strong>
          <div class="workspace-target-actions"><button class="outline-button" type="button" :disabled="controlsBusy" @click="linkTarget">链接并切换</button><button class="outline-button workspace-migrate-action" type="button" :disabled="controlsBusy" @click="requestMigration">迁移数据并清理旧位置</button></div>
        </div>
        <p class="workspace-hint">链接仅切换到所选工作区；迁移会完整复制并校验数据，成功切换后清理旧位置。</p>
      </template>
    </section>

    <section class="workspace-update-card" aria-labelledby="workspace-update-title">
      <div><p class="section-label">UPDATE（更新）</p><h3 id="workspace-update-title">应用更新</h3><p>在线更新尚未启用。当前版本 {{ workspace?.version || '—' }}。</p></div>
      <button class="outline-button" type="button" disabled>检查更新</button>
    </section>

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
.workspace-card,.workspace-update-card,.workspace-job-card{min-width:0;border:1px solid var(--line);border-radius:14px;background:#ffffff02;padding:18px}
.workspace-card{display:grid;gap:16px}
.workspace-card-heading,.workspace-update-card,.workspace-job-heading,.workspace-job-heading>div{display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0}
.workspace-card-heading>div,.workspace-update-card>div{display:grid;gap:6px;min-width:0}
.workspace-card h3,.workspace-update-card h3{font-size:var(--font-section)}
.workspace-channel{flex:none;padding:5px 9px;border:1px solid var(--line-strong);border-radius:999px;color:var(--muted);font-size:var(--font-caption);white-space:nowrap}
.workspace-paths,.workspace-confirm-copy dl{display:grid;gap:12px;margin:0}
.workspace-paths>div,.workspace-confirm-copy dl>div{display:grid;gap:5px;min-width:0}
.workspace-paths dt,.workspace-confirm-copy dt{color:var(--muted);font-size:var(--font-caption)}
.workspace-paths dd,.workspace-confirm-copy dd{margin:0;overflow-wrap:anywhere;word-break:break-word;font-size:var(--font-body);line-height:1.55}
.workspace-version,.workspace-hint{color:var(--muted);font-size:var(--font-caption);line-height:1.6}
.workspace-actions button,.workspace-target-actions button,.workspace-update-card button{min-height:var(--compact-control-height);padding:7px 12px;font-size:var(--font-body)}
.workspace-target{display:grid;gap:8px;padding:14px;border:1px solid var(--line-strong);border-radius:12px;background:#0000000b;min-width:0}
.workspace-target>span{font-size:var(--font-caption);color:var(--muted)}
.workspace-target>strong{overflow-wrap:anywhere;word-break:break-word;font-size:var(--font-body);line-height:1.55}
.workspace-target-actions{display:flex;gap:8px;flex-wrap:wrap}
.workspace-migrate-action{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 32%,transparent)}
.workspace-update-card>div>p:last-child{font-size:var(--font-caption);color:var(--muted);line-height:1.55}
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
@media(max-width:560px){.workspace-card,.workspace-update-card,.workspace-job-card{padding:14px}.workspace-card-heading,.workspace-update-card,.workspace-job-heading{align-items:flex-start}.workspace-update-card{flex-direction:column}.workspace-update-card button{width:100%}.workspace-target-actions{display:grid;grid-template-columns:1fr}.workspace-progress-wrap{grid-template-columns:1fr;gap:6px}.workspace-progress-wrap small{text-align:right}}
</style>

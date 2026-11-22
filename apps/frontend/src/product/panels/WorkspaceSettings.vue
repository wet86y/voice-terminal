<script setup lang="ts">
import { formatBytes } from '../../interface/format-bytes'
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import ManagedResourceProgress from '../../interface/ManagedResourceProgress.vue'
import type { ManagedResourceJob } from '../../interface/local-components'
import UiDialog from '../../interface/UiDialog.vue'
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue'
import AppUpdateCard from '../../interface/AppUpdateCard.vue'
import WorkspaceLocationCard from '../../interface/WorkspaceLocationCard.vue'
import { desktopPort, type DesktopPort, type DesktopWorkspacePort, type WorkspaceCleanup } from '../desktop-port'
import type { DesktopSettingsSection } from '../../interface/settings'
import type { useWorkspaceResources } from '../useWorkspaceResources'

const props = defineProps<{ controller: ReturnType<typeof useWorkspaceResources>; storagePort?: DesktopPort & DesktopWorkspacePort }>()
const emit = defineEmits<{ 'open-settings': [section: DesktopSettingsSection] }>()
const candidateRoot = ref('')
const pickerBusy = ref(false)
const migrationConfirm = ref(false)
const cleanupPlan = ref<WorkspaceCleanup | null>(null)
const cleanupResult = ref<WorkspaceCleanup | null>(null)
const cleanupBusy = ref(false)
const cleanupExecuting = ref(false) // cleanupExecuting（所选垃圾正在执行回收）。
const storageOpen = ref(false) // storageOpen（本机空间管理展开状态）。
const selectedEntries = ref<string[]>([]) // selectedEntries（用户明确勾选的垃圾条目）。
const storageError = ref('') // storageError（本机维护错误）。
const uninstallPlan = ref<Awaited<ReturnType<DesktopWorkspacePort['uninstallPlan']>> | null>(null)
const uninstallProgress = ref<ManagedResourceJob | null>(null) // uninstallProgress（正式本机卸载任务的进度投影）。
const uninstallJob = ref<string | null>(null) // uninstallJob（等待完成的本机卸载任务）。

const canManage = computed(() => props.controller.canManage.value)
const installedComponents = computed(() => (cleanupPlan.value?.components ?? []).filter(item => item.installed)) // installedComponents（仅展示本机已安装的可选组件）。
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
const controlsBusy = computed(() => pickerBusy.value || cleanupBusy.value || props.controller.workspaceOperationStarting.value || Boolean(activeJob.value))
const updateStatus = computed(() => props.controller.appUpdate.value)
const debugPort = props.storagePort ?? desktopPort() as DesktopPort & DesktopWorkspacePort
const localVersion = ref('') // localVersion（本机程序版本，不随远端目标切换）。
const localRoot = ref('') // localRoot（客户端本机工作区，与远端服务分离）。
const debugAccess = ref<boolean | null>(null)
const debugSaving = ref(false)
const debugError = ref('')

onMounted(async () => {
  if (!debugPort.available) return
  try {
    const info = await debugPort.workspaceInfo()
    localRoot.value = info.root
    localVersion.value = info.version
    debugAccess.value = info.allow_external_debug ?? null
  } catch (error) {
    debugError.value = error instanceof Error ? error.message : '无法读取外部调试设置。'
  }
})

async function changeDebugAccess(event: Event) {
  const control = event.target as HTMLInputElement
  const allowed = control.checked
  debugSaving.value = true
  debugError.value = ''
  try {
    await debugPort.setAgentDebugAllowed(allowed)
    debugAccess.value = allowed
  } catch (error) {
    control.checked = debugAccess.value === true
    debugError.value = error instanceof Error ? error.message : '无法保存外部调试设置。'
  } finally {
    debugSaving.value = false
  }
}

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

async function scanCleanup() {
  if (cleanupBusy.value) return
  cleanupBusy.value = true
  storageError.value = ''
  selectedEntries.value = []
  uninstallPlan.value = null
  try { cleanupPlan.value = await debugPort.storageScan() }
  catch (error) { storageError.value = String(error) }
  finally { cleanupBusy.value = false }
}
async function toggleStorage() {
  storageOpen.value = !storageOpen.value
  if (storageOpen.value) await scanCleanup()
}
async function confirmCleanup() {
  if (cleanupBusy.value || !cleanupPlan.value || !selectedEntries.value.length) return
  cleanupBusy.value = true
  storageError.value = ''
  let failure = ''
  cleanupExecuting.value = true
  try { cleanupResult.value = await debugPort.storageCleanup(cleanupPlan.value.token, selectedEntries.value) }
  catch (error) { failure = String(error) }
  finally { cleanupBusy.value = false; cleanupExecuting.value = false }
  await scanCleanup()
  if (failure) storageError.value = failure
}
async function previewUninstall(id: string) {
  storageError.value = ''
  try { uninstallPlan.value = await debugPort.uninstallPlan(id) }
  catch (error) { storageError.value = String(error) }
}
let storagePoll: ReturnType<typeof setTimeout> | undefined
let disposed = false
onBeforeUnmount(() => { disposed = true; clearTimeout(storagePoll) })
async function confirmUninstall() {
  const plan = uninstallPlan.value
  if (!plan || cleanupBusy.value || uninstallJob.value) return
  uninstallPlan.value = null
  try {
    uninstallJob.value = (await debugPort.uninstallResource(plan.resource_id, plan.token)).job_id
    const poll = async () => {
      try {
        const { job } = await debugPort.uninstallJobStatus(plan.resource_id, uninstallJob.value!)
        if (disposed) return
        if (job) uninstallProgress.value = { id:job.job_id, resourceId:job.resource_id, label:'组件卸载', state:job.state,
          phase:['complete','completed'].includes(job.state) ? '卸载完成，用户资料已保留。' : job.phase, canCancel:false,
          completedBytes:job.completed_bytes, totalBytes:job.total_bytes, error:job.error || undefined }
        if (job && ['complete','completed','failed','cancelled'].includes(job.state)) {
          const failure = job.error || ''
          uninstallJob.value = null
          await scanCleanup()
          if (failure) storageError.value = failure
          return
        }
        storagePoll = setTimeout(poll, 1000)
      } catch (error) { storageError.value = String(error); uninstallJob.value = null }
    }
    await poll()
  } catch (error) { storageError.value = String(error); uninstallJob.value = null }
}

function cancelJob() {
  const job = activeJob.value
  if (job?.can_cancel === false) return
  if (job) void props.controller.cancelJob(job.job_id)
}



</script>

<template>
  <section class="workspace-settings" aria-labelledby="workspace-settings-title">
    <SettingsPageHeader section="workspace" title-id="workspace-settings-title" />

    <AppUpdateCard :status="updateStatus" :integrity="controller.integrity.value" :current-version="localVersion || workspace?.version" :disabled="controlsBusy || !canManage" @download-integrity="controller.downloadIntegrity()" @check-integrity="controller.checkIntegrity()" @check="controller.checkAppUpdate()" @download="controller.downloadAppUpdate()" @cancel="controller.cancelAppUpdate()" @repository="controller.openProjectRepository()" />

    <div v-if="controller.loading.value && !workspace" class="workspace-empty" role="status">正在读取工作区信息…</div>
    <p v-if="controller.workspaceError.value" class="workspace-error" role="alert">{{ controller.workspaceError.value }}</p>

    <WorkspaceLocationCard v-if="localRoot || workspace" :root="localRoot || workspace?.root || ''" :can-manage="canManage" :can-cleanup="Boolean(localRoot)" :expanded="storageOpen" :disabled="controlsBusy" :choosing="pickerBusy" :cleaning="cleanupBusy" @cleanup="toggleStorage" @migrate="chooseTarget" />
    <section v-if="storageOpen" id="local-storage-panel" class="workspace-job-card settings-stack" aria-label="本机空间管理" :aria-busy="cleanupBusy">
      <div class="workspace-job-heading"><strong>本机空间管理</strong><button type="button" class="outline-button" :disabled="cleanupBusy || Boolean(uninstallJob)" @click="scanCleanup">重新扫描</button></div>
      <p>本机 · {{ cleanupPlan?.root || localRoot }}</p>
      <details v-if="cleanupPlan?.protected?.length"><summary>保留内容占用</summary><p v-for="item in cleanupPlan.protected" :key="item.path">{{ item.label }} · {{ formatBytes(item.bytes) }}<br />{{ item.reason }}</p></details>
      <p v-if="cleanupBusy" role="status">{{ cleanupExecuting ? '正在清理所选项目…' : '正在扫描本机工作区…' }}</p>
      <p v-if="storageError" role="alert">{{ storageError }}</p>
      <ManagedResourceProgress v-if="uninstallProgress" :job="uninstallProgress" label="组件卸载" />
      <p v-else-if="uninstallJob" role="status">正在卸载组件，请等待任务完成…</p>
      <template v-if="cleanupPlan">
        <h3>垃圾清理</h3><p>占用为估算逻辑容量，仅清理勾选项目。</p>
        <p v-if="!cleanupPlan.entries.length">当前未发现可清理的工作区文件。</p>
        <label v-for="entry in cleanupPlan.entries" :key="entry.path" class="storage-entry">
          <input v-model="selectedEntries" type="checkbox" :value="entry.path" :disabled="cleanupBusy || Boolean(uninstallJob)" :aria-label="`清理 ${entry.path}`" />
          <span><strong>{{ entry.reason }} · {{ formatBytes(entry.bytes) }}</strong><br />{{ entry.path }}</span>
        </label>
        <button class="save-button workspace-confirm-button" type="button" :disabled="!selectedEntries.length || cleanupBusy || Boolean(uninstallJob)" @click="confirmCleanup">清理所选</button>
        <details v-if="cleanupPlan.skipped.length"><summary>保留及跳过原因</summary><ul><li v-for="message in cleanupPlan.skipped" :key="message">{{ message }}</li></ul></details>
        <h3>组件卸载</h3><p>用户音色、录音与原始资料保留；使用中的组件请先在设置中停用。</p>
        <p v-if="!installedComponents.length" role="status">本机暂无已安装的可选组件。</p>
        <div v-for="item in installedComponents" :key="item.resource_id" class="storage-component">
          <strong>{{ item.label }}</strong><p>{{ formatBytes(item.bytes) }}</p>
          <p v-if="item.purpose">{{ item.purpose }}</p><p v-if="item.impact">{{ item.impact }}</p>
          <p v-if="item.blocked_reason">{{ item.blocked_reason }}</p>
          <button v-if="item.blocked_reason" type="button" class="text-action" @click="emit('open-settings', controller.remoteMode?.value ? 'devices' : item.resource_id.startsWith('tts-aniflive-') ? 'tts' : 'compute')">{{ controller.remoteMode?.value ? '切换本机运行模式' : '前往相关设置' }}</button>
          <button class="outline-button" type="button" :disabled="Boolean(item.blocked_reason) || cleanupBusy || Boolean(uninstallJob)" @click="previewUninstall(item.resource_id)">卸载</button>
          <div v-if="uninstallPlan?.resource_id === item.resource_id" role="group" aria-label="确认本机组件卸载">
            <p>{{ uninstallPlan.message }}</p><ul><li v-for="path in uninstallPlan.paths" :key="path">{{ path }}</li></ul>
            <details v-if="uninstallPlan.environment"><summary>处理器依赖准备 · 最多 {{ formatBytes(uninstallPlan.environment.total_bytes) }}</summary>
              <p>优先复用本机已校验依赖，仅下载缺失内容；实际下载量会减少。</p>
              <ul><li v-for="wheel in uninstallPlan.environment.wheels" :key="wheel.name">{{ wheel.name }} {{ wheel.version }} · {{ formatBytes(wheel.size) }}</li></ul>
            </details>
            <button type="button" class="outline-button" @click="uninstallPlan = null">取消</button>
            <button type="button" class="save-button" @click="confirmUninstall">确认卸载</button>
          </div>
        </div>
      </template>
    </section>

    <div v-if="cleanupResult" class="workspace-job-card" role="status">
      <strong>已清理 {{ cleanupResult.removed_count }} 项，共 {{ formatBytes(cleanupResult.removed_bytes) }}</strong>
      <details v-if="cleanupResult.skipped.length"><summary>{{ cleanupResult.skipped.length }} 项已跳过</summary><ul><li v-for="message in cleanupResult.skipped" :key="message">{{ message }}</li></ul></details>
    </div>

    <section v-if="latestJob" class="workspace-job-card" :class="{ failed: latestJob.state === 'failed' }" aria-live="polite">
      <div class="workspace-job-heading"><div><strong>{{ isMigration ? '工作区迁移' : '工作区切换' }}</strong><span>{{ latestJob.state === 'failed' ? '失败' : ['complete', 'completed'].includes(latestJob.state) ? '已完成' : ['cancelled', 'canceled'].includes(latestJob.state) ? '已取消' : '处理中' }}</span></div><button v-if="activeJob && latestJob.can_cancel !== false" class="text-action" type="button" @click="cancelJob">取消迁移</button></div>
      <p>{{ latestJob.phase }}</p>
      <div v-if="progress !== undefined" class="workspace-progress-wrap"><div class="workspace-progress" role="progressbar" aria-label="工作区迁移进度" :aria-valuemin="0" :aria-valuemax="100" :aria-valuenow="progress"><span :style="{ width: `${progress}%` }"></span></div><small>{{ formatBytes(latestJob.completed_bytes) }} / {{ formatBytes(latestJob.total_bytes) }} · {{ progress }}%</small></div>
      <p v-if="latestJob.error" class="workspace-error" role="alert">{{ latestJob.error }}</p>
    </section>

    <p v-if="controller.eventError.value" class="workspace-error" role="status">{{ controller.eventError.value }}</p>

    <section v-if="debugAccess !== null || debugError" class="workspace-job-card debug-access-card">
      <label v-if="debugAccess !== null" class="switch-row debug-access-toggle">
        <div><strong>允许外部调试</strong><span>允许同一系统账户查询状态和切换启动模式。调试模式下可操作此程序的控件和业务接口。</span></div>
        <input type="checkbox" role="switch" aria-label="允许外部调试" :checked="debugAccess" :disabled="debugSaving" @change="changeDebugAccess" /><i aria-hidden="true"></i>
      </label>
      <p v-if="debugError" class="workspace-error" role="alert">{{ debugError }}</p>
    </section>


    <UiDialog :open="migrationConfirm" title="迁移并清理旧工作区？" @close="migrationConfirm = false">
      <div class="workspace-confirm-copy"><p>将工作区数据从当前目录复制到所选位置。</p><dl><div><dt>当前目录</dt><dd>{{ workspace?.root }}</dd></div><div><dt>目标目录</dt><dd>{{ candidateRoot }}</dd></div></dl><p>只有复制和校验成功后才会切换，并清理旧位置。</p></div>
      <template #actions><button class="outline-button" type="button" @click="migrationConfirm = false">返回</button><button class="save-button workspace-confirm-button" type="button" @click="confirmMigration">开始迁移</button></template>
    </UiDialog>
  </section>
</template>

<style scoped>
.storage-entry{display:flex;gap:12px;align-items:flex-start;padding:12px 0;overflow-wrap:anywhere}.storage-component{padding:16px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}
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
.debug-access-toggle{padding:0;border:0;background:transparent}
.debug-access-toggle strong{font-size:var(--font-body)}
.debug-access-toggle span{font-size:var(--font-caption);line-height:1.5}
.debug-access-toggle input{right:0}
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
.cleanup-entries{display:grid;grid-auto-rows:max-content;gap:10px;list-style:none;margin:0;padding:12px;border:1px solid var(--line);border-radius:10px}
.cleanup-entries li{display:grid;gap:2px;overflow-wrap:anywhere}
.cleanup-entries strong{color:var(--text);font-weight:600}
.cleanup-entries span{font-size:var(--font-caption)}
@media(max-width:560px){.workspace-job-card{padding:14px}.workspace-job-heading{align-items:flex-start}.workspace-progress-wrap{grid-template-columns:1fr;gap:6px}.workspace-progress-wrap small{text-align:right}}
</style>

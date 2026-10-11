<script setup lang="ts">
import { computed, ref } from 'vue'
import SettingsPageHeader from './SettingsPageHeader.vue'
import ManagedResourceProgress from './ManagedResourceProgress.vue'
import type { LocalComponentRow, LocalComponentsModel, ManagedResourceJob } from './local-components'

const props = defineProps<{
  /** model（页面状态与指标）与 saving（保存中）。 */
  model: LocalComponentsModel
  saving?: boolean
}>()
const visibleResources = computed(() => props.model.resources.filter(resource => resource.used !== undefined))
// batchImportAvailable（批量导入入口）：缺失、不完整或存在必须处理的更新时展示。
const batchImportAvailable = computed(() => props.model.resourceActionsAvailable && props.model.rows.some(needsResource))
const emit = defineEmits<{
  /** refresh（刷新指标）与 retry（重试指定组件）。 */
  refresh: []
  retry: [id: string]
  /** resourceDownload/resourceImport（下载或导入资源）、openResourceSource（打开上游）、cancelResource（取消任务）。 */
  resourceDownload: [id: string]
  /** resourceRefresh（展开资源详情后刷新一次资源清单）。 */
  resourceRefresh: []
  resourceImport: [id: string]
  resourceImportAll: []
  openResourceSource: [id: string]
  cancelResource: [jobId: string]
  /** device（修改运行位置草稿）与 save（保存运行位置）。 */
  device: [id: string, value: string]
  save: [id: string]
}>()
const expanded = ref(new Set<string>())
// latestJobs（各资源的最近任务）：运行中的任务优先，旧失败记录不覆盖新下载。
const latestJobs = computed(() => {
  const jobs = new Map<string, ManagedResourceJob>()
  for (const job of props.model.managedResourceJobs ?? []) {
    const previous = jobs.get(job.resourceId)
    if (!previous || jobIsActive(job) || !jobIsActive(previous)) jobs.set(job.resourceId, job)
  }
  return jobs
})
const componentCards = computed(() => props.model.rows.map(row => {
  const job = visibleJob(latestJobs.value.get(row.resource?.id ?? row.id))
  // 没有独立安装资源的进程条目仅展示活动任务，历史下载错误不覆盖进程重启状态。
  return { row, job: row.resource || (job && jobIsActive(job)) ? job : undefined }
}))
const batchJob = computed(() => visibleJob(latestJobs.value.get('batch')))
function visibleJob(job: ManagedResourceJob | undefined) {
  return job && !['complete', 'completed'].includes(job.state) ? job : undefined
}
function jobIsActive(job?: ManagedResourceJob) {
  return Boolean(job && !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state))
}
function jobStateLabel(job: ManagedResourceJob) {
  if (job.state === 'failed') return '失败'
  if (job.state === 'cancelled' || job.state === 'canceled') return '已取消'
  if (jobIsActive(job)) return '处理中'
  return '已完成'
}
function jobState(job: ManagedResourceJob) {
  return job.state === 'failed' ? 'failed' : jobIsActive(job) ? 'loading' : 'disabled'
}
function resourceStateLabel(state: string) {
  return state === 'installed' ? '已安装' : state === 'incomplete' ? '不完整' : '缺失'
}
function needsResource(row: LocalComponentRow) {
  return Boolean(row.resource && (row.resource.state !== 'installed' || row.resource.updateAvailable))
}
function canExpand(row: LocalComponentRow) {
  return row.available && !needsResource(row)
}
function activate(row: LocalComponentRow) {
  if (!canExpand(row)) {
    if (row.retryable && !row.busy) emit('retry', row.id)
    return
  }
  if (expanded.value.has(row.id)) expanded.value.delete(row.id)
  else {
    expanded.value.add(row.id)
    if (row.resource && props.model.resourceActionsAvailable) emit('resourceRefresh')
  }
}
function changeDevice(id: string, event: Event) {
  const value = (event.target as HTMLSelectElement).value
  const option = props.model.rows.find(row => row.id === id)?.device?.options.find(option => option.value === value)
  if (option && !option.disabled) emit('device', id, value)
}
function invalidDevice(row: LocalComponentRow) {
  return row.device?.options.find(option => option.value === row.device?.value)?.disabled
}
</script>

<template>
  <section class="local-components" aria-labelledby="local-components-title">
    <SettingsPageHeader section="compute" title-id="local-components-title">
      <template #actions>
        <button class="outline-button local-refresh" type="button" :disabled="model.refreshing" @click="emit('refresh')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 7A7 7 0 0 1 18 5l2 2M4 17l2 2a7 7 0 0 0 11.9-2" /></svg>
          {{ model.refreshing ? '刷新中' : '刷新' }}
        </button>
      </template>
    </SettingsPageHeader>

    <div class="local-overview">
      <div class="local-overview-line"><span class="local-state" :class="model.state"><i aria-hidden="true"></i>{{ model.status }}</span><small>最近采样：{{ model.sampledAt }}</small></div>
      <div v-if="visibleResources.length" class="local-memory-summary">
        <article v-for="resource in visibleResources" :key="resource.id" :data-resource="resource.id"><span>{{ resource.label }}</span><div class="local-resource-value" :aria-label="`${resource.label}，已用 ${resource.used}${resource.total ? `，全部 ${resource.total}` : ''}`"><strong>{{ resource.used }}</strong><span v-if="resource.total" class="local-resource-total">/ {{ resource.total }}</span></div></article>
      </div>
    </div>

    <p v-if="model.error" class="local-error" role="alert">{{ model.error }}</p>
    <p v-if="model.resourceError" class="local-error" role="alert">{{ model.resourceError }}</p>
    <p v-if="model.resourceEventError" class="local-error" role="status">{{ model.resourceEventError }}</p>
    <div class="local-list-heading local-managed-heading">
      <div><h3>模型与组件</h3><small>{{ model.rows.length }} 项；安装状态独立于语音服务健康状态</small></div>
      <button v-if="batchJob && jobIsActive(batchJob)" class="outline-button resource-batch-import" type="button" :disabled="!batchJob.canCancel" @click="emit('cancelResource', batchJob.id)">{{ batchJob.canCancel ? '取消批量导入' : '正在完成导入…' }}</button>
      <button v-else-if="batchImportAvailable" class="outline-button resource-batch-import" type="button" :disabled="model.resourceTasksBusy || model.resourceLoading" @click="emit('resourceImportAll')">{{ model.resourceTasksBusy ? '资源任务处理中…' : model.resourceBatchRetry || batchJob?.state === 'failed' ? '重试批量导入' : '批量导入资源包' }}</button>
      <ManagedResourceProgress v-if="batchJob" class="managed-batch-progress" :job="batchJob" label="批量资源导入" />
    </div>
    <div v-if="model.resourceLoading && !model.rows.some(row => row.resource)" class="resource-empty" role="status">正在读取固定资源状态…</div>
    <div v-if="!model.rows.length" class="pool-empty">暂无本地组件信息。</div>
    <div v-else class="local-component-list">
      <article v-for="{ row, job } in componentCards" :key="row.id" class="local-component-card" :class="job ? jobState(job) : row.state" :data-component="row.id">
        <div class="local-component-summary" :class="{ 'resource-needed': needsResource(row) }">
          <div class="local-component-body">
            <div class="local-component-identity"><h4 :title="row.name">{{ row.name }}</h4><span class="local-state" :class="job ? jobState(job) : needsResource(row) ? 'failed' : row.state"><i aria-hidden="true"></i>{{ job?.state === 'failed' && row.resource?.updateAvailable ? '需更新' : job ? jobStateLabel(job) : needsResource(row) && row.resource ? row.resource.updateAvailable ? '需更新' : resourceStateLabel(row.resource.state) : row.status }}</span></div>
            <p v-if="row.state === 'loading' && !needsResource(row)" class="local-component-phase" aria-live="polite">{{ row.description }}</p>
            <dl class="local-component-metrics">
              <div v-if="row.runtime && row.runtime !== '未提供'" class="local-runtime-metric" data-metric="runtime"><dt>运行时</dt><dd>{{ row.runtime }}</dd></div>
              <div class="local-resource-metrics">
                <div v-if="row.residentMemory !== undefined || row.privateMemory !== undefined" data-metric="memory"><dt>{{ row.residentMemory !== undefined && row.privateMemory !== undefined ? '常驻内存／提交内存' : row.residentMemory !== undefined ? '常驻内存' : '提交内存' }}</dt><dd>{{ [row.residentMemory, row.privateMemory].filter(value => value !== undefined).join(' / ') }}</dd></div>
                <div v-if="row.vram !== undefined" data-metric="vram"><dt>显存</dt><dd>{{ row.vram }}</dd></div>
              </div>
            </dl>
            <p v-if="!job && needsResource(row) && row.resource" class="local-error" role="status">{{ row.resource.error || (row.resource.required === false ? '可选组件，缺失或待更新不阻塞启动。' : row.resource.updateAvailable ? '此组件需更新并通过校验后才能启动。' : '请下载或导入此组件。') }}</p>
            <p v-else-if="!job && row.error && !row.busy" class="local-error">{{ row.error }}</p>
          </div>
          <button v-if="job && jobIsActive(job)" class="outline-button local-resource-cancel" type="button" :disabled="!job.canCancel" :aria-label="`${row.name}：取消任务`" @click="emit('cancelResource', job.id)">{{ job.canCancel ? '取消任务' : '处理中…' }}</button>
          <div v-else-if="row.resource && needsResource(row) && model.resourceActionsAvailable" class="managed-resource-actions local-install-actions" :data-resource="row.resource.id" :aria-label="`${row.name}资源操作`">
            <button class="outline-button" type="button" :disabled="row.resource.busy || model.resourceLoading" :aria-label="`${row.name}：${row.resource.updateAvailable ? '更新' : '下载'}`" @click="emit('resourceDownload', row.resource.id)">{{ row.resource.busy ? '处理中…' : row.resource.updateAvailable ? row.resource.retryable || job?.state === 'failed' ? '重试更新' : '更新' : row.resource.retryable || job?.state === 'failed' ? '重试下载' : '下载' }}</button>
            <button class="outline-button" type="button" :disabled="row.resource.busy || model.resourceLoading" :aria-label="`${row.name}：导入`" @click="emit('resourceImport', row.resource.id)">{{ row.resource.busy ? '处理中…' : row.resource.updateAvailable ? '导入' : row.resource.retryable || job?.state === 'failed' ? '重试导入' : '导入' }}</button>
            <button class="outline-button" type="button" :disabled="model.resourceLoading" :aria-label="`${row.name}：跳转官方来源`" title="打开官方来源页面" @click="emit('openResourceSource', row.resource.id)">跳转</button>
          </div>
          <button v-else-if="!needsResource(row)" class="local-component-action" :class="{ 'retry-action': row.state === 'failed' }" type="button" :disabled="row.busy || row.state === 'loading' || (!canExpand(row) && !row.retryable)" :aria-label="`${row.name}：${row.state === 'loading' ? row.status : canExpand(row) ? expanded.has(row.id) ? '收起详情' : '详情' : row.state === 'disabled' ? '已停用' : '重试'}`" :aria-expanded="canExpand(row) ? expanded.has(row.id) : undefined" :aria-controls="canExpand(row) ? `local-detail-${row.id}` : undefined" @click="activate(row)">
            {{ row.state === 'loading' ? row.status : canExpand(row) ? expanded.has(row.id) ? '收起' : '详情' : row.state === 'disabled' ? '已停用' : '重试' }}
            <svg v-if="canExpand(row)" viewBox="0 0 24 24" aria-hidden="true" :class="{ expanded: expanded.has(row.id) }"><path d="m6 9 6 6 6-6" /></svg>
          </button>
        </div>
        <ManagedResourceProgress v-if="job" class="local-resource-progress" :job="job" :label="row.name" />
        <section v-if="row.resource && !needsResource(row) && expanded.has(row.id)" class="local-managed-resource" :data-resource="row.resource.id" :aria-label="`${row.resource.label}安装状态`">
          <div class="local-managed-resource-heading"><span>{{ row.resource.label }}</span><span class="resource-install-state" :class="row.resource.state">{{ row.resource.state === 'installed' ? '✓' : '!' }} {{ row.resource.status || resourceStateLabel(row.resource.state) }}</span></div>
          <dl v-if="row.resource.installedVersion || row.resource.targetVersion" class="local-resource-versions"><div v-if="row.resource.installedVersion"><dt>已安装版本</dt><dd>{{ row.resource.installedVersion }}</dd></div><div v-if="row.resource.targetVersion"><dt>目标版本</dt><dd>{{ row.resource.targetVersion }}</dd></div></dl>
          <p v-if="row.resource.error" class="local-error" role="status">{{ row.resource.error }}</p>
        </section>
        <div v-if="canExpand(row) && expanded.has(row.id)" :id="`local-detail-${row.id}`" class="local-component-detail">
          <p>{{ row.description }}</p>
          <dl v-if="row.facts.length" class="local-detail-facts"><div v-for="fact in row.facts" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ fact.value }}</dd></div></dl>
          <div v-if="row.device" class="local-device-editor">
            <label :for="`local-device-${row.id}`"><span>运行位置</span><select :id="`local-device-${row.id}`" :value="row.device.value" :disabled="saving || row.device.locked" @change="changeDevice(row.id, $event)"><option v-for="option in row.device.options" :key="option.value" :value="option.value" :disabled="option.disabled">{{ option.label }}</option></select></label>
            <button class="save-button" type="button" :disabled="saving || row.device.locked || invalidDevice(row)" @click="emit('save', row.id)">{{ saving ? '保存中' : '保存' }}</button>
            <small>实际运行：{{ row.device.current }}<span v-if="row.device.draft"> · 草稿尚未保存</span><span v-if="row.device.pending"> · 已保存配置下次启动生效</span></small>
          </div>
          <div v-else class="local-location"><span>运行位置</span><strong>{{ row.location || '本机' }}</strong></div>
          <small v-if="row.device?.hint || row.note" class="local-detail-note">{{ [row.device?.hint, row.note].filter(Boolean).join(' ') }}</small>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.local-component-phase{margin:6px 0 0;color:var(--muted);font-size:var(--font-caption);line-height:1.6}
.local-components{display:flex;flex-direction:column;gap:20px;min-width:0;container-type:inline-size}
.local-managed-heading>div{display:grid;gap:4px;min-width:0}
.local-managed-heading>div>small{color:var(--muted)}
.resource-batch-import{white-space:nowrap}
.resource-empty{padding:14px 16px;border:1px dashed var(--line-strong);border-radius:12px;color:var(--muted);font-size:var(--font-body)}
.local-managed-resource{display:grid;gap:8px;padding:10px 16px;border-top:1px solid var(--line);background:#00000008}
.local-managed-resource-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0;font-size:var(--font-caption);color:var(--muted)}
.local-managed-resource-heading>span:first-child{min-width:0;overflow-wrap:anywhere}
.local-resource-versions{display:flex;gap:18px;flex-wrap:wrap}.local-resource-versions>div{display:flex;gap:6px;align-items:baseline}.local-resource-versions dd{font-size:var(--font-caption)}
.resource-install-state{flex:none;display:inline-flex;align-items:center;gap:4px;font-size:var(--font-caption);color:var(--muted);white-space:nowrap}
.resource-install-state.installed{color:var(--mint)}
.resource-install-state.missing,.resource-install-state.incomplete{color:var(--accent)}
.managed-resource-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px}
.managed-resource-actions button{min-height:var(--compact-control-height);padding:5px 10px;font-size:var(--font-caption)}
.managed-batch-progress{flex-basis:100%}
.local-resource-progress{padding:0 16px 14px}
.local-resource-cancel{min-height:var(--compact-control-height);padding:5px 10px;font-size:var(--font-caption);white-space:nowrap}
.local-refresh{display:inline-flex;align-items:center;gap:6px;flex-shrink:0;min-height:var(--compact-control-height);padding:6px 10px;font-size:var(--font-body)}
svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.local-overview{display:grid;gap:12px}
.local-overview-line,.local-list-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
small,dt{font-size:var(--font-caption)}
.local-state{display:inline-flex;align-items:center;gap:5px;font-size:var(--font-caption);color:var(--muted);white-space:nowrap}
.local-state i{width:5px;height:5px;flex-shrink:0;border-radius:50%;background:currentColor}
.local-state.ready{color:var(--mint)}
.local-state.loading{color:var(--accent)}
.local-state.failed{color:var(--danger)}
.local-memory-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.local-memory-summary article{display:flex;flex-direction:column;gap:10px;padding:14px 16px;border:1px solid var(--line-strong);border-radius:12px;background:#ffffff02;min-width:0}
.local-memory-summary article>span{font-size:var(--font-caption);color:var(--muted);line-height:1.5}
.local-resource-value{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;font-variant-numeric:tabular-nums}
.local-resource-value strong{font-size:18px;line-height:1.3;white-space:nowrap}
.local-resource-total{font-size:var(--font-body);color:var(--muted);white-space:nowrap}
.local-list-heading h3{font-size:var(--font-section)}
.local-component-list{display:grid;gap:10px}
.local-component-card{min-width:0;border:1px solid var(--line);border-radius:12px;background:#ffffff02;overflow:hidden}
.local-component-card.failed{border-color:color-mix(in srgb,var(--danger) 28%,transparent)}
/* action column（操作列）：按钮文案或箭头变化不挤动名称与状态列。 */
.local-component-summary{--local-action-width:64px;display:grid;grid-template-columns:minmax(0,1fr) var(--local-action-width);gap:12px;align-items:center;padding:12px 16px}
.local-component-summary.resource-needed{grid-template-columns:minmax(0,1fr) auto}
.local-install-actions{margin-top:0;max-width:240px;justify-content:flex-end}
.local-component-body{min-width:0}
/* name column（名称列）：统一预留宽度，使状态不随名称长度移动。 */
.local-component-identity{display:flex;justify-content:space-between;gap:12px;align-items:start;margin-bottom:8px}
.local-component-identity h4{font-size:var(--font-body);line-height:1.5;overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.local-component-identity>.local-state{line-height:18px;flex:none;justify-content:flex-end;text-align:right}
dl,dd{margin:0}
.local-component-metrics{display:grid;grid-template-columns:minmax(100px,1fr) auto;align-items:start;gap:8px 16px}
.local-runtime-metric,.local-resource-metrics>div{min-width:0;display:flex;flex-direction:column;gap:3px}
.local-resource-metrics{display:flex;justify-content:flex-end;gap:16px;text-align:right;min-width:0}
.local-resource-metrics dd{white-space:nowrap}
.local-component-metrics dt{white-space:nowrap}
.local-detail-facts>div{min-width:0;display:flex;flex-direction:column;gap:4px}
dt{color:var(--subtle)}
dd{font-size:var(--font-body);font-variant-numeric:tabular-nums;overflow-wrap:anywhere;line-height:1.5}
.local-component-action{display:inline-flex;align-self:center;align-items:center;justify-content:center;gap:4px;min-height:var(--compact-control-height);width:100%;min-width:0;padding:6px 10px;font-size:var(--font-body);background:#ffffff03}
.local-component-action svg{transition:transform 180ms}
.local-component-action svg.expanded{transform:rotate(180deg)}
.retry-action{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 30%,transparent)}
.local-error{color:var(--danger);font-size:var(--font-caption);overflow-wrap:anywhere;white-space:pre-wrap}
.local-component-body>.local-error{margin-top:10px}
.local-component-detail{display:grid;gap:14px;padding:16px;border-top:1px solid var(--line);background:#0000000b}
.local-component-detail>p{color:var(--muted);font-size:var(--font-body);overflow-wrap:anywhere}
.local-detail-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px}
.local-device-editor{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;align-items:end;max-width:420px}
.local-device-editor label{display:grid;gap:6px;min-width:0}
.local-device-editor label>span,.local-location>span{font-size:var(--font-caption);color:var(--muted)}
.local-device-editor select{font-size:var(--font-body);min-height:var(--compact-control-height);height:var(--compact-control-height);padding:5px 10px}
.local-device-editor button{min-height:var(--compact-control-height);height:var(--compact-control-height);padding:5px 14px;font-size:var(--font-body)}
.local-device-editor>small{grid-column:1/-1}
.local-detail-note{overflow-wrap:anywhere}
.local-location{display:flex;gap:12px;align-items:center;font-size:var(--font-body)}
@container (max-width:440px){.local-memory-summary{grid-template-columns:1fr;gap:8px}.local-memory-summary article{flex-direction:row;align-items:center;justify-content:space-between;padding:12px;gap:8px}.local-component-summary{--local-action-width:58px;padding:12px;gap:8px}.local-component-metrics{gap:6px}.local-component-detail{padding:12px}.local-component-action{padding-inline:8px}.local-resource-progress{padding:0 12px 12px}.local-managed-resource{padding:10px 12px}.managed-resource-actions{gap:6px}}
@container (max-width:560px){.local-managed-heading{align-items:flex-start}.resource-batch-import{white-space:normal;max-width:100%}}
@container (max-width:520px){.local-component-metrics{grid-template-columns:1fr}.local-resource-metrics{gap:12px}.local-resource-metrics dt,.local-resource-metrics dd{font-size:var(--font-caption)}}
@media (prefers-reduced-motion:reduce){.local-component-action svg{transition:none}}
@container (max-width:440px){.local-component-summary.resource-needed{grid-template-columns:minmax(0,1fr)}.local-install-actions{justify-content:flex-start;max-width:100%;gap:6px}}
</style>

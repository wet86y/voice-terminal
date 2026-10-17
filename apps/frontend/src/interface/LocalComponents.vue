<script setup lang="ts">
import { computed, ref } from 'vue'
import SettingsPageHeader from './SettingsPageHeader.vue'
import ManagedResourceProgress from './ManagedResourceProgress.vue'
import FixedProviderSettings from './FixedProviderSettings.vue'
import type { LocalComponentCategory, LocalComponentRow, LocalComponentsModel, LocalDownloadItem, ManagedResourceJob, ManagedResourceRow } from './local-components'

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
  /** resourceBundleDownload（分类批量下载）：先读取清单，确认后并行启动适用资源。 */
  resourceBundleDownload: [request: { title: string; entries: LocalDownloadItem[] }]
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
const expandedCategories = ref(new Set<LocalComponentCategory>()) // expandedCategories（已展开用途分类）。
const expandedComponents = ref(new Set<string>()) // expandedComponents（已展开组件详情）。
const categoryDefinitions: Array<{ id: LocalComponentCategory; title: string; description: string }> = [
  { id: 'speech', title: '语音识别', description: '语音转写、语音轮次判断与流式标点' },
  { id: 'tts', title: '语音合成', description: '本地 VoxCPM 模型或 MiniMax 在线 API（程序接口）' },
  { id: 'inference', title: '推理执行', description: 'DeepSeek 在线推理服务与 DSH 执行组件' },
  { id: 'extensions', title: '拓展组件', description: '唤醒词、声纹、语音检测与热词修订' },
  { id: 'voice-server', title: 'Voice Server（语音服务）', description: '独立管理桌面语音运行环境' },
]
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
  return { row, job: job && jobIsActive(job) ? job : undefined, category: rowCategory(row) }
}))
const batchJob = computed(() => visibleJob(latestJobs.value.get('batch')))
const categories = computed(() => categoryDefinitions.map(definition => {
  const rows = componentCards.value.filter(card => card.category === definition.id)
  const entries = rows.flatMap(({ row }) => downloadEntries(row))
  const pendingCount = entries.filter(entryNeedsDownload).length
  return { ...definition, rows, entries, pendingCount }
}))
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
  return Boolean((row.resource && (row.resource.state !== 'installed' || row.resource.updateAvailable))
    || row.resourceChoices?.some(resource => resource.state !== 'installed' || resource.updateAvailable))
}
function resourceRetryable(resource: ManagedResourceRow) {
  return Boolean(resource.retryable || latestJobs.value.get(resource.id)?.state === 'failed')
}
function rowCategory(row: LocalComponentRow): LocalComponentCategory {
  if (row.category) return row.category
  if (row.id === 'voice-system' || row.id === 'voice-server') return 'voice-server'
  if (row.id === 'tts') return 'tts'
  if (row.id === 'dsh' || row.id === 'deepseek') return 'inference'
  if (['paraformer', 'sensevoice', 'smart_turn', 'punctuation', 'streaming-punctuation', 'paraformer-streaming', 'sensevoice-small'].includes(row.id)) return 'speech'
  return 'extensions'
}
function rowResource(row: LocalComponentRow, fallback: ManagedResourceRow): ManagedResourceRow {
  return props.model.downloadResources?.find(resource => resource.id === fallback.id) ?? fallback
}
function downloadEntries(row: LocalComponentRow): LocalDownloadItem[] {
  const entries: LocalDownloadItem[] = []
  if (row.resource) entries.push({ ...rowResource(row, row.resource), optional: false })
  const sharedRuntimeMissing = Boolean(row.sharedResource && (row.sharedResource.state !== 'installed' || row.sharedResource.updateAvailable))
  for (const resource of row.resourceChoices ?? []) entries.push({ ...rowResource(row, resource), optional: true, forcePlan: sharedRuntimeMissing })
  if (row.sharedResource) {
    const shared = rowResource(row, row.sharedResource)
    entries.push({
      label: `${shared.label}（共享依赖）`,
      state: shared.state,
      status: shared.updateAvailable ? '需更新' : shared.state === 'installed' ? '已就绪' : '选择本地模型后准备',
      updateAvailable: shared.updateAvailable,
      busy: shared.busy,
      note: '会随所选 VoxCPM 本地模型统一检查、安装并复用。',
    })
  }
  if (row.provider) {
    const label = row.provider.provider === 'deepseek' ? 'DeepSeek 在线 API（推理接口）' : 'MiniMax 在线 API（语音接口）'
    entries.push({
      label,
      status: row.provider.configured ? '已配置' : '待配置',
      note: '在线配置项，不产生下载文件。',
    })
  } else if (!row.resource && !row.resourceChoices?.length) {
    entries.push({ label: row.name, status: '内置能力', note: '随语音服务提供，无独立下载文件。' })
  }
  return entries
}
function entryNeedsDownload(entry: LocalDownloadItem) {
  return Boolean(entry.id && !entry.busy && (entry.forcePlan || entry.state !== 'installed' || entry.updateAvailable))
}
function toggleCategory(category: LocalComponentCategory) {
  const next = new Set(expandedCategories.value)
  if (next.has(category)) next.delete(category)
  else {
    next.add(category)
    if (props.model.resourceActionsAvailable) emit('resourceRefresh')
  }
  expandedCategories.value = next
}
function toggleComponent(row: LocalComponentRow) {
  const next = new Set(expandedComponents.value)
  if (next.has(row.id)) next.delete(row.id)
  else {
    next.add(row.id)
    if ((row.resource || row.resourceChoices) && props.model.resourceActionsAvailable) emit('resourceRefresh')
  }
  expandedComponents.value = next
}
function downloadCategory(category: { title: string; entries: LocalDownloadItem[]; pendingCount: number }) {
  if (!category.entries.length || !props.model.resourceActionsAvailable || props.model.resourceLoading) return
  emit('resourceBundleDownload', { title: category.title, entries: category.entries })
}
function rowState(row: LocalComponentRow) {
  if (row.resource?.updateAvailable && needsResource(row)) return 'failed'
  if (row.state === 'loading' || row.state === 'failed' && (row.error || row.retryable)) return row.state
  if (row.resource && needsResource(row)) return 'disabled'
  return row.state
}
function rowStatus(row: LocalComponentRow) {
  if (row.resource?.updateAvailable && needsResource(row)) return '需更新'
  if (row.state === 'loading' || row.state === 'failed' && (row.error || row.retryable)) return row.status
  if (row.resource && needsResource(row)) return resourceStateLabel(row.resource.state)
  return row.status
}
function changeDevice(id: string, event: Event) {
  const value = (event.target as HTMLSelectElement).value
  const option = props.model.rows.find(row => row.id === id)?.device?.options.find(option => option.value === value)
  if (option && !option.disabled) emit('device', id, value)
}
function invalidDevice(row: LocalComponentRow) {
  return row.device?.options.find(option => option.value === row.device?.value)?.disabled
}
function selectedResources(row: LocalComponentRow) {
  return row.resourceChoices ?? []
}
</script>

<template>
  <section class="local-components" aria-labelledby="local-components-title">
    <SettingsPageHeader section="compute" title-id="local-components-title">
      <template #actions>
        <button class="outline-button local-refresh" type="button" :disabled="model.refreshing" @click="emit('refresh')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 9a8 8 0 1 0 0 6" /><path d="M20 4.5V9h-4.5" /></svg>
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
      <div><h3>按用途分类</h3><small>{{ model.rows.length }} 个组件；展开分类查看组件，再展开详情配置</small></div>
      <button v-if="batchJob && jobIsActive(batchJob)" class="outline-button resource-batch-import" type="button" :disabled="!batchJob.canCancel" @click="emit('cancelResource', batchJob.id)">{{ batchJob.canCancel ? '取消批量导入' : '正在完成导入…' }}</button>
      <button v-else-if="batchImportAvailable" class="outline-button resource-batch-import" type="button" :disabled="model.resourceTasksBusy || model.resourceLoading" @click="emit('resourceImportAll')">{{ model.resourceTasksBusy ? '资源任务处理中…' : model.resourceBatchRetry || batchJob?.state === 'failed' ? '重试批量导入' : '批量导入资源包' }}</button>
      <ManagedResourceProgress v-if="batchJob" class="managed-batch-progress" :job="batchJob" label="批量资源导入" />
    </div>
    <div v-if="model.resourceLoading && !model.rows.some(row => row.resource)" class="resource-empty" role="status">正在读取固定资源状态…</div>
    <div class="local-category-list">
      <section v-for="category in categories" :key="category.id" class="local-category-card" :class="{ expanded: expandedCategories.has(category.id) }" :data-category="category.id">
        <header class="local-category-heading">
          <button class="local-category-toggle" type="button" :aria-expanded="expandedCategories.has(category.id)" :aria-controls="`local-category-${category.id}`" @click="toggleCategory(category.id)">
            <span><strong>{{ category.title }}</strong><small>{{ category.description }} · {{ category.rows.length }} 个组件</small></span>
            <svg viewBox="0 0 24 24" aria-hidden="true" :class="{ expanded: expandedCategories.has(category.id) }"><path d="m6 9 6 6 6-6" /></svg>
          </button>
          <button v-if="model.resourceActionsAvailable" class="outline-button category-download" type="button" :disabled="model.resourceLoading" @click="downloadCategory(category)">
            {{ category.pendingCount ? category.id === 'voice-server' ? '配置环境' : category.id === 'tts' ? `选择本地模型（${category.pendingCount}）` : `下载组件（${category.pendingCount}）` : '查看下载清单' }}
          </button>
        </header>
        <div v-if="expandedCategories.has(category.id)" :id="`local-category-${category.id}`" class="local-category-content">
          <p v-if="!category.rows.length" class="resource-empty">暂无该分类的组件状态。</p>
          <div v-else class="local-component-list">
            <article v-for="{ row, job } in category.rows" :key="row.id" class="local-component-card" :class="job ? jobState(job) : rowState(row)" :data-component="row.id">
              <div class="local-component-summary">
                <div class="local-component-body">
                  <div class="local-component-identity"><h4 :title="row.name">{{ row.name }}</h4><span class="local-state" :class="job ? jobState(job) : rowState(row)"><i aria-hidden="true"></i>{{ job ? jobStateLabel(job) : rowStatus(row) }}</span></div>
                  <p v-if="row.state === 'loading'" class="local-component-phase" aria-live="polite">{{ row.description }}</p>
                </div>
                <button class="local-component-action" type="button" :aria-expanded="expandedComponents.has(row.id)" :aria-controls="`local-detail-${row.id}`" @click="toggleComponent(row)">
                  {{ expandedComponents.has(row.id) ? '收起详情' : '详情' }}
                  <svg viewBox="0 0 24 24" aria-hidden="true" :class="{ expanded: expandedComponents.has(row.id) }"><path d="m6 9 6 6 6-6" /></svg>
                </button>
              </div>
              <div v-if="expandedComponents.has(row.id)" :id="`local-detail-${row.id}`" class="local-component-detail">
                <p>{{ row.description }}</p>
                <section v-if="row.resource" class="local-managed-resource" :data-resource="row.resource.id" :aria-label="`${row.resource.label}安装状态`">
                  <div class="local-managed-resource-heading"><span>{{ row.resource.label }}</span><span class="resource-install-state" :class="row.resource.state">{{ row.resource.status || resourceStateLabel(row.resource.state) }}</span></div>
                  <dl v-if="row.resource.installedVersion || row.resource.targetVersion" class="local-resource-versions"><div v-if="row.resource.installedVersion"><dt>已安装版本</dt><dd>{{ row.resource.installedVersion }}</dd></div><div v-if="row.resource.targetVersion"><dt>目标版本</dt><dd>{{ row.resource.targetVersion }}</dd></div></dl>
                  <p v-if="row.resource.error && row.state !== 'loading' && !jobIsActive(latestJobs.get(row.resource.id))" class="local-error" role="status">{{ row.resource.error }}</p>
                  <ManagedResourceProgress v-if="visibleJob(latestJobs.get(row.resource.id))" :job="visibleJob(latestJobs.get(row.resource.id))!" :label="row.resource.label" />
                  <button v-if="visibleJob(latestJobs.get(row.resource.id)) && jobIsActive(latestJobs.get(row.resource.id))" class="outline-button local-resource-cancel" type="button" :aria-label="`${row.resource.label}：取消任务`" :disabled="!visibleJob(latestJobs.get(row.resource.id))?.canCancel" @click="emit('cancelResource', visibleJob(latestJobs.get(row.resource.id))!.id)">取消任务</button>
                  <div v-else-if="needsResource(row) && model.resourceActionsAvailable" class="managed-resource-actions">
                      <button class="outline-button" type="button" :aria-label="`${row.resource.label}：${row.resource.updateAvailable ? '更新' : '下载'}`" :disabled="row.resource.busy || model.resourceLoading" @click="emit('resourceDownload', row.resource.id)">{{ row.resource.busy ? '处理中…' : row.resource.updateAvailable ? resourceRetryable(row.resource) ? '重试更新' : '更新' : resourceRetryable(row.resource) ? '重试下载' : '下载' }}</button>
                      <button class="outline-button" type="button" :aria-label="`${row.resource.label}：导入`" :disabled="row.resource.busy || model.resourceLoading" @click="emit('resourceImport', row.resource.id)">导入</button>
                      <button class="outline-button" type="button" :aria-label="`${row.resource.label}：跳转官方来源`" :disabled="model.resourceLoading" @click="emit('openResourceSource', row.resource.id)">来源说明</button>
                  </div>
                </section>
                <section v-if="row.resourceChoices?.length" class="local-tts-downloads">
                  <h5>本地 TTS（语音合成）模型</h5>
                  <small>在分类下载清单中可选择 VoxCPM1、VoxCPM2 或两者；所选模型会自动准备共用运行程序。下载不会改变当前播放后端。</small>
                  <article v-for="resource in selectedResources(row)" :key="resource.id" :data-resource="resource.id" class="local-download-choice">
                    <div class="local-managed-resource-heading"><span>{{ resource.label }}</span><span class="resource-install-state" :class="resource.state">{{ resource.updateAvailable ? '需更新' : resource.status || resourceStateLabel(resource.state) }}</span></div>
                    <dl v-if="resource.installedVersion || resource.targetVersion" class="local-resource-versions"><div v-if="resource.installedVersion"><dt>已安装版本</dt><dd>{{ resource.installedVersion }}</dd></div><div v-if="resource.targetVersion"><dt>目标版本</dt><dd>{{ resource.targetVersion }}</dd></div></dl>
                    <p v-if="resource.error && row.state !== 'loading' && !jobIsActive(latestJobs.get(resource.id))" class="local-error">{{ resource.error }}</p>
                    <ManagedResourceProgress v-if="visibleJob(latestJobs.get(resource.id))" :job="visibleJob(latestJobs.get(resource.id))!" :label="resource.label" />
                    <button v-if="visibleJob(latestJobs.get(resource.id)) && jobIsActive(latestJobs.get(resource.id))" class="outline-button local-resource-cancel" type="button" :disabled="!visibleJob(latestJobs.get(resource.id))?.canCancel" @click="emit('cancelResource', visibleJob(latestJobs.get(resource.id))!.id)">取消任务</button>
                    <div v-else-if="model.resourceActionsAvailable" class="managed-resource-actions">
                      <button type="button" class="outline-button" :aria-label="`${resource.label}：${resource.updateAvailable ? '更新' : '下载'}`" :disabled="resource.busy || model.resourceLoading" @click="emit('resourceDownload', resource.id)">{{ resource.busy ? '处理中…' : resource.updateAvailable ? resourceRetryable(resource) ? '重试更新' : '更新' : resourceRetryable(resource) ? '重试下载' : resource.state === 'installed' ? '校验并补齐' : '下载' }}</button>
                      <button type="button" class="outline-button" :disabled="resource.busy || model.resourceLoading" @click="emit('resourceImport', resource.id)">导入</button>
                      <button type="button" class="outline-button" @click="emit('openResourceSource', resource.id)">来源说明</button>
                    </div>
                  </article>
                  <div v-if="row.sharedResource" class="local-managed-resource-heading"><span>{{ row.sharedResource.label }}（共享运行程序）</span><span class="resource-install-state" :class="row.sharedResource.state">{{ row.sharedResource.status }}</span></div>
                </section>
                <FixedProviderSettings v-if="row.provider" :model="row.provider" />
                <p v-if="!job && row.resource && needsResource(row) && !row.resource.error" class="local-resource-hint" role="status">{{ row.resource.required === false ? '可选组件；缺失或待更新不阻塞启动。' : '资源缺失或需要更新，可通过本分类下载清单统一处理。' }}</p>
                <p v-if="row.error && !row.busy" class="local-error" role="alert">{{ row.error }}</p>
                <button v-if="row.retryable && !row.busy" class="outline-button retry-action" type="button" @click="emit('retry', row.id)">重试组件</button>
                <dl v-if="row.facts.length" class="local-detail-facts"><div v-for="fact in row.facts" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ fact.value }}</dd></div></dl>
                <dl v-if="row.runtime && row.runtime !== '未提供' || row.residentMemory !== undefined || row.privateMemory !== undefined || row.vram !== undefined" class="local-component-metrics local-detail-metrics">
                  <div v-if="row.runtime && row.runtime !== '未提供'" class="local-runtime-metric" data-metric="runtime"><dt>运行时</dt><dd>{{ row.runtime }}</dd></div>
                  <div v-if="row.residentMemory !== undefined || row.privateMemory !== undefined" class="local-runtime-metric" data-metric="memory"><dt>{{ row.residentMemory !== undefined && row.privateMemory !== undefined ? '常驻内存／提交内存' : row.residentMemory !== undefined ? '常驻内存' : '提交内存' }}</dt><dd>{{ [row.residentMemory, row.privateMemory].filter(value => value !== undefined).join(' / ') }}</dd></div>
                  <div v-if="row.vram !== undefined" class="local-runtime-metric" data-metric="vram"><dt>显存</dt><dd>{{ row.vram }}</dd></div>
                </dl>
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
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.local-tts-downloads{display:grid;gap:12px;width:100%;min-width:0}.local-tts-downloads h5{font-size:var(--font-body)}.local-download-choice{display:grid;gap:8px;padding:12px;border:1px solid var(--line);border-radius:12px}
.local-component-phase{margin:6px 0 0;color:var(--muted);font-size:var(--font-caption);line-height:1.6}
.local-components{display:flex;flex-direction:column;gap:20px;min-width:0;container-type:inline-size}
.local-managed-heading>div{display:grid;gap:4px;min-width:0}
.local-managed-heading>div>small{color:var(--muted)}
.resource-batch-import{white-space:nowrap}
.resource-empty{padding:14px 16px;border:1px dashed var(--line-strong);border-radius:12px;color:var(--muted);font-size:var(--font-body)}
.local-category-list{display:grid;gap:12px}.local-category-card{min-width:0;border:1px solid var(--line-strong);border-radius:16px;background:#ffffff02;overflow:hidden}.local-category-card.expanded{background:#ffffff04}.local-category-heading{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;padding:12px 16px}.local-category-toggle{display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0;padding:3px 0;text-align:left;background:transparent;border:0;cursor:pointer}.local-category-toggle>span{display:grid;gap:4px;min-width:0}.local-category-toggle strong{font-size:var(--font-section);line-height:1.4}.local-category-toggle small{color:var(--muted);line-height:1.5}.local-category-toggle svg{flex:none;width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;transition:transform 180ms}.local-category-toggle svg.expanded{transform:rotate(180deg)}.category-download{white-space:nowrap;min-height:var(--compact-control-height);padding:6px 11px;font-size:var(--font-caption)}.local-category-content{display:grid;gap:10px;padding:0 14px 14px}.local-category-content>.resource-empty{margin-top:2px}
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
.local-refresh{display:inline-flex;align-items:center;gap:7px;flex-shrink:0;min-height:var(--compact-control-height);padding:6px 10px;font-size:var(--font-body)}
.local-refresh svg{width:16px;height:16px;stroke-width:1.9}
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
.local-component-list{display:grid;gap:8px}
.local-component-card{min-width:0;border:1px solid var(--line);border-radius:12px;background:#ffffff02;overflow:hidden}
.local-component-card.failed{border-color:color-mix(in srgb,var(--danger) 28%,transparent)}
/* action column（操作列）：按钮文案或箭头变化不挤动名称与状态列。 */
.local-component-summary{display:grid;grid-template-columns:minmax(0,1fr) 84px;gap:12px;align-items:center;padding:11px 14px}
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
.local-detail-metrics{grid-template-columns:repeat(auto-fit,minmax(120px,1fr));padding-top:4px}.local-detail-metrics .local-runtime-metric{display:flex;flex-direction:column;gap:3px;min-width:0}.local-resource-hint{font-size:var(--font-caption);color:var(--muted);line-height:1.6}
.local-device-editor{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;align-items:end;max-width:420px}
.local-device-editor label{display:grid;gap:6px;min-width:0}
.local-device-editor label>span,.local-location>span{font-size:var(--font-caption);color:var(--muted)}
.local-device-editor select{font-size:var(--font-body);min-height:var(--compact-control-height);height:var(--compact-control-height);padding:5px 10px}
.local-device-editor button{min-height:var(--compact-control-height);height:var(--compact-control-height);padding:5px 14px;font-size:var(--font-body)}
.local-device-editor>small{grid-column:1/-1}
.local-detail-note{overflow-wrap:anywhere}
.local-location{display:flex;gap:12px;align-items:center;font-size:var(--font-body)}
@container (max-width:440px){.local-memory-summary{grid-template-columns:1fr;gap:8px}.local-memory-summary article{flex-direction:row;align-items:center;justify-content:space-between;padding:12px;gap:8px}.local-component-summary{grid-template-columns:minmax(0,1fr) 72px;padding:10px;gap:8px}.local-category-heading{grid-template-columns:minmax(0,1fr);padding:11px 12px}.category-download{justify-self:stretch}.local-category-content{padding:0 10px 10px}.local-component-metrics{gap:6px}.local-component-detail{padding:12px}.local-component-action{padding-inline:8px}.local-resource-progress{padding:0 12px 12px}.local-managed-resource{padding:10px 12px}.managed-resource-actions{gap:6px}}
@container (max-width:560px){.local-managed-heading{align-items:flex-start}.resource-batch-import{white-space:normal;max-width:100%}}
@container (max-width:520px){.local-component-metrics{grid-template-columns:1fr}.local-resource-metrics{gap:12px}.local-resource-metrics dt,.local-resource-metrics dd{font-size:var(--font-caption)}}
@media (prefers-reduced-motion:reduce){.local-component-action svg{transition:none}}
@media (prefers-reduced-motion:reduce){.local-category-toggle svg{transition:none}}
</style>

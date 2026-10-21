<script setup lang="ts">
import { computed, ref } from 'vue'
import SettingsPageHeader from './SettingsPageHeader.vue'
import ManagedResourceProgress from './ManagedResourceProgress.vue'
import FixedProviderSettings from './FixedProviderSettings.vue'
import FixedProviderUsage from './FixedProviderUsage.vue'
import { formatComponentMemory, type LocalComponentCategory, type LocalComponentRow, type LocalComponentsModel, type LocalDownloadItem, type ManagedResourceJob, type ManagedResourceRow } from './local-components'

const props = defineProps<{
  /** model（页面状态与指标）与 saving（保存中）。 */
  model: LocalComponentsModel
  saving?: boolean
}>()
const visibleResources = computed(() => props.model.resources.filter(resource => resource.used !== undefined))
// batchImportAvailable（批量导入入口）：有任一缺失资源时展示，含不阻塞启动的可选运行依赖。
const batchImportAvailable = computed(() => props.model.resourceActionsAvailable && props.model.rows.some(hasPendingResource))
const emit = defineEmits<{
  /** refresh（刷新指标）与 retry（重试指定组件）。 */
  refresh: []
  retry: [id: string]
  /** resourceDownload/resourceImport（下载或导入资源）、openResourceSource（打开上游）、cancelResource（取消任务）。 */
  resourceDownload: [id: string]
  resourceDownloadRequired: [] // resourceDownloadRequired（一键必要组件）。
  ttsSelection: [choice: string] // ttsSelection（显式播放目标）。
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
const providerUsageRevision = ref(0) // providerUsageRevision（密钥更新后刷新在线服务额度摘要）。
const categoryDefinitions: Array<{ id: LocalComponentCategory; title: string; description: string }> = [
  { id: 'speech', title: '语音识别', description: '语音活动检测、转写、轮次判断与流式标点' },
  { id: 'tts', title: '语音合成', description: '本地 VoxCPM 模型或 MiniMax 在线 API（程序接口）' },
  { id: 'inference', title: '推理执行', description: 'DeepSeek 在线推理服务与 DSH 执行组件' },
  { id: 'extensions', title: '拓展组件', description: '唤醒词、声纹、情绪背景、声学事件与热词修订' },
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
const componentCards = computed(() => props.model.rows.flatMap(expandRuntimeDependencies).map(row => {
  const job = rowJobs(row).find(jobIsActive)
  // 没有独立安装资源的进程条目仅展示活动任务，历史下载错误不覆盖进程重启状态。
  return { row, job: job && jobIsActive(job) ? job : undefined, category: rowCategory(row) }
}))
const batchJob = computed(() => visibleJob(latestJobs.value.get('required-batch')) ?? visibleJob(latestJobs.value.get('batch')))
const categories = computed(() => categoryDefinitions.map(definition => {
  const rows = componentCards.value.filter(card => card.category === definition.id)
  const entries = rows.flatMap(({ row }) => downloadEntries(row))
  const pendingEntries = entries.filter(entryNeedsDownload)
  // Voice Server（语音服务）分类顶层操作只针对阻塞启动的 CPU（中央处理器）依赖；
  // audio.cpp（本地语音合成引擎）和 GPU（图形处理器）依赖仍留在各自子卡片操作。
  const pendingCount = (definition.id === 'voice-server'
    ? pendingEntries.filter(entry => entry.optional !== true)
    : pendingEntries).length
  return {
    ...definition, rows, entries, pendingCount,
    observation: categoryObservation(rows.map(card => card.row)),
    progress: categoryProgress(definition.id, rows.map(card => card.row)),
  }
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
function expandRuntimeDependencies(row: LocalComponentRow): LocalComponentRow[] {
  const dependencies = row.runtimeDependencies
  // runtimeDependencies（运行依赖子卡片）仅展开到 Voice Server 分类内，不改变其他组件的分类归属。
  if (rowCategory(row) !== 'voice-server' || !dependencies?.length) return [row]
  const order = ['voice-runtime-cpu', 'tts-audiocpp-runtime', 'voice-runtime-gpu']
  return [...dependencies].sort((a, b) => {
    const aIndex = order.indexOf(a.id)
    const bIndex = order.indexOf(b.id)
    return (aIndex < 0 ? order.length : aIndex) - (bIndex < 0 ? order.length : bIndex)
  }).map((dependency, index): LocalComponentRow => {
    const isCpu = dependency.id === 'voice-runtime-cpu' || dependency.required !== false && index === 0
    const dependencyMetrics = row.runtimeDependencyMetrics?.[dependency.id]
    const installed = dependency.state === 'installed' && !dependency.updateAvailable
    const serviceBusy = isCpu && row.state === 'loading'
    const serviceFailed = isCpu && row.state === 'failed'
    const dependencyVram = typeof dependencyMetrics?.vram === 'number' && dependencyMetrics.vram > 0
      ? dependencyMetrics.vram : undefined
    return {
      ...row,
      id: isCpu ? row.id : dependency.id,
      name: dependency.label,
      category: 'voice-server',
      state: serviceBusy ? 'loading' : serviceFailed ? 'failed'
        : dependency.updateAvailable ? 'failed' : installed ? 'ready' : 'disabled',
      status: serviceBusy || serviceFailed ? row.status
        : dependency.updateAvailable ? '需更新' : dependency.status || resourceStateLabel(dependency.state),
      available: installed && !serviceBusy && !serviceFailed,
      busy: serviceBusy,
      retryable: isCpu && row.retryable || dependency.retryable,
      runtime: '',
      residentMemory: isCpu ? row.residentMemory : formatComponentMemory(dependencyMetrics?.resident),
      privateMemory: isCpu ? row.privateMemory : formatComponentMemory(dependencyMetrics?.privateCommit),
      vram: isCpu ? row.vram : formatComponentMemory(dependencyVram),
      resourceBytes: isCpu ? row.resourceBytes : dependencyMetrics ? {
        resident: dependencyMetrics.resident,
        privateCommit: dependencyMetrics.privateCommit,
        vram: dependencyVram,
      } : undefined,
      description: runtimeDescription(dependency.id),
      error: dependency.error || (serviceFailed ? row.error : undefined),
      resource: dependency,
      resourceChoices: undefined,
      sharedResource: undefined,
      runtimeDependencies: undefined,
      provider: undefined,
      location: row.location || '本机',
      facts: isCpu ? row.facts : [],
      note: dependency.required === false
        ? '可选运行依赖；未安装时不影响中央处理器语音环境。'
        : '中央处理器基础运行依赖；语音服务启动所必需。',
    }
  })
}
function resourceNeedsInstall(resource?: ManagedResourceRow) {
  return Boolean(resource && (resource.state !== 'installed' || resource.updateAvailable))
}
function needsResource(row: LocalComponentRow) {
  return Boolean(row.resource?.required !== false && resourceNeedsInstall(row.resource)
    || row.runtimeDependencies?.some(resource => resource.required !== false && resourceNeedsInstall(resource))
    || row.resourceChoices?.some(resource => resource.required !== false && resourceNeedsInstall(resource))
    || row.sharedResource?.required !== false && resourceNeedsInstall(row.sharedResource))
}
function hasPendingResource(row: LocalComponentRow) {
  return rowResources(row).some(resourceNeedsInstall)
}
function resourceRetryable(resource: ManagedResourceRow) {
  return Boolean(resource.retryable || resourceJob(resource.id)?.state === 'failed')
}
function resourceJob(id: string) { return latestJobs.value.get(id) ?? (id === 'voice-runtime-cpu' ? latestJobs.value.get('voice-server') : undefined) }
function runtimeDescription(id: string) {
  if (id === 'voice-runtime-cpu') return '必需依赖，提供基础语音运行能力。更新会保留已安装的显卡能力。'
  if (id === 'voice-runtime-gpu') return '可选依赖，为流式识别、整句精修和声纹提供显卡运行能力；复用现有模型。安装后刷新环境检查，再选择模型运行位置。'
  return '可选依赖，由本地 VoxCPM 语音合成模型共用；使用在线语音合成时无需安装。'
}
function runtimeNeedsAction(resource: ManagedResourceRow) { return resource.state !== 'installed' || resource.updateAvailable || resourceRetryable(resource) }
function rowCategory(row: LocalComponentRow): LocalComponentCategory {
  if (row.category) return row.category
  if (row.id === 'voice-system' || row.id === 'voice-server') return 'voice-server'
  if (row.id === 'tts') return 'tts'
  if (row.id === 'dsh' || row.id === 'deepseek') return 'inference'
  if (['paraformer', 'smart_turn', 'voice_vad', 'fsmn-vad', 'punctuation', 'streaming-punctuation', 'paraformer-streaming', 'paraformer-offline'].includes(row.id)) return 'speech'
  return 'extensions'
}
function rowResource(row: LocalComponentRow, fallback: ManagedResourceRow): ManagedResourceRow {
  return props.model.downloadResources?.find(resource => resource.id === fallback.id) ?? fallback
}
function downloadEntries(row: LocalComponentRow): LocalDownloadItem[] {
  if (row.runtimeDependencies?.length) return row.runtimeDependencies.map(resource => ({
    ...resource, optional: resource.required === false, note: runtimeDescription(resource.id),
  }))
  const entries: LocalDownloadItem[] = []
  if (row.resource) entries.push({ ...rowResource(row, row.resource), optional: row.resource.required === false })
  for (const resource of row.resourceChoices ?? []) entries.push({ ...rowResource(row, resource), optional: true })
  if (row.sharedResource) {
    const shared = rowResource(row, row.sharedResource)
    entries.push({
      label: `${shared.label}（共享依赖）`,
      state: shared.state,
      status: shared.updateAvailable ? '需更新' : shared.state === 'installed' ? '已就绪' : '在语音服务分类单独管理',
      updateAvailable: shared.updateAvailable,
      busy: shared.busy,
      note: '运行程序在语音服务分类单独下载；本模型下载只处理自身文件。',
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
function refreshProviderUsage() { providerUsageRevision.value += 1 }
function downloadCategory(category: { id: LocalComponentCategory; title: string; entries: LocalDownloadItem[]; pendingCount: number }) {
  if (!category.entries.length || !props.model.resourceActionsAvailable || props.model.resourceLoading) return
  const entries = category.id === 'voice-server'
    ? category.entries.filter(entry => entry.optional !== true && entryNeedsDownload(entry))
    : category.entries
  if (!entries.length) return
  emit('resourceBundleDownload', { title: category.title, entries })
}
function hasReadyTtsAlternative(row: LocalComponentRow) {
  return row.category === 'tts' && props.model.rows.some(candidate => candidate.id !== row.id
    && candidate.category === 'tts' && candidate.state === 'ready' && candidate.available)
}
function resourceVersionText(resource?: ManagedResourceRow) {
  if (!resource) return ''
  const installed = resource.installedVersion?.trim()
  const target = resource.targetVersion?.trim()
  if (installed && target && installed === target) return `当前版本 ${installed}`
  if (installed && target) return `已安装版本 ${installed} · 目标版本 ${target}`
  if (installed) return `当前版本 ${installed}`
  if (target) return `目标版本 ${target}`
  return ''
}
/** ResourceRuntimeFact（组件运行信息字段）：field label（字段名称）与对应展示值。 */
type ResourceRuntimeFact = {
  label: string // label（运行信息字段的中文名称）。
  value: string // value（对应版本或运行指标的展示值）。
}
/** resourceRuntimeFacts（运行信息字段）：摘要已显示运行时，这里只列版本、位置与进程指标。 */
function resourceRuntimeFacts(resource?: ManagedResourceRow, row?: LocalComponentRow): ResourceRuntimeFact[] {
  const version = resourceVersionText(resource)
  return [
    ...(version ? [{ label: '版本', value: version }] : []),
    ...((row?.device?.current || !row?.device && row?.location)
      ? [{ label: '运行位置', value: row.device?.current || row.location! }] : []),
    ...(row?.facts ?? [])
      .filter(fact => fact.label === '进程标识' || fact.label === '处理器占用')
      .map(fact => ({ label: fact.label, value: fact.value })),
  ]
}
function hasComponentMemory(row: LocalComponentRow) {
  return row.residentMemory !== undefined || row.privateMemory !== undefined || row.vram !== undefined
}
function componentMemoryLabel(row: LocalComponentRow) {
  const labels = [
    ...(row.residentMemory !== undefined ? ['常驻内存'] : []),
    ...(row.privateMemory !== undefined ? ['提交内存'] : []),
    ...(row.vram !== undefined ? ['显存'] : []),
  ]
  return labels.join('／')
}
function componentMemoryValue(row: LocalComponentRow) {
  return [row.residentMemory, row.privateMemory, row.vram]
    .filter((value): value is string => value !== undefined).join(' / ')
}
function detailFacts(row: LocalComponentRow) {
  const mergedIntoRuntimeLine = Boolean(row.resource || row.runtimeDependencies?.length)
  return row.facts.filter(fact => !mergedIntoRuntimeLine
    || fact.label !== '进程标识' && fact.label !== '处理器占用')
}
function rowState(row: LocalComponentRow) {
  const readyAlternative = hasReadyTtsAlternative(row)
  if (readyAlternative && row.state !== 'ready' && row.state !== 'loading') return 'disabled'
  if (row.category === 'voice-server' && row.resource?.required === false && row.state === 'failed') return 'disabled'
  if (rowResources(row).some(resource => resource.updateAvailable
    && (resource.required !== false || row.category === 'tts' && !readyAlternative))) return 'failed'
  if (row.state === 'loading' || row.state === 'failed' && (row.error || row.retryable)) return row.state
  if (row.resource && resourceNeedsInstall(row.resource)) return 'disabled'
  if (row.sharedResource && (row.sharedResource.state !== 'installed' || row.sharedResource.updateAvailable)) return 'disabled'
  return row.state
}
function rowStatus(row: LocalComponentRow) {
  if (hasReadyTtsAlternative(row) && row.state !== 'ready' && row.state !== 'loading') {
    return row.resource?.updateAvailable ? '需更新' : row.status
  }
  if (row.resource?.updateAvailable) return '需更新'
  if (row.resource && row.resource.state !== 'installed') return resourceStateLabel(row.resource.state)
  if (rowResources(row).some(resource => resource.updateAvailable)) return '需更新'
  if (row.state === 'loading' || row.state === 'failed' && (row.error || row.retryable)) return row.status
  if (row.sharedResource && (row.sharedResource.state !== 'installed' || row.sharedResource.updateAvailable)) {
    return row.sharedResource.updateAvailable ? '运行环境需更新' : '运行环境待配置'
  }
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
function rowResources(row: LocalComponentRow) {
  return [row.resource, ...(row.resourceChoices ?? []), row.sharedResource, ...(row.runtimeDependencies ?? [])]
    .filter((resource): resource is ManagedResourceRow => Boolean(resource))
}
function rowResourceIds(row: LocalComponentRow) {
  return [row.resource?.id, ...(row.resourceChoices ?? []).map(resource => resource.id), ...(row.runtimeDependencies ?? []).map(resource => resource.id)]
    .filter((id): id is string => Boolean(id))
}
function rowJobs(row: LocalComponentRow) {
  return [...new Map([...new Set(rowResourceIds(row))]
    .map(id => visibleJob(resourceJob(id)))
    .filter((job): job is ManagedResourceJob => Boolean(job)).map(job => [job.id, job])).values()]
}
function rowActionResources(row: LocalComponentRow) {
  if (row.runtimeDependencies?.length) return []
  const resources = [row.resource, ...(row.resourceChoices ?? [])].filter((resource): resource is ManagedResourceRow => Boolean(resource))
  return resources.filter(resource => resource.state !== 'installed' || resource.updateAvailable)
}
function rowHasActiveJob(row: LocalComponentRow) {
  return rowJobs(row).some(job => jobIsActive(job))
}
function resourceActionLabel(row: LocalComponentRow, resource: ManagedResourceRow) {
  return resource.updateAvailable
    ? resourceRetryable(resource) ? '重试更新' : '更新'
    : resourceRetryable(resource) ? '重试下载' : '下载'
}
function categoryObservation(rows: LocalComponentRow[]) {
  const activeResource = rows.some(row => rowJobs(row).some(jobIsActive))
  const healthRows = rows[0]?.category === 'voice-server'
    ? rows.filter(row => row.resource?.required !== false)
    : rows
  const blocking = (row: LocalComponentRow) => row.facts.some(fact => fact.label === '语音门禁')
    || row.category === 'voice-server' && row.resource?.required !== false
  const blockingFailure = healthRows.some(row => blocking(row) && row.state === 'failed')
  const blockingMissing = healthRows.some(row => blocking(row) && needsResource(row))
  // 分类摘要要反映子卡当前的启动过程；可选依赖不参与 Voice Server（语音服务）的汇总状态。
  const componentLoading = rows.some(row => rowState(row) === 'loading'
    && (row.category !== 'voice-server' || row.resource?.required !== false))
  const hasReadyComponent = rows.some(row => rowState(row) === 'ready' && row.available)
  const hasFailedComponent = rows.some(row => row.state === 'failed')
  const missing = healthRows.some(needsResource)
  const label = activeResource ? '下载中'
    : blockingFailure ? '错误'
      : componentLoading ? '启动中'
        : blockingMissing ? '待下载或更新'
          : hasReadyComponent ? '运行中'
            : missing ? '待下载或更新'
              : hasFailedComponent ? '待检查'
                : rows.length ? '待配置或已停用' : '暂无组件'
  const sum = (key: 'resident' | 'privateCommit' | 'vram') => {
    const bytes = rows.flatMap(row => {
      const value = row.resourceBytes?.[key]
      return typeof value === 'number' && Number.isFinite(value) && (key === 'vram' ? value > 0 : value >= 0) ? [value] : []
    })
    return bytes.length ? formatComponentMemory(bytes.reduce((total, value) => total + value, 0)) : undefined
  }
  const state = activeResource || componentLoading ? 'loading' : blockingFailure ? 'failed'
    : hasReadyComponent ? 'ready' : 'disabled'
  return { state, label,
    residentMemory: sum('resident'), privateMemory: sum('privateCommit'), vram: sum('vram') }
}
function categoryProgress(category: LocalComponentCategory, rows: LocalComponentRow[]): ManagedResourceJob | undefined {
  const ids = new Set(rows.flatMap(rowResourceIds))
  const jobs = [...new Map([...ids].flatMap(id => {
    const job = resourceJob(id)
    return job && (jobIsActive(job) || ['complete', 'completed', 'failed'].includes(job.state)) ? [job] : []
  }).map(job => [job.id, job])).values()]
  const active = jobs.filter(jobIsActive)
  const failed = jobs.filter(job => job.state === 'failed')
  if (!active.length && !failed.length) return undefined
  const tracked = jobs.filter(job => jobIsActive(job) || ['complete', 'completed', 'failed'].includes(job.state))
  const currentKind = active.some(job => job.progressKind === 'download') ? 'download'
    : active.some(job => job.progressKind === 'verify') ? 'verify'
      : active.some(job => job.progressKind) ? 'waiting' : undefined
  const counters = tracked.map(job => currentKind === 'download'
    ? [job.downloadCompletedBytes, job.downloadTotalBytes]
    : currentKind === 'verify' ? [job.verificationCompletedBytes, job.verificationTotalBytes]
      : [job.completedBytes, job.totalBytes])
  const measurable = currentKind !== 'waiting' && counters.length > 0
    && counters.every(([done, total]) => typeof done === 'number' && Number.isFinite(done)
      && typeof total === 'number' && Number.isFinite(total) && total > 0)
  const phase = active.length
    ? `正在处理 ${active.length} 项资源：${[...new Set(active.map(job => job.phase).filter(Boolean))].join(' · ')}`
    : failed[0]?.phase || '资源任务失败'
  return {
    id: `category-${category}`, resourceId: category, label: categoryDefinitions.find(item => item.id === category)?.title ?? category,
    state: active.length ? 'running' : 'failed', phase,
    progressKind: currentKind,
    ...(measurable ? {
      completedBytes: counters.reduce((sum, [done]) => sum + (done ?? 0), 0),
      totalBytes: counters.reduce((sum, [, total]) => sum + (total ?? 0), 0),
    } : {}),
    error: failed[0]?.error, canCancel: false,
  }
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
    <p v-for="error in model.errors" :key="error.id" class="local-error" role="alert" :data-error-source="error.id">{{ error.message }}</p>
    <p v-if="model.resourceEventError" class="local-error" role="status">{{ model.resourceEventError }}</p>
    <div class="local-list-heading local-managed-heading">
      <div><h3>组件分类</h3></div>
      <div class="local-heading-actions">
        <button v-if="model.resourceActionsAvailable" class="outline-button resource-download-required" type="button" :disabled="model.resourceTasksBusy || model.resourceLoading || !!batchJob && jobIsActive(batchJob)" @click="emit('resourceDownloadRequired')">一键下载</button>
        <button v-if="batchJob && jobIsActive(batchJob)" class="outline-button resource-batch-import" type="button" :disabled="!batchJob.canCancel" @click="emit('cancelResource', batchJob.id)">{{ batchJob.canCancel ? '取消任务' : '正在完成安装…' }}</button>
        <button v-else-if="model.resourceActionsAvailable" class="outline-button resource-batch-import" type="button" :disabled="model.resourceTasksBusy || model.resourceLoading" @click="emit('resourceImportAll')">资源导入</button>
      </div>
      <ManagedResourceProgress v-if="batchJob" class="managed-batch-progress" :job="batchJob" :label="batchJob.resourceId === 'required-batch' ? '一键下载' : '资源导入'" />
    </div>
    <div v-if="model.resourceLoading && !model.rows.some(row => row.resource)" class="resource-empty" role="status">正在读取固定资源状态…</div>
    <div class="local-category-list">
      <section v-for="category in categories" :key="category.id" class="local-category-card" :class="{ expanded: expandedCategories.has(category.id) }" :data-category="category.id">
        <header class="local-category-heading">
          <div class="local-category-main">
            <div class="local-category-row">
              <button class="local-category-toggle" type="button" :aria-expanded="expandedCategories.has(category.id)" :aria-controls="`local-category-${category.id}`" @click="toggleCategory(category.id)">
                <span class="local-category-title"><strong>{{ category.title }}</strong><small>{{ category.description }} · {{ category.rows.length }} 个组件</small></span>
              </button>
              <div class="local-category-observability">
                <span class="local-state" :class="category.observation.state"><i aria-hidden="true"></i>{{ category.observation.label }}</span>
                <dl v-if="category.observation.residentMemory || category.observation.privateMemory || category.observation.vram" class="local-category-metrics">
                  <div v-if="category.observation.residentMemory || category.observation.privateMemory">
                    <dt>{{ category.observation.residentMemory && category.observation.privateMemory ? '常驻内存／提交内存' : category.observation.residentMemory ? '常驻内存' : '提交内存' }}</dt>
                    <dd>{{ [category.observation.residentMemory, category.observation.privateMemory].filter(Boolean).join(' / ') }}</dd>
                  </div>
                  <div v-if="category.observation.vram"><dt>显存</dt><dd>{{ category.observation.vram }}</dd></div>
                </dl>
              </div>
              <button class="local-category-disclosure" type="button" :aria-expanded="expandedCategories.has(category.id)" :aria-controls="`local-category-${category.id}`" @click="toggleCategory(category.id)"><span>{{ expandedCategories.has(category.id) ? '收起' : '展开' }}</span><svg viewBox="0 0 24 24" aria-hidden="true" :class="{ expanded: expandedCategories.has(category.id) }"><path d="m6 9 6 6 6-6" /></svg></button>
            </div>
            <ManagedResourceProgress v-if="!expandedCategories.has(category.id) && category.progress" class="local-category-progress" :job="category.progress" :label="category.title" />
          </div>
          <div class="local-category-actions">
            <button v-if="category.id !== 'tts' && model.resourceActionsAvailable && category.pendingCount" class="outline-button category-download" type="button" :disabled="model.resourceLoading" @click="downloadCategory(category)">下载组件（{{ category.pendingCount }}）</button>
            <button v-if="category.id !== 'tts' && model.resourceActionsAvailable && batchImportAvailable && category.pendingCount" class="outline-button category-import" type="button" :disabled="model.resourceTasksBusy || model.resourceLoading" @click="emit('resourceImportAll')">导入</button>
          </div>
        </header>
        <div v-if="expandedCategories.has(category.id)" :id="`local-category-${category.id}`" class="local-category-content">
          <label v-if="category.id === 'tts' && model.ttsSelection" class="local-tts-selection form-field">
            <span>语音播放目标</span>
            <select :value="model.ttsSelection.options.some(option => option.value === model.ttsSelection!.value) ? model.ttsSelection.value : ''" :disabled="model.ttsSelection.saving || model.resourceTasksBusy" @change="emit('ttsSelection', ($event.target as HTMLSelectElement).value)">
              <option v-if="!model.ttsSelection.options.some(option => option.value === model.ttsSelection!.value)" value="" disabled>当前选择未就绪，请补齐资源或选择其他目标</option>
              <option v-for="option in model.ttsSelection.options" :key="option.value" :value="option.value">{{ option.label }}</option>
            </select>
            <small v-if="model.ttsSelection.error" class="local-error">{{ model.ttsSelection.error }}</small>
          </label>
          <p v-if="!category.rows.length" class="resource-empty">暂无该分类的组件状态。</p>
          <div v-else class="local-component-list">
            <article v-for="{ row, job } in category.rows" :key="row.id" class="local-component-card" :class="job ? jobState(job) : rowState(row)" :data-component="row.id">
              <div class="local-component-summary">
                <div class="local-component-body">
                  <div class="local-component-identity" :class="{ 'has-provider-usage': Boolean(row.provider) }">
                    <div class="local-component-name"><h4 :title="row.name">{{ row.name }}</h4><small v-if="row.category === 'voice-server' && row.resource?.required === false">可选运行依赖，不阻塞基础语音服务</small></div>
                    <div class="local-component-state-group">
                      <FixedProviderUsage v-if="row.provider" class="local-component-provider-usage" :model="row.provider" :refresh-key="providerUsageRevision" />
                      <span class="local-state" :class="job ? jobState(job) : rowState(row)"><i aria-hidden="true"></i>{{ job ? jobStateLabel(job) : rowStatus(row) }}</span>
                    </div>
                  </div>
                  <p v-if="row.state === 'loading'" class="local-component-phase" aria-live="polite">{{ row.description }}</p>
                  <dl v-if="row.runtime && row.runtime !== '未提供' || hasComponentMemory(row)" class="local-component-metrics local-summary-metrics">
                    <div v-if="row.runtime && row.runtime !== '未提供'" class="local-runtime-metric" data-metric="runtime"><dt>运行时</dt><dd>{{ row.runtime }}</dd></div>
                    <div v-if="hasComponentMemory(row)" class="local-runtime-metric" data-metric="memory"><dt>{{ componentMemoryLabel(row) }}</dt><dd>{{ componentMemoryValue(row) }}</dd></div>
                  </dl>
                </div>
                <div class="local-component-controls">
                <div v-if="row.resourceChoices?.length && rowActionResources(row).length && model.resourceActionsAvailable && !rowHasActiveJob(row)" class="local-inline-resource-actions">
                  <div v-for="resource in selectedResources(row)" :key="resource.id" class="local-inline-resource-action">
                    <span class="local-inline-resource-status"><strong>{{ resource.label }}</strong><span class="resource-install-state" :class="resource.state">{{ resource.updateAvailable ? '需更新' : resource.status || resourceStateLabel(resource.state) }}</span></span>
                    <template v-if="rowActionResources(row).some(actionResource => actionResource.id === resource.id)">
                      <button class="outline-button" type="button" :aria-label="`${resource.label}：${resourceActionLabel(row, resource)}`" :disabled="resource.busy || model.resourceLoading" @click="emit('resourceDownload', resource.id)">{{ resource.busy ? '处理中…' : resourceActionLabel(row, resource) }}</button>
                      <button class="outline-button" type="button" :aria-label="`${resource.label}：导入`" :disabled="resource.busy || model.resourceLoading" @click="emit('resourceImport', resource.id)">导入</button>
                      <button class="outline-button" type="button" :aria-label="`${resource.label}：跳转`" :disabled="model.resourceLoading" @click="emit('openResourceSource', resource.id)">跳转</button>
                    </template>
                  </div>
                </div>
                <div v-if="!row.resourceChoices?.length && rowActionResources(row).length && model.resourceActionsAvailable && !rowHasActiveJob(row)" class="local-inline-resource-actions">
                  <div v-for="resource in rowActionResources(row)" :key="resource.id" class="local-inline-resource-action">
                    <button class="outline-button" type="button" :aria-label="`${resource.label}：${resourceActionLabel(row, resource)}`" :disabled="resource.busy || model.resourceLoading" @click="emit('resourceDownload', resource.id)">{{ resource.busy ? '处理中…' : resourceActionLabel(row, resource) }}</button>
                    <button class="outline-button" type="button" :aria-label="`${resource.label}：导入`" :disabled="resource.busy || model.resourceLoading" @click="emit('resourceImport', resource.id)">导入</button>
                    <button class="outline-button" type="button" :aria-label="`${resource.label}：跳转`" :disabled="model.resourceLoading" @click="emit('openResourceSource', resource.id)">跳转</button>
                  </div>
                </div>
                <button v-if="row.enhancement || row.runtimeDependencies?.length || expandedComponents.has(row.id) || !rowHasActiveJob(row) && (!rowActionResources(row).length || !model.resourceActionsAvailable)" class="local-component-action" type="button" :aria-label="`${row.name}：${expandedComponents.has(row.id) ? '收起详情' : '详情'}`" :aria-expanded="expandedComponents.has(row.id)" :aria-controls="`local-detail-${row.id}`" @click="toggleComponent(row)">
                  {{ expandedComponents.has(row.id) ? '收起' : '详情' }}
                  <svg viewBox="0 0 24 24" aria-hidden="true" :class="{ expanded: expandedComponents.has(row.id) }"><path d="m6 9 6 6 6-6" /></svg>
                </button>
                </div>
              </div>
              <div v-for="resourceJob in row.runtimeDependencies?.length && expandedComponents.has(row.id) ? [] : rowJobs(row)" :key="resourceJob.id" class="local-component-inline-progress">
                <ManagedResourceProgress :job="resourceJob" :label="resourceJob.label || row.name" />
                <button v-if="jobIsActive(resourceJob)" class="outline-button local-resource-cancel" type="button" :aria-label="`${row.name}：取消任务`" :disabled="!resourceJob.canCancel" @click="emit('cancelResource', resourceJob.id)">取消任务</button>
              </div>
              <div v-if="expandedComponents.has(row.id)" :id="`local-detail-${row.id}`" class="local-component-detail">
                <p v-if="!row.resource && !row.provider && row.description">{{ row.description }}</p>
                <section v-if="row.runtimeDependencies?.length" class="local-runtime-dependencies" aria-label="语音运行依赖">
                  <article v-for="dependency in row.runtimeDependencies" :key="dependency.id" class="local-download-choice" :data-runtime-dependency="dependency.id">
                    <div class="local-managed-resource-heading"><strong>{{ dependency.label }}</strong><span class="resource-install-state" :class="dependency.state">{{ dependency.updateAvailable ? '需更新' : dependency.status || resourceStateLabel(dependency.state) }}</span></div>
                    <small class="local-detail-note">{{ runtimeDescription(dependency.id) }}</small>
                    <dl v-if="resourceRuntimeFacts(dependency, row).length" class="local-resource-versions"><div v-for="fact in resourceRuntimeFacts(dependency, row)" :key="fact.label"><dt>{{ fact.label }}</dt><dd :title="fact.value">{{ fact.value }}</dd></div></dl>
                    <ManagedResourceProgress v-if="visibleJob(resourceJob(dependency.id))" :job="visibleJob(resourceJob(dependency.id))!" :label="dependency.label" />
                    <div v-if="model.resourceActionsAvailable" class="managed-resource-actions">
                      <button v-if="jobIsActive(resourceJob(dependency.id))" class="outline-button" type="button" :disabled="!resourceJob(dependency.id)?.canCancel" @click="emit('cancelResource', resourceJob(dependency.id)!.id)">取消任务</button>
                      <template v-else-if="runtimeNeedsAction(dependency)">
                        <button class="outline-button" type="button" :aria-label="`${dependency.label}：${resourceActionLabel(row, dependency)}`" :disabled="dependency.busy || model.resourceLoading || rowHasActiveJob(row)" @click="emit('resourceDownload', dependency.id)">{{ dependency.busy ? '处理中…' : resourceActionLabel(row, dependency) }}</button>
                        <button class="outline-button" type="button" :aria-label="`${dependency.label}：导入`" :disabled="dependency.busy || model.resourceLoading || rowHasActiveJob(row)" @click="emit('resourceImport', dependency.id)">导入</button>
                        <button class="outline-button" type="button" :aria-label="`${dependency.label}：跳转`" :disabled="model.resourceLoading" @click="emit('openResourceSource', dependency.id)">跳转</button>
                      </template>
                    </div>
                    <p v-if="dependency.error && !jobIsActive(resourceJob(dependency.id))" class="local-error" role="status">{{ dependency.error }}</p>
                  </article>
                </section>
                <section v-else-if="row.resource" class="local-managed-resource" :data-resource="row.resource.id" :aria-label="`${row.resource.label}安装状态`">
                  <div v-if="row.resource.updateAvailable || row.resource.state !== 'installed' || !resourceRuntimeFacts(row.resource, row).length" class="local-managed-resource-heading"><span>{{ row.resource.label }}</span><span class="resource-install-state" :class="row.resource.state">{{ row.resource.status || resourceStateLabel(row.resource.state) }}</span></div>
                  <dl v-if="resourceRuntimeFacts(row.resource, row).length" class="local-resource-versions"><div v-for="fact in resourceRuntimeFacts(row.resource, row)" :key="fact.label"><dt>{{ fact.label }}</dt><dd :title="fact.value">{{ fact.value }}</dd></div></dl>
                  <p v-if="row.resource.error && row.state !== 'loading' && !jobIsActive(latestJobs.get(row.resource.id))" class="local-error" role="status">{{ row.resource.error }}</p>
                </section>
                <section v-if="row.sharedResource" class="local-managed-resource" :data-resource="row.sharedResource.id" :aria-label="`${row.sharedResource.label}安装状态`">
                  <div class="local-managed-resource-heading"><span>{{ row.sharedResource.label }}（共享运行程序）</span><span class="resource-install-state" :class="row.sharedResource.state">{{ row.sharedResource.status || resourceStateLabel(row.sharedResource.state) }}</span></div>
                  <dl v-if="resourceRuntimeFacts(row.sharedResource).length" class="local-resource-versions"><div v-for="fact in resourceRuntimeFacts(row.sharedResource)" :key="fact.label"><dt>{{ fact.label }}</dt><dd :title="fact.value">{{ fact.value }}</dd></div></dl>
                  <p v-if="row.sharedResource.error && !rowHasActiveJob(row)" class="local-error" role="status">{{ row.sharedResource.error }}</p>
                </section>
                <FixedProviderSettings v-if="row.provider" :model="row.provider" @saved="refreshProviderUsage" />
                <p v-if="!job && row.resource && needsResource(row) && !row.resource.error" class="local-resource-hint" role="status">{{ row.resource.required === false ? '可选组件；缺失或待更新不阻塞启动。' : '资源缺失或需要更新，可通过分类卡片中的下载操作处理。' }}</p>
                <p v-if="row.error && !row.busy" class="local-error" role="alert">{{ row.error }}</p>
                <button v-if="row.retryable && !row.busy" class="outline-button retry-action" type="button" @click="emit('retry', row.id)">重试组件</button>
                <dl v-if="detailFacts(row).length" class="local-detail-facts"><div v-for="fact in detailFacts(row)" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ fact.value }}</dd></div></dl>
                <label v-if="row.enhancement" class="switch-row component-enhancement-toggle" :aria-busy="row.enhancement.busy">
                  <div class="component-enhancement-copy"><strong>{{ row.enhancement.label }}</strong><small aria-live="polite">{{ row.enhancement.busy ? row.enhancement.busyLabel || '切换中' : row.enhancement.enabled ? '已开启' : '已关闭' }}</small></div>
                  <input type="checkbox" role="switch" :aria-label="`${row.enhancement.label}开关`" :checked="row.enhancement.enabled" :disabled="row.enhancement.busy" @change="row.enhancement.toggle()" />
                  <i aria-hidden="true"></i>
                </label>
                <div v-if="row.device" class="local-device-editor">
                  <label :for="`local-device-${row.id}`"><span>运行位置</span><select :id="`local-device-${row.id}`" :value="row.device.value" :disabled="saving || row.device.locked" @change="changeDevice(row.id, $event)"><option v-for="option in row.device.options" :key="option.value" :value="option.value" :disabled="option.disabled">{{ option.label }}</option></select></label>
                  <button class="save-button" type="button" :disabled="saving || row.device.locked || invalidDevice(row)" @click="emit('save', row.id)">{{ row.device.busy ? '切换中' : saving ? '保存中' : '保存' }}</button>
                  <small v-if="row.device.draft || row.device.pending"><span v-if="row.device.draft">运行位置草稿尚未保存</span><span v-if="row.device.draft && row.device.pending"> · </span><span v-if="row.device.pending">设备请求与当前运行状态不一致</span></small>
                </div>
                <div v-else-if="!row.resource || !resourceRuntimeFacts(row.resource, row).length" class="local-location"><span>运行位置</span><strong>{{ row.location || '本机' }}</strong></div>
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
.local-runtime-dependencies{display:grid;gap:12px;width:100%;min-width:0}.local-runtime-dependencies .local-managed-resource-heading{color:var(--text);flex-wrap:wrap}.local-runtime-dependencies .managed-resource-actions{margin-top:0}
.local-download-choice{display:grid;gap:8px;padding:12px;border:1px solid var(--line);border-radius:12px}
.local-component-phase{margin:6px 0 0;color:var(--muted);font-size:var(--font-caption);line-height:1.6}
.local-components{display:flex;flex-direction:column;gap:20px;min-width:0;container-type:inline-size}
.local-managed-heading>div{display:grid;gap:4px;min-width:0}
.local-managed-heading>div>small{color:var(--muted)}
.resource-batch-import{white-space:nowrap}.local-managed-heading .local-heading-actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.local-managed-heading h3{font-size:var(--font-section);margin:0}.local-tts-selection{max-width:520px;padding:12px 0}.managed-batch-progress{grid-column:1 / -1}
.resource-empty{padding:14px 16px;border:1px dashed var(--line-strong);border-radius:12px;color:var(--muted);font-size:var(--font-body)}
.local-category-list{display:grid;gap:12px}.local-category-card{min-width:0;border:1px solid var(--line-strong);border-radius:16px;background:#ffffff02;overflow:hidden}.local-category-card.expanded{background:#ffffff04}.local-category-heading{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:14px;padding:12px 16px}.local-category-main{display:grid;gap:9px;min-width:0}.local-category-toggle{display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0;padding:3px 0;text-align:left;background:transparent;border:0;cursor:pointer}.local-category-toggle>span{display:grid;gap:4px;min-width:0}.local-category-toggle strong{font-size:var(--font-section);line-height:1.4}.local-category-toggle small{color:var(--muted);line-height:1.5}.local-category-toggle svg{flex:none;width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;transition:transform 180ms}.local-category-toggle svg.expanded{transform:rotate(180deg)}.local-category-observability{display:grid;gap:8px;min-width:0}.local-category-metrics{grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:8px 14px}.local-category-progress{max-width:620px}.local-category-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}.category-download,.category-import{white-space:nowrap;height:var(--compact-control-height);min-height:var(--compact-control-height);padding:6px 11px;font-size:var(--font-caption)}.local-category-content{display:grid;gap:10px;padding:0 8px 14px}.local-category-content>.resource-empty{margin-top:2px}
.local-managed-resource{display:grid;gap:8px;padding:10px 16px;border-top:1px solid var(--line);background:#00000008}
.local-category-heading{align-items:center}
.local-category-main{gap:0}
.local-category-row{display:grid;grid-template-columns:minmax(150px,1fr) auto auto;align-items:center;gap:16px;width:100%;padding:3px 0}
.local-category-toggle{display:block;width:100%;min-width:0;padding:0;border:0;background:transparent;color:inherit;text-align:left;cursor:pointer}
.local-category-title{display:grid;gap:4px;min-width:0}
.local-category-observability{display:grid;justify-items:end;align-content:center;gap:3px;min-width:0;min-height:54px}
.local-category-metrics{display:flex;justify-content:flex-end;gap:3px 14px;min-width:0;margin:0;text-align:right;font-variant-numeric:tabular-nums}
.local-category-metrics>div{display:grid;gap:2px;min-width:0}
.local-category-metrics dt{font-size:var(--font-caption);color:var(--subtle);white-space:nowrap}
.local-category-metrics dd{font-size:var(--font-body);font-weight:650;line-height:1.25;white-space:nowrap}
.local-category-disclosure{display:inline-flex;align-items:center;justify-content:center;gap:5px;height:var(--compact-control-height);min-height:var(--compact-control-height);padding:5px 10px;border:1px solid var(--line-strong);border-radius:9px;background:#ffffff03;color:inherit;font:inherit;font-size:var(--font-body);font-weight:600;white-space:nowrap;cursor:pointer}
.local-category-disclosure svg{width:14px;height:14px}
.local-category-progress{max-width:620px;margin:8px 0 0 2px}
.local-component-provider-usage{margin:0;align-self:center}
@container (max-width:760px){.local-category-row{grid-template-columns:minmax(0,1fr) auto auto;gap:8px}.local-category-observability{min-height:54px}.local-category-metrics{gap:3px 8px}}
.local-managed-resource-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0;font-size:var(--font-caption);color:var(--muted)}
.local-managed-resource-heading>span:first-child{min-width:0;overflow-wrap:anywhere}
.local-runtime-dependencies .local-managed-resource-heading>strong{flex:1 1 auto;min-width:0;white-space:nowrap;overflow-wrap:normal}
.local-resource-versions{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,140px),1fr));gap:6px;min-width:0}.local-resource-versions>div{display:grid;gap:2px;min-width:0;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:#ffffff02}.local-resource-versions dt{font-size:var(--font-caption);color:var(--subtle)}.local-resource-versions dd{min-width:0;font-size:var(--font-caption);line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
.local-component-list{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;width:100%}
.local-component-card{width:100%;min-width:0;box-sizing:border-box;border:1px solid var(--line);border-radius:12px;background:#ffffff02;overflow:hidden}
.local-component-card.failed{border-color:color-mix(in srgb,var(--danger) 28%,transparent)}
/* action column（操作列）：按钮文案或箭头变化不挤动名称与状态列。 */
.local-component-summary{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;align-items:center;min-height:72px;padding:11px 14px}
.local-install-actions{margin-top:0;max-width:240px;justify-content:flex-end}
.local-component-body{min-width:0}
.local-component-controls{display:grid;justify-items:end;align-content:center;gap:8px;min-width:0}
/* name column（名称列）：统一预留宽度，使状态不随名称长度移动。 */
.local-component-identity{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:12px;align-items:center;margin-bottom:8px}
.local-component-identity.has-provider-usage{margin-bottom:0}
.local-component-name{display:grid;gap:2px;min-width:0}
.local-component-name>small{font-size:var(--font-caption);line-height:1.4;color:var(--muted)}
.local-component-identity h4{font-size:var(--font-body);line-height:1.5;overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.local-component-state-group>.local-state{line-height:18px;flex:none;justify-content:flex-end;text-align:right}
.local-summary-metrics{grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:7px 16px;margin-top:6px}
.local-inline-resource-actions{display:grid;gap:6px;justify-items:end;max-width:310px}
.local-inline-resource-action{display:flex;align-items:center;justify-content:flex-end;gap:5px;flex-wrap:wrap}
.local-inline-resource-status{display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:100%;font-size:var(--font-caption)}
.local-inline-resource-status strong{min-width:0;overflow-wrap:anywhere}
.local-inline-resource-action button{min-height:var(--compact-control-height);padding:5px 8px;font-size:var(--font-caption);white-space:nowrap}
.local-component-inline-progress{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:10px;padding:0 14px 12px}
.local-component-state-group{display:flex;align-items:center;justify-content:flex-end;gap:8px 14px;min-width:0;flex-wrap:wrap}
.local-component-identity .local-component-provider-usage{flex:1 1 0;margin:0;min-width:0;align-self:center}
dl,dd{margin:0}
.local-component-metrics{display:grid;grid-template-columns:minmax(100px,1fr) auto;align-items:start;gap:8px 16px}
.local-runtime-metric,.local-resource-metrics>div{min-width:0;display:flex;flex-direction:column;gap:3px}
.local-resource-metrics{display:flex;justify-content:flex-end;gap:16px;text-align:right;min-width:0}
.local-resource-metrics dd{white-space:nowrap}
.local-component-metrics dt{white-space:nowrap}
.local-detail-facts>div{min-width:0;display:flex;flex-direction:column;gap:4px}
dt{color:var(--subtle)}
dd{font-size:var(--font-body);font-variant-numeric:tabular-nums;overflow-wrap:anywhere;line-height:1.5}
.local-component-action{display:inline-flex;align-self:center;align-items:center;justify-content:center;gap:4px;height:var(--compact-control-height);min-height:var(--compact-control-height);min-width:68px;padding:6px 8px;font-size:var(--font-caption);white-space:nowrap;background:#ffffff03}
.local-component-action svg{transition:transform 180ms}
.local-component-action svg.expanded{transform:rotate(180deg)}
.retry-action{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 30%,transparent)}
.local-error{color:var(--danger);font-size:var(--font-caption);overflow-wrap:anywhere;white-space:pre-wrap}
.local-component-body>.local-error{margin-top:10px}
.local-component-detail{display:grid;gap:14px;padding:16px;border-top:1px solid var(--line);background:#0000000b}
.local-component-detail>p{color:var(--muted);font-size:var(--font-body);overflow-wrap:anywhere}
.component-enhancement-toggle{width:100%;min-height:54px;margin:0;padding:10px 16px;cursor:pointer}
.component-enhancement-copy{display:grid;gap:3px;min-width:0}
.component-enhancement-copy small{font-size:var(--font-caption);line-height:1.35;color:var(--subtle)}
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
@container (max-width:560px){.local-component-identity{grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:8px}.local-component-state-group{justify-content:flex-end;flex-wrap:nowrap}.local-component-state-group>.local-state{white-space:nowrap}.local-component-identity .local-component-provider-usage{flex:1 1 0;min-width:0}}
@container (max-width:440px){.local-memory-summary{grid-template-columns:1fr;gap:8px}.local-memory-summary article{flex-direction:row;align-items:center;justify-content:space-between;padding:12px;gap:8px}.local-component-summary{grid-template-columns:minmax(0,1fr);padding:10px;gap:9px}.local-component-controls{justify-items:stretch}.local-category-heading{grid-template-columns:minmax(0,1fr);padding:11px 12px}.local-category-actions{justify-content:stretch}.category-download,.category-import{flex:1}.local-category-content{padding:0 10px 10px}.local-component-metrics{gap:6px}.local-component-detail{padding:12px}.local-component-action{justify-self:stretch;width:100%;padding-inline:8px}.local-inline-resource-actions{max-width:none;justify-items:stretch}.local-inline-resource-action{justify-content:stretch}.local-inline-resource-action button{flex:1}.local-component-inline-progress{grid-template-columns:1fr;padding:0 10px 10px}.local-resource-progress{padding:0 12px 12px}.local-managed-resource{padding:10px 12px}.managed-resource-actions{gap:6px}}
@container (max-width:560px){.local-managed-heading{align-items:flex-start}.resource-batch-import{white-space:normal;max-width:100%}}
@container (max-width:520px){.local-component-metrics{grid-template-columns:1fr}.local-resource-metrics{gap:12px}.local-resource-metrics dt,.local-resource-metrics dd{font-size:var(--font-caption)}}
.local-component-card[data-component="tts-audiocpp-runtime"] .local-component-identity{grid-template-columns:minmax(260px,1.2fr) minmax(0,1fr)}
.local-component-card[data-component="tts-audiocpp-runtime"] .local-component-identity h4{display:block;white-space:nowrap;word-break:normal;overflow-wrap:normal;overflow:hidden;text-overflow:clip}
@container (max-width:560px){.local-component-card[data-component="tts-audiocpp-runtime"] .local-component-identity{grid-template-columns:minmax(220px,1.2fr) minmax(0,1fr)}}
@container (max-width:440px){.local-component-card[data-component="tts-audiocpp-runtime"] .local-component-identity{grid-template-columns:minmax(0,1fr)}}
@media (prefers-reduced-motion:reduce){.local-component-action svg{transition:none}}
@media (prefers-reduced-motion:reduce){.local-category-toggle svg{transition:none}}
</style>

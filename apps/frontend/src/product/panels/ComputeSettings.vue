<script setup lang="ts">
import ResourceInstallDialog from './ResourceInstallDialog.vue';
import { computed, ref } from 'vue'
import LocalComponents from '../../interface/LocalComponents.vue'
import { formatComponentMemory, type LocalComponentRow, type LocalComponentsModel, type ManagedResourceRow } from '../../interface/local-components'
import { useApplicationHost, type ApplicationComponent } from '../application-host'
import { useProductContext } from '../product-context'
import type { ComputeDeviceMode, ProductLocalModelId } from '../types'
import { componentResources, modelComponentOwners as owners } from '../component-resources'
import type { useWorkspaceResources } from '../useWorkspaceResources'

const props = defineProps<{ resourceController?: ReturnType<typeof useWorkspaceResources> }>()
const { settings, saving, computeStatus, computeSavedDevices, refreshComputeStatus, saveComputeSettings, computeModelRows, deviceLabel, serviceLabel } = useProductContext()
const application = useApplicationHost()
const refreshing = ref(false)
const retrying = ref(new Set<string>())
// modelResourceIds（模型资源编号映射）：与 catalog.resource_id（资源清单编号）一致。
const modelResourceIds: Record<ProductLocalModelId, string> = {
  kws: 'kws-sherpa-onnx',
  paraformer: 'paraformer-streaming',
  voice_vad: 'fsmn-vad',
  wespeaker: 'wespeaker-cnceleb-resnet34',
  smart_turn: 'smart-turn',
  sensevoice: 'sensevoice-small',
}
// availableModelServices（可用模型状态）：与后端就绪契约一致，工作中的状态同样可用。
// READY（就绪）、RUNNING（运行中）、STREAMING（流式传输）、CONNECTED（已连接）；DEGRADED（降级）沿用原有可用规则。
const availableModelServices = new Set(['READY', 'RUNNING', 'STREAMING', 'CONNECTED', 'DEGRADED'])
function componentFor(id: string) { return application.snapshot.value?.components?.find(row => row.id === id) }
const resources = computed(() => componentResources(application.snapshot.value))
const systemComponents = computed(() => (application.snapshot.value?.components ?? []).filter(row => !['asr', 'analysis', 'speaker', 'dsh'].includes(row.id)))
function managedResource(resourceId: string): ManagedResourceRow | undefined {
  const resource = props.resourceController?.resources.value.find(row => row.resource_id === resourceId)
  if (!resource) return undefined
  const jobs = props.resourceController?.resourceJobs.value.filter(job => job.resource_id === resourceId) ?? []
  const active = jobs.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state))
  const failed = jobs.some(job => job.state === 'failed')
  return {
    id: resource.resource_id,
    label: resource.label,
    state: resource.installation_state,
    status: resource.installation_state === 'installed' ? '已安装' : resource.installation_state === 'incomplete' ? '不完整' : '缺失',
    updateAvailable: resource.update_available === true,
    installedVersion: resource.installed_version,
    targetVersion: resource.resource_id === 'voice-server' || resource.resource_id === 'dsh' ? undefined : resource.version,
    error: resource.error || jobs.find(job => job.state === 'failed')?.error || undefined,
    retryable: failed,
    busy: Boolean(active || props.resourceController?.startingResources.value.has(resourceId)
      || props.resourceController?.pendingDownloadPlan.value?.resource_id === resourceId),
  }
}
function facts(component?: ApplicationComponent) {
  return [
    ...(component?.pid == null ? [] : [{ label: '进程标识', value: String(component.pid) }]),
    ...(component?.cpu_percent == null ? [] : [{ label: '处理器占用', value: component.cpu_percent.toFixed(1) + '%' }]),
  ]
}
const rows = computed<LocalComponentRow[]>(() => {
  const models = computeModelRows.map<LocalComponentRow>(item => {
    const status = computeStatus.value?.models?.[item.id]
    const owner = componentFor(owners[item.id])
    const resource = status?.resource_status
    const managed = managedResource(modelResourceIds[item.id])
    const countedHere = ['paraformer', 'sensevoice', 'wespeaker'].includes(item.id)
    const metrics = resources.value.get(owners[item.id])
    const legacyResource = application.snapshot.value?.resource_processes === undefined ? resource : undefined
    const busy = Boolean(owner?.retrying || retrying.value.has(owners[item.id]))
    // ownerUnavailable（所属进程不可用）：停止、阻塞或无进程编号的失败会使旧模型快照失效；
    // 仍有 pid（进程编号）的 failed（失败）表示宿主仍报告进程存在，Voice（语音服务）核心门禁也会这样标记，模型状态应独立展示。
    // external（外部进程）由外部管理，不用缺少 pid 推断其退出。
    const ownerUnavailable = !busy && Boolean(owner) && owner?.external !== true && owner?.state !== 'external' &&
      (['blocked', 'stopped'].includes(owner?.state ?? '') || (owner?.state === 'failed' && owner.pid == null))
    // busy（正在重试）先覆盖旧故障；宿主加载状态优先于启动期间的临时健康检查结果。
    const ownerLoading = ['waiting', 'starting', 'stopping'].includes(owner?.state ?? '')
    const disabled = !busy && !ownerUnavailable && (owner?.state === 'disabled' || (!ownerLoading && application.snapshot.value?.startup_complete !== false && status?.service === 'DISABLED'))
    const awaitingStartup = application.snapshot.value?.startup_complete === false && !availableModelServices.has(status?.service ?? '')
    const loading = busy || (!ownerUnavailable && !disabled && (ownerLoading || awaitingStartup || (!owner && !status) || status?.service === 'STARTING'))
    // 模型详情接口尚未开放时，以宿主已通过的工作进程健康检查呈现就绪状态。
    const updateRequired = managed?.updateAvailable === true
    const available = !updateRequired && !loading && !ownerUnavailable && !disabled && (status ? availableModelServices.has(status.service) : ['ready', 'external'].includes(owner?.state ?? ''))
    const ownerError = ownerUnavailable ? owner?.error || status?.runtime_error || (owner?.state === 'blocked' ? '组件依赖不可用，请检查所属进程。' : owner?.state === 'stopped' ? '组件进程已退出。' : '组件运行异常，请查看日志并重试。') : undefined
    const gpuAvailable = available && !refreshing.value && status?.gpu_available === true
    return {
      id: item.id, name: item.title,
      state: updateRequired ? 'failed' : available ? 'ready' : disabled ? 'disabled' : loading ? 'loading' : 'failed',
      status: updateRequired ? '需更新' : loading ? owner?.state === 'stopping' ? '退出中' : '启动中' : ownerUnavailable ? '不可用' : disabled ? '已停用' : !status && available ? '运行中' : serviceLabel(status?.service),
      available, retryable: !updateRequired && !available && !loading && !disabled && Boolean(owner?.retry_allowed), busy: loading,
      runtime: status?.runtime || '未提供',
      residentMemory: countedHere ? formatComponentMemory(metrics?.memory_rss_bytes ?? legacyResource?.rss_bytes) : undefined,
      privateMemory: countedHere ? formatComponentMemory(metrics?.memory_private_commit_bytes ?? legacyResource?.private_commit_bytes) : undefined,
      vram: countedHere && status?.device?.startsWith('cuda') ? formatComponentMemory(metrics?.gpu_dedicated_bytes ?? legacyResource?.gpu_dedicated_bytes) : undefined,
      description: updateRequired ? '此模型需更新并通过校验后才能启动语音服务。' : loading ? busy && !owner?.retrying ? '正在重新启动组件' : owner?.phase || '等待组件启动' : item.detail, error: updateRequired ? '此模型需更新并通过校验后才能启动语音服务。' : !available && !loading && !disabled ? ownerError || status?.runtime_error || status?.load_error || owner?.error : undefined,
      note: countedHere ? '资源来自宿主同次采样，包含该进程及其子进程。' : item.id === 'smart_turn' ? '与 SenseVoice（语音识别与分析）共享进程，资源计入该条目。' : '与 Voice Server（语音服务）共享进程，资源计入语音服务条目。',
      facts: [
        ...(resource?.pid == null ? [] : [{ label: '进程标识', value: String(resource.pid) }]),
        ...(resource?.pid === owner?.pid && owner?.cpu_percent != null ? [{ label: '处理器占用', value: owner.cpu_percent.toFixed(1) + '%' }] : []),
      ],
      resource: managed,
      device: {
        value: settings.compute.devices[item.id], current: deviceLabel(status?.device),
        locked: updateRequired || !available || !status || refreshing.value,
        draft: settings.compute.devices[item.id] !== computeSavedDevices.value[item.id],
        pending: Boolean(status && computeSavedDevices.value[item.id] !== status.requested_device),
        options: [
          { value: 'auto', label: '自动选择' }, { value: 'cpu', label: 'CPU（中央处理器）' },
          ...(gpuAvailable ? [{ value: 'cuda', label: 'GPU（图形处理器）' }] : settings.compute.devices[item.id] === 'cuda' ? [{ value: 'cuda', label: 'GPU（当前不可用）', disabled: true }] : []),
        ],
        hint: updateRequired ? '此模型需更新并通过校验后才能启动语音服务。' : status?.load_error && available ? status.load_error : !gpuAvailable ? status?.gpu_unavailable_reason || '当前推理环境不支持显卡运行。' : undefined,
      },
    }
  })
  const components = (application.snapshot.value?.components ?? []).filter(row => row.id === 'dsh').map<LocalComponentRow>(component => {
    const metrics = resources.value.get(component.id)
    const busy = component.retrying || retrying.value.has(component.id)
    const loading = busy || ['starting', 'waiting', 'stopping'].includes(component.state)
    const available = !loading && ['ready', 'external'].includes(component.state)
    return {
      id: component.id, name: component.name,
      state: loading ? 'loading' : available ? 'ready' : component.state === 'disabled' ? 'disabled' : 'failed',
      status: loading ? component.state === 'stopping' ? '退出中' : '启动中' : available ? '运行中' : component.state === 'disabled' ? '已停用' : '不可用',
      available, retryable: !loading && component.retry_allowed, busy: loading,
      runtime: component.id === 'dsh' ? 'Node.js（执行环境）' : component.id === 'voice' ? 'Python（语音进程）' : component.id === 'web' ? '桌面宿主' : '本地进程',
      residentMemory: formatComponentMemory(metrics?.memory_rss_bytes), privateMemory: formatComponentMemory(metrics?.memory_private_commit_bytes),
      vram: metrics?.gpu_dedicated_bytes != null && metrics.gpu_dedicated_bytes > 0 ? formatComponentMemory(metrics.gpu_dedicated_bytes) : undefined,
      description: busy && !component.retrying ? '正在重新启动组件' : component.phase, error: !loading && !available && component.state !== 'disabled' ? component.error : undefined,
      location: '本机', facts: facts(component),
      resource: managedResource('dsh'),
      note: component.shared_with ? '与应用宿主共享进程，内存总量只计一次。' : component.external ? '此组件使用外部调试进程。' : undefined,
    }
  })
  const system = systemComponents.value
  const environment = props.resourceController?.resources.value.find(row => row.resource_id === 'voice-server')
  if (system.length || environment) {
    const metrics = resources.value.get('voice-system')
    const failure = system.find(row => !row.retrying && !retrying.value.has(row.id) && ['failed', 'blocked', 'stopped'].includes(row.state))
    const busy = system.some(row => row.retrying || retrying.value.has(row.id))
    const loading = busy || system.some(row => ['waiting', 'starting', 'stopping'].includes(row.state))
    const active = system.find(row => row.retrying || retrying.value.has(row.id) || ['waiting', 'starting', 'stopping'].includes(row.state))
    components.push({
      id: 'voice-system', name: 'Voice Server（语音服务）',
      state: failure ? 'failed' : loading ? 'loading' : !system.length && !environment?.can_start ? 'disabled' : 'ready',
      status: failure ? '部分不可用' : loading ? '启动中' : system.length ? '运行中' : environment?.can_start ? '已配置' : '待配置',
      available: !failure && !loading && Boolean(system.length || environment?.can_start), retryable: !loading && system.some(row => row.retry_allowed), busy: loading && !failure,
      runtime: '本地系统服务', location: '本机',
      residentMemory: formatComponentMemory(metrics?.memory_rss_bytes), privateMemory: formatComponentMemory(metrics?.memory_private_commit_bytes),
      vram: metrics?.gpu_dedicated_bytes != null && metrics.gpu_dedicated_bytes > 0 ? formatComponentMemory(metrics.gpu_dedicated_bytes) : undefined,
      description: loading && !failure ? active && retrying.value.has(active.id) && !active.retrying ? '正在重新启动组件' : active?.phase || '等待系统服务启动' : '统一管理桌面窗口、界面服务、应用宿主、环境检查与语音服务。',
      error: failure ? `${failure.name}：${failure.error || failure.phase}` : undefined,
      resource: managedResource('voice-server'),
      facts: [
        ...(environment?.python ? [{ label: 'Python（后端解释器）', value: environment.python }] : []),
        ...(environment?.version ? [{ label: '解释器版本', value: environment.version }] : []),
        ...system.map(row => ({ label: row.name, value: ['ready', 'external'].includes(row.state) ? row.id === 'setup' ? '已通过' : '运行中' : row.phase })),
      ],
      note: '包含系统进程及其子进程；模型工作进程和代理执行宿主单独计量，共享进程只计一次。',
    })
  }
  return [...models, ...components]
})
const model = computed<LocalComponentsModel>(() => {
  const snapshot = application.snapshot.value
  const hardware = computeStatus.value?.hardware
  const failed = rows.value.some(row => row.state === 'failed')
  const loading = rows.value.some(row => row.state === 'loading')
  // 总显存取自受管进程的去重计数，不把整卡占用当作应用占用。
  const gpu = hardware?.gpu
  return {
    status: failed ? '部分组件不可用' : loading ? '组件启动中' : '运行正常', state: failed ? 'failed' : loading ? 'loading' : 'ready',
    sampledAt: snapshot?.sampled_at == null ? '—' : new Date(snapshot.sampled_at * 1000).toLocaleString('zh-CN', { hour12: false }),
    resources: [
      { id: 'resident', label: '常驻内存', used: formatComponentMemory(snapshot?.total_memory_rss_bytes), total: formatComponentMemory(hardware?.system_memory.total_bytes) },
      { id: 'commit', label: '提交内存', used: formatComponentMemory(snapshot?.total_memory_private_commit_bytes), total: formatComponentMemory(hardware?.system_memory.commit_limit_bytes) },
      { id: 'vram', label: '显存', used: formatComponentMemory(snapshot?.total_gpu_dedicated_bytes), total: formatComponentMemory(gpu?.total_bytes) },
    ],
    managedResourceJobs: props.resourceController?.resourceJobs.value.map(job => {
      const resource = props.resourceController?.resources.value.find(item => item.resource_id === job.resource_id)
      return {
        id: job.job_id,
        resourceId: job.resource_id,
        label: job.resource_id === 'batch' ? '批量资源导入'
          : resource?.installation_state === 'installed' && resource.update_available ? `更新 ${resource.label}`
            : resource?.label || job.resource_id,
        state: job.state,
        phase: job.phase,
        completedBytes: job.completed_bytes,
        totalBytes: job.total_bytes,
        error: job.error || undefined,
        canCancel: job.can_cancel !== false,
      }
    }),
    resourceActionsAvailable: props.resourceController?.resourcesCanManage.value ?? false,
    resourceLoading: Boolean(props.resourceController?.loading.value || props.resourceController?.refreshing.value
      || props.resourceController?.inventoryRefreshing.value),
    resourceError: props.resourceController?.resourceError.value || undefined,
    resourceEventError: props.resourceController?.eventError.value || undefined,
    resourceTasksBusy: Boolean(props.resourceController?.startingResources.value.size
      || props.resourceController?.resourceJobs.value.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state))),
    resourceBatchRetry: props.resourceController?.resourceJobs.value.some(job => job.resource_id === 'batch' && job.state === 'failed') ?? false,
    rows: rows.value, error: application.error.value, refreshing: refreshing.value,
  }
})
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try { await Promise.allSettled([application.refresh(), refreshComputeStatus(true), props.resourceController?.refresh()]) }
  finally { refreshing.value = false }
}
async function retry(id: string) {
  if (id === 'voice-system') {
    const component = ['setup', 'web', 'voice', 'desktop'].map(componentFor).find(row => row?.retry_allowed)
    if (component) await retry(component.id)
    return
  }
  const owner = owners[id as ProductLocalModelId] ?? id
  if (retrying.value.has(owner)) return
  retrying.value.add(owner)
  try { await application.retry(owner); await refreshComputeStatus() }
  finally { retrying.value.delete(owner) }
}
function device(id: string, value: string) {
  if (refreshing.value || rows.value.find(row => row.id === id)?.device?.locked) return
  if (computeModelRows.some(row => row.id === id) && ['auto', 'cpu', 'cuda'].includes(value)) {
    if (value === 'cuda' && computeStatus.value?.models?.[id as ProductLocalModelId]?.gpu_available !== true) return
    settings.compute.devices[id as ProductLocalModelId] = value as ComputeDeviceMode
  }
}
function resourceDownload(id: string) { void props.resourceController?.downloadResource(id) }
function resourceRefresh() { void props.resourceController?.refreshInventory() }
function resourceImport(id: string) { void props.resourceController?.importResource(id) }
function resourceImportAll() { void props.resourceController?.importResource() }
function openResourceSource(id: string) { void props.resourceController?.openResourceSource(id) }
function cancelResource(id: string) { void props.resourceController?.cancelJob(id) }
</script>
<template>
  <ResourceInstallDialog v-if="resourceController" :controller="resourceController" />
  <LocalComponents :model="model" :saving="saving" @refresh="refresh" @retry="retry" @device="device" @save="saveComputeSettings" @resource-download="resourceDownload" @resource-refresh="resourceRefresh" @resource-import="resourceImport" @resource-import-all="resourceImportAll" @open-resource-source="openResourceSource" @cancel-resource="cancelResource" />
</template>

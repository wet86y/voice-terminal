<script setup lang="ts">
import { computed, ref } from 'vue'
import LocalComponents from '../../interface/LocalComponents.vue'
import { formatComponentMemory, type LocalComponentRow, type LocalComponentsModel } from '../../interface/local-components'
import { useApplicationHost, type ApplicationComponent } from '../application-host'
import { useProductContext } from '../product-context'
import type { ComputeDeviceMode, ProductLocalModelId } from '../types'
import { componentResources, modelComponentOwners as owners } from '../component-resources'

const { settings, saving, computeStatus, computeSavedDevices, refreshComputeStatus, saveComputeSettings, computeModelRows, deviceLabel, serviceLabel } = useProductContext()
const application = useApplicationHost()
const refreshing = ref(false)
const retrying = ref(new Set<string>())
function componentFor(id: string) { return application.snapshot.value?.components?.find(row => row.id === id) }
const resources = computed(() => componentResources(application.snapshot.value))
const systemComponents = computed(() => (application.snapshot.value?.components ?? []).filter(row => !['asr', 'analysis', 'speaker', 'dsh'].includes(row.id)))
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
    const countedHere = ['paraformer', 'sensevoice', 'wespeaker'].includes(item.id)
    const metrics = resources.value.get(owners[item.id])
    const legacyResource = application.snapshot.value?.resource_processes === undefined ? resource : undefined
    const busy = Boolean(owner?.retrying || retrying.value.has(owners[item.id]))
    // busy（正在重试）先覆盖旧故障；宿主加载状态优先于启动期间的临时健康检查结果。
    const ownerFailed = !busy && ['failed', 'blocked', 'stopped'].includes(owner?.state ?? '')
    const ownerLoading = ['waiting', 'starting', 'stopping'].includes(owner?.state ?? '')
    const disabled = !busy && !ownerFailed && (owner?.state === 'disabled' || (!ownerLoading && application.snapshot.value?.startup_complete !== false && status?.service === 'DISABLED'))
    const awaitingStartup = application.snapshot.value?.startup_complete === false && !['READY', 'DEGRADED'].includes(status?.service ?? '')
    const loading = busy || (!ownerFailed && !disabled && (ownerLoading || awaitingStartup || (!owner && !status) || status?.service === 'STARTING'))
    // 模型详情接口尚未开放时，以宿主已通过的工作进程健康检查呈现就绪状态。
    const available = !loading && !ownerFailed && !disabled && (status ? ['READY', 'DEGRADED'].includes(status.service) : ['ready', 'external'].includes(owner?.state ?? ''))
    const ownerError = ownerFailed ? owner?.error || status?.runtime_error || (owner?.state === 'blocked' ? '组件依赖不可用，请检查所属进程。' : owner?.state === 'stopped' ? '组件进程已退出。' : '组件运行异常，请查看日志并重试。') : undefined
    const gpuAvailable = available && !refreshing.value && status?.gpu_available === true
    return {
      id: item.id, name: item.title,
      state: available ? 'ready' : disabled ? 'disabled' : loading ? 'loading' : 'failed',
      status: loading ? owner?.state === 'stopping' ? '退出中' : '启动中' : ownerFailed ? '不可用' : disabled ? '已停用' : !status && available ? '运行中' : serviceLabel(status?.service),
      available, retryable: !available && !loading && !disabled && Boolean(owner?.retry_allowed), busy: loading,
      runtime: status?.runtime || '未提供',
      residentMemory: countedHere ? formatComponentMemory(metrics?.memory_rss_bytes ?? legacyResource?.rss_bytes) : undefined,
      privateMemory: countedHere ? formatComponentMemory(metrics?.memory_private_commit_bytes ?? legacyResource?.private_commit_bytes) : undefined,
      vram: countedHere && status?.device?.startsWith('cuda') ? formatComponentMemory(metrics?.gpu_dedicated_bytes ?? legacyResource?.gpu_dedicated_bytes) : undefined,
      description: loading ? busy && !owner?.retrying ? '正在重新启动组件' : owner?.phase || '等待组件启动' : item.detail, error: !available && !loading && !disabled ? ownerError || status?.runtime_error || status?.load_error || owner?.error : undefined,
      note: countedHere ? '资源来自宿主同次采样，包含该进程及其子进程。' : item.id === 'smart_turn' ? '与 SenseVoice（语音识别与分析）共享进程，资源计入该条目。' : '与 Voice Server（语音服务）共享进程，资源计入语音服务条目。',
      facts: [
        ...(resource?.pid == null ? [] : [{ label: '进程标识', value: String(resource.pid) }]),
        ...(resource?.pid === owner?.pid && owner?.cpu_percent != null ? [{ label: '处理器占用', value: owner.cpu_percent.toFixed(1) + '%' }] : []),
      ],
      device: {
        value: settings.compute.devices[item.id], current: deviceLabel(status?.device),
        locked: !available || !status || refreshing.value,
        draft: settings.compute.devices[item.id] !== computeSavedDevices.value[item.id],
        pending: Boolean(status && computeSavedDevices.value[item.id] !== status.requested_device),
        options: [
          { value: 'auto', label: '自动选择' }, { value: 'cpu', label: 'CPU（中央处理器）' },
          ...(gpuAvailable ? [{ value: 'cuda', label: 'GPU（图形处理器）' }] : settings.compute.devices[item.id] === 'cuda' ? [{ value: 'cuda', label: 'GPU（当前不可用）', disabled: true }] : []),
        ],
        hint: status?.load_error && available ? status.load_error : !gpuAvailable ? status?.gpu_unavailable_reason || '当前推理环境不支持显卡运行。' : undefined,
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
      note: component.shared_with ? '与应用宿主共享进程，内存总量只计一次。' : component.external ? '此组件使用外部调试进程。' : undefined,
    }
  })
  const system = systemComponents.value
  if (system.length) {
    const metrics = resources.value.get('voice-system')
    const failure = system.find(row => !row.retrying && !retrying.value.has(row.id) && ['failed', 'blocked', 'stopped'].includes(row.state))
    const busy = system.some(row => row.retrying || retrying.value.has(row.id))
    const loading = busy || system.some(row => ['waiting', 'starting', 'stopping'].includes(row.state))
    const active = system.find(row => row.retrying || retrying.value.has(row.id) || ['waiting', 'starting', 'stopping'].includes(row.state))
    components.push({
      id: 'voice-system', name: 'Voice Server（语音服务）',
      state: failure ? 'failed' : loading ? 'loading' : 'ready',
      status: failure ? '部分不可用' : loading ? '启动中' : '运行中',
      available: !failure && !loading, retryable: !loading && system.some(row => row.retry_allowed), busy: loading && !failure,
      runtime: '本地系统服务', location: '本机',
      residentMemory: formatComponentMemory(metrics?.memory_rss_bytes), privateMemory: formatComponentMemory(metrics?.memory_private_commit_bytes),
      vram: metrics?.gpu_dedicated_bytes != null && metrics.gpu_dedicated_bytes > 0 ? formatComponentMemory(metrics.gpu_dedicated_bytes) : undefined,
      description: loading && !failure ? active && retrying.value.has(active.id) && !active.retrying ? '正在重新启动组件' : active?.phase || '等待系统服务启动' : '统一管理桌面窗口、界面服务、应用宿主、环境检查与语音服务。',
      error: failure ? `${failure.name}：${failure.error || failure.phase}` : undefined,
      facts: system.map(row => ({ label: row.name, value: ['ready', 'external'].includes(row.state) ? row.id === 'setup' ? '已通过' : '运行中' : row.phase })),
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
    ], rows: rows.value, error: application.error.value, refreshing: refreshing.value,
  }
})
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try { await Promise.all([application.refresh(), refreshComputeStatus(true)]) }
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
</script>
<template>
  <LocalComponents :model="model" :saving="saving" @refresh="refresh" @retry="retry" @device="device" @save="saveComputeSettings" />
</template>

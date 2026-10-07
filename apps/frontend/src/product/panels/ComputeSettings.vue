<script setup lang="ts">
import { computed, ref } from 'vue'
import LocalComponents from '../../interface/LocalComponents.vue'
import { formatComponentMemory, type LocalComponentRow, type LocalComponentsModel } from '../../interface/local-components'
import { useApplicationHost, type ApplicationComponent } from '../application-host'
import { useProductContext } from '../product-context'
import type { ComputeDeviceMode, ProductLocalModelId } from '../types'

const { settings, saving, computeStatus, computeSavedDevices, refreshComputeStatus, saveComputeSettings, computeModelRows, deviceLabel, serviceLabel } = useProductContext()
const application = useApplicationHost()
const refreshing = ref(false)
const retrying = ref(new Set<string>())
/** owners（模型所属进程）：对应现有宿主组件，重试继续使用原有接口。 */
const owners: Record<ProductLocalModelId, string> = {
  paraformer: 'asr', sensevoice: 'analysis', voice_vad: 'voice', kws: 'voice', smart_turn: 'analysis', wespeaker: 'speaker',
}
function componentFor(id: string) { return application.snapshot.value?.components?.find(row => row.id === id) }
function facts(component?: ApplicationComponent) {
  return [
    { label: '进程标识', value: component?.pid == null ? '未提供' : String(component.pid) },
    { label: '处理器占用', value: component?.cpu_percent == null ? '未提供' : component.cpu_percent.toFixed(1) + '%' },
  ]
}
const rows = computed<LocalComponentRow[]>(() => {
  const models = computeModelRows.map<LocalComponentRow>(item => {
    const status = computeStatus.value?.models?.[item.id]
    const owner = componentFor(owners[item.id])
    const resource = status?.resource_status
    const busy = Boolean(owner?.retrying || retrying.value.has(owners[item.id]))
    // ownerFailed（所属进程故障）优先于上一次模型采样，文案和操作采用同一判定。
    const ownerFailed = ['failed', 'blocked', 'stopped'].includes(owner?.state ?? '')
    const disabled = !ownerFailed && (status?.service === 'DISABLED' || owner?.state === 'disabled')
    const loading = busy || (!ownerFailed && !disabled && (['waiting', 'starting', 'stopping'].includes(owner?.state ?? (!status ? 'starting' : '')) || status?.service === 'STARTING'))
    const available = !loading && !ownerFailed && !disabled && ['READY', 'DEGRADED'].includes(status?.service ?? '')
    const ownerError = ownerFailed ? owner?.error || status?.runtime_error || (owner?.state === 'blocked' ? '组件依赖不可用，请检查所属进程。' : owner?.state === 'stopped' ? '组件进程已退出。' : '组件运行异常，请查看日志并重试。') : undefined
    const gpuAvailable = status?.gpu_available === true
    return {
      id: item.id, name: item.title,
      state: available ? 'ready' : disabled ? 'disabled' : loading ? 'loading' : 'failed',
      status: busy ? '正在恢复' : ownerFailed ? '不可用' : disabled ? '已停用' : loading ? owner?.state === 'stopping' ? '退出中' : '启动中' : serviceLabel(status?.service),
      available, retryable: !available && !loading && Boolean(owner?.retry_allowed), busy,
      runtime: status?.runtime || '未提供',
      residentMemory: formatComponentMemory(resource?.rss_bytes), privateMemory: formatComponentMemory(resource?.private_commit_bytes),
      vram: status?.device?.startsWith('cuda') ? '未提供' : undefined,
      description: item.detail, error: !available && !loading ? ownerError || status?.runtime_error || status?.load_error || owner?.error : undefined,
      note: '内存为所在进程占用，共享进程不重复计入总量。' + (status?.device?.startsWith('cuda') ? '当前未提供单个模型的显存指标。' : ''),
      facts: [
        { label: '进程标识', value: resource?.pid == null ? '未提供' : String(resource.pid) },
        { label: '处理器占用', value: resource?.pid === owner?.pid && owner?.cpu_percent != null ? owner.cpu_percent.toFixed(1) + '%' : '未提供' },
      ],
      device: {
        value: settings.compute.devices[item.id], current: deviceLabel(status?.device),
        draft: settings.compute.devices[item.id] !== computeSavedDevices.value[item.id],
        pending: Boolean(status && computeSavedDevices.value[item.id] !== status.requested_device),
        options: [
          { value: 'auto', label: '自动选择' }, { value: 'cpu', label: 'CPU（中央处理器）' },
          { value: 'cuda', label: 'GPU（图形处理器）', disabled: !gpuAvailable },
        ],
        hint: status?.load_error && available ? status.load_error : !gpuAvailable ? status?.gpu_unavailable_reason || '当前推理环境不支持显卡运行。' : undefined,
      },
    }
  })
  const components = (application.snapshot.value?.components ?? []).filter(row => !['asr', 'analysis', 'speaker'].includes(row.id)).map<LocalComponentRow>(component => {
    const busy = component.retrying || retrying.value.has(component.id)
    const available = !busy && ['ready', 'external'].includes(component.state)
    const loading = busy || ['starting', 'waiting'].includes(component.state)
    return {
      id: component.id, name: component.name,
      state: available ? 'ready' : component.state === 'disabled' ? 'disabled' : loading ? 'loading' : 'failed',
      status: busy ? '正在恢复' : available ? '运行中' : component.state === 'disabled' ? '已停用' : loading ? '启动中' : '不可用',
      available, retryable: component.retry_allowed && !busy, busy,
      runtime: component.id === 'dsh' ? 'Node.js（执行环境）' : component.id === 'voice' ? 'Python（语音进程）' : component.id === 'web' ? '桌面宿主' : '本地进程',
      residentMemory: formatComponentMemory(component.memory_rss_bytes), privateMemory: formatComponentMemory(component.memory_private_commit_bytes),
      description: component.phase, error: component.error,
      location: '本机', facts: facts(component),
      note: component.shared_with ? '与应用宿主共享进程，内存总量只计一次。' : component.external ? '此组件使用外部调试进程。' : undefined,
    }
  }).sort((a, b) => Number(b.id === 'dsh') - Number(a.id === 'dsh'))
  return [...models, ...components]
})
const model = computed<LocalComponentsModel>(() => {
  const snapshot = application.snapshot.value
  const hardware = computeStatus.value?.hardware
  const failed = rows.value.some(row => row.state === 'failed')
  const loading = rows.value.some(row => row.state === 'loading')
  // 显卡容量来自设备；当前没有可靠的本应用显存占用指标。
  const gpu = hardware?.gpu
  return {
    status: failed ? '部分组件不可用' : loading ? '组件启动中' : '运行正常', state: failed ? 'failed' : loading ? 'loading' : 'ready',
    sampledAt: snapshot?.sampled_at == null ? '—' : new Date(snapshot.sampled_at * 1000).toLocaleString('zh-CN', { hour12: false }),
    resources: [
      { id: 'resident', label: '常驻内存', used: formatComponentMemory(snapshot?.total_memory_rss_bytes), total: formatComponentMemory(hardware?.system_memory.total_bytes) },
      { id: 'commit', label: '提交内存', used: formatComponentMemory(snapshot?.total_memory_private_commit_bytes), total: formatComponentMemory(hardware?.system_memory.commit_limit_bytes) },
      { id: 'vram', label: '显存', used: '未提供', total: formatComponentMemory(gpu?.total_bytes) },
    ], rows: rows.value, error: application.error.value, refreshing: refreshing.value,
  }
})
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try { await Promise.all([application.refresh(), refreshComputeStatus()]) }
  finally { refreshing.value = false }
}
async function retry(id: string) {
  const owner = owners[id as ProductLocalModelId] ?? id
  if (retrying.value.has(owner)) return
  retrying.value.add(owner)
  try { await application.retry(owner); await refreshComputeStatus() }
  finally { retrying.value.delete(owner) }
}
function device(id: string, value: string) {
  if (computeModelRows.some(row => row.id === id) && ['auto', 'cpu', 'cuda'].includes(value)) {
    settings.compute.devices[id as ProductLocalModelId] = value as ComputeDeviceMode
  }
}
</script>
<template>
  <LocalComponents :model="model" :saving="saving" @refresh="refresh" @retry="retry" @device="device" @save="saveComputeSettings" />
</template>

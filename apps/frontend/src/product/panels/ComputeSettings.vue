<script setup lang="ts">
import ResourceInstallDialog from './ResourceInstallDialog.vue';
import { computed, onMounted, ref } from 'vue'
import LocalComponents from '../../interface/LocalComponents.vue'
import { formatComponentMemory, type FixedProviderId, type LocalComponentCategory, type LocalComponentRow, type LocalComponentsModel, type LocalDownloadItem, type LocalProviderSettings, type ManagedResourceRow } from '../../interface/local-components'
import { useApplicationHost, type ApplicationComponent } from '../application-host'
import { useProductContext } from '../product-context'
import { productApi } from '../api'
import type { ComputeDeviceMode, ProductLocalModelId } from '../types'
import { componentResources, modelComponentOwners as owners } from '../component-resources'
import type { useWorkspaceResources } from '../useWorkspaceResources'
import { useLocalTtsStatus } from '../useLocalTtsStatus'
import { operationPhaseLabel, type RuntimeOperation } from '../../runtime/runtime-operations'

const props = defineProps<{ resourceController?: ReturnType<typeof useWorkspaceResources>; operations?: RuntimeOperation[] }>()
const { settings, loading: settingsLoading, saving, computeStatus, computeSavedDevices, refreshComputeStatus, saveComputeSettings, saveAudioEnhancement, computeModelRows, deviceLabel, serviceLabel } = useProductContext()
const application = useApplicationHost()
const tts = useLocalTtsStatus(() => application.snapshot.value?.startup_complete === true)
const refreshing = ref(false)
const retrying = ref(new Set<string>())
const enhancementBusy = ref(false) // enhancementBusy（增强切换事务进行中）。
const enhancementError = ref('') // enhancementError（切换或回滚失败原因）。
type EnhancementKey = 'dual_pass' | 'emotion' | 'acoustic_events'
function enhancement(key: EnhancementKey, label: string) {
  return { label, enabled: settings.audio_enhancements?.[key] !== false,
    busy: enhancementBusy.value || settingsLoading.value, busyLabel: settingsLoading.value ? '读取状态中' : '切换中',
    toggle: async () => {
      if (enhancementBusy.value || settingsLoading.value) return
      enhancementBusy.value = true; enhancementError.value = ''
      try {
        await saveAudioEnhancement(key)
        void refreshComputeStatus(true)
      } catch (failure) { enhancementError.value = failure instanceof Error ? failure.message : String(failure) }
      finally { enhancementBusy.value = false }
    } }
}
const providerLoading = ref(true) // providerLoading（正在读取在线服务配置状态）。
const providerSaving = ref(false) // providerSaving（正在保存在线服务密钥）。
const providerConfigured = ref({ deepseek: false, minimax: false }) // providerConfigured（只含配置状态，不含密钥正文）。
// modelResourceIds（模型资源编号映射）：与 catalog.resource_id（资源清单编号）一致。
const modelResourceIds: Record<ProductLocalModelId, string> = {
  kws: 'kws-sherpa-onnx',
  paraformer: 'paraformer-streaming',
  voice_vad: 'fsmn-vad',
  campplus: 'campplus-zh-cn-common',
  smart_turn: 'smart-turn',
  emotion: 'emotion2vec-plus-base',
}
// deviceSelectableModelIds（可切换 CPU／GPU 的模型）：Paraformer（识别）、emotion2vec+ base（情绪）与 CAM++（声纹）。
const deviceSelectableModelIds = new Set<ProductLocalModelId>(['paraformer', 'emotion', 'campplus'])
const switchOperationLabels: Partial<Record<ProductLocalModelId, string>> = {
  paraformer: 'Paraformer', emotion: 'emotion2vec', campplus: 'CAM++',
}
function computeSwitchOperation(id: ProductLocalModelId) {
  const label = switchOperationLabels[id]
  if (!label) return undefined
  return props.operations?.find(operation => (operation.target === 'compute_device'
    || operation.target === `compute_device:${id}`)
    && operation.state === 'running' && operation.label.startsWith(label))
}
// availableModelServices（可用模型状态）：与后端就绪契约一致，工作中的状态同样可用。
// READY（就绪）、RUNNING（运行中）、STREAMING（流式传输）、CONNECTED（已连接）；DEGRADED（降级）沿用原有可用规则。
const availableModelServices = new Set(['READY', 'RUNNING', 'STREAMING', 'CONNECTED', 'DEGRADED'])
function componentFor(id: string) { return application.snapshot.value?.components?.find(row => row.id === id) }
async function readProviderConfiguration() {
  try {
    const response = await productApi.fixedCredentials()
    providerConfigured.value = { deepseek: response.providers.deepseek.configured, minimax: response.providers.minimax.configured }
  } catch { /* 组件详情仍允许填写；保存时会显示明确错误。 */ }
  finally { providerLoading.value = false }
}
function providerSettings(provider: FixedProviderId): LocalProviderSettings {
  return {
    provider,
    configured: providerConfigured.value[provider],
    disabled: providerLoading.value || providerSaving.value,
    save: async apiKey => {
      providerSaving.value = true
      try {
        const request = provider === 'deepseek' ? { deepseek_api_key: apiKey } : { minimax_api_key: apiKey }
        const response = await productApi.saveFixedCredentials(request)
        providerConfigured.value = { deepseek: response.providers.deepseek.configured, minimax: response.providers.minimax.configured }
        if (provider === 'minimax') await props.resourceController?.refreshInventory()
        return { configured: response.providers[provider].configured, message: response.message }
      } finally { providerSaving.value = false }
    },
    usage: async () => (await productApi.fixedProviderUsage(provider)).data,
  }
}
onMounted(() => { void readProviderConfiguration() })
const resources = computed(() => componentResources(application.snapshot.value, computeStatus.value))
const systemComponents = computed(() => (application.snapshot.value?.components ?? []).filter(row => !['asr', 'analysis', 'speaker', 'dsh'].includes(row.id)))
function managedResource(resourceId: string): ManagedResourceRow | undefined {
  const resource = props.resourceController?.resources.value.find(row => row.resource_id === resourceId)
  if (!resource) return undefined
  const jobs = props.resourceController?.resourceJobs.value.filter(job => job.resource_id === resourceId || resourceId === 'voice-runtime-cpu' && job.resource_id === 'voice-server') ?? []
  const active = jobs.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state))
  const failed = jobs.some(job => job.state === 'failed')
  return {
    id: resource.resource_id,
    label: resource.label,
    state: resource.installation_state,
    status: resource.installation_state === 'installed' ? '已安装' : resource.installation_state === 'incomplete' ? '不完整' : '缺失',
    updateAvailable: resource.update_available === true,
    required: resource.required !== false,
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
    const switchOperation = computeSwitchOperation(item.id)
    const switching = Boolean(switchOperation)
    const owner = componentFor(owners[item.id])
    const resource = status?.resource_status
    const managed = managedResource(modelResourceIds[item.id])
    const sharedEstimate = ['kws', 'voice_vad', 'smart_turn', 'emotion'].includes(item.id)
    const countedHere = !sharedEstimate || resources.value.has(item.id)
    const metrics = resources.value.get(sharedEstimate ? item.id : owners[item.id])
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
    const loading = switching || busy || (!ownerUnavailable && !disabled && (ownerLoading || awaitingStartup || (!owner && !status) || status?.service === 'STARTING'))
    // 模型详情接口尚未开放时，以宿主已通过的工作进程健康检查呈现就绪状态。
    const updateRequired = managed?.updateAvailable === true
    const available = !updateRequired && !loading && !ownerUnavailable && !disabled && (status ? availableModelServices.has(status.service) : ['ready', 'external'].includes(owner?.state ?? ''))
    const ownerError = ownerUnavailable ? owner?.error || status?.runtime_error || (owner?.state === 'blocked' ? '组件依赖不可用，请检查所属进程。' : owner?.state === 'stopped' ? '组件进程已退出。' : '组件运行异常，请查看日志并重试。') : undefined
    const gpuAvailable = available && !refreshing.value && status?.gpu_available === true
    const category: LocalComponentCategory = ['paraformer', 'smart_turn', 'voice_vad'].includes(item.id) ? 'speech' : 'extensions'
    const analysisModel = ['emotion', 'smart_turn'].includes(item.id)
    const modelVram = metrics?.gpu_dedicated_bytes ?? legacyResource?.gpu_dedicated_bytes
    const measuredVram = status?.device?.startsWith('cuda') && typeof modelVram === 'number' && modelVram > 0 ? modelVram : undefined
    return {
      id: item.id, name: item.title, category,
      enhancement: item.id === 'paraformer' ? enhancement('dual_pass', '双路精修') : item.id === 'emotion' ? enhancement('emotion', '情绪背景') : undefined,
      state: updateRequired ? 'failed' : switching ? 'loading' : available ? 'ready' : disabled ? 'disabled' : loading ? 'loading' : 'failed',
      status: updateRequired ? '需更新' : switching ? '切换中' : loading ? owner?.state === 'stopping' ? '退出中' : '启动中' : ownerUnavailable ? '不可用' : disabled ? '已停用' : !status && available ? '运行中' : serviceLabel(status?.service),
      available, retryable: !updateRequired && !available && !loading && !disabled && Boolean(owner?.retry_allowed), busy: loading,
      runtime: status?.runtime || '未提供',
      location: deviceLabel(status?.device),
      residentMemory: countedHere ? formatComponentMemory(metrics?.memory_rss_bytes ?? legacyResource?.rss_bytes) : undefined,
      privateMemory: countedHere ? formatComponentMemory(metrics?.memory_private_commit_bytes ?? legacyResource?.private_commit_bytes) : undefined,
      vram: countedHere && measuredVram !== undefined ? formatComponentMemory(measuredVram) : undefined,
      resourceBytes: {
        resident: countedHere ? metrics?.memory_rss_bytes ?? legacyResource?.rss_bytes ?? undefined : undefined,
        privateCommit: countedHere ? metrics?.memory_private_commit_bytes ?? legacyResource?.private_commit_bytes ?? undefined : undefined,
        vram: countedHere ? measuredVram : undefined,
      },
      description: updateRequired ? '此模型需更新并通过校验后才能启动语音服务。' : switching && switchOperation ? operationPhaseLabel(switchOperation) : loading ? busy && !owner?.retrying ? '正在重新启动组件' : owner?.phase || '等待组件启动' : item.detail, error: updateRequired ? '此模型需更新并通过校验后才能启动语音服务。' : !available && !loading && !disabled ? ownerError || status?.runtime_error || status?.load_error || owner?.error : undefined,
      note: sharedEstimate ? countedHere ? analysisModel ? '包含模型加载增量及共享分析进程余量；共享内存按已加载模型均分，显存计入报告 GPU（图形处理器）的模型。' : '加载与预热的内存增量估算，已从所属进程扣除；共享运行库与后续缓存仍计入进程余量。' : '尚无加载增量采样，资源暂计入所属进程。' : '所属进程当前余量；已扣除单列模型的加载增量估算，显存仍按进程计量。',
      facts: [
        ...(item.id === 'paraformer' && computeStatus.value?.auxiliary?.dual_pass?.load_error ? [{ label: '离线精修错误', value: computeStatus.value.auxiliary.dual_pass.load_error }] : []),
        ...(item.id === 'emotion' ? [{ label: '情绪模型加载增量', value: formatComponentMemory(status?.resource_footprint?.rss_bytes) || "未采样" }] : []),
        ...(resource?.pid == null ? [] : [{ label: '进程标识', value: String(resource.pid) }]),
        ...(resource?.pid === owner?.pid && owner?.cpu_percent != null ? [{ label: '处理器占用', value: owner.cpu_percent.toFixed(1) + '%' }] : []),
      ],
      resource: managed,
      resourceChoices: item.id === 'paraformer' ? [managedResource('paraformer-offline')].filter((row): row is ManagedResourceRow => Boolean(row)) : undefined,
      ...(deviceSelectableModelIds.has(item.id) ? { device: {
        value: settings.compute.devices[item.id], current: deviceLabel(status?.device),
        locked: updateRequired || switching || !available || !status || refreshing.value,
        busy: switching,
        draft: !switching && settings.compute.devices[item.id] !== computeSavedDevices.value[item.id],
        pending: Boolean(status && (
          status.requested_device && computeSavedDevices.value[item.id] !== status.requested_device
          || computeSavedDevices.value[item.id] === 'cpu' && status.device !== 'cpu'
          || computeSavedDevices.value[item.id] === 'cuda' && !String(status.device || '').toLowerCase().startsWith('cuda')
        )),
        options: [
          { value: 'auto', label: '自动选择' }, { value: 'cpu', label: 'CPU（中央处理器）' },
          ...(gpuAvailable || switching ? [{ value: 'cuda', label: 'GPU（图形处理器）' }] : settings.compute.devices[item.id] === 'cuda' ? [{ value: 'cuda', label: 'GPU（当前不可用）', disabled: true }] : []),
        ],
        hint: updateRequired ? '此模型需更新并通过校验后才能启动语音服务。' : switching && switchOperation ? operationPhaseLabel(switchOperation) : status?.load_error && available ? status.load_error : !gpuAvailable && !switching ? status?.gpu_unavailable_reason || '当前推理环境不支持显卡运行。' : undefined,
      } } : {}),
    }
  })
  const acoustic = computeStatus.value?.auxiliary?.acoustic_events
  const acousticMetrics = resources.value.get('acoustic_events')
  const acousticOwner = componentFor('analysis')
  const acousticLoading = Boolean(acousticOwner?.retrying || ['waiting', 'starting', 'stopping'].includes(acousticOwner?.state ?? '') || !acoustic && application.snapshot.value?.startup_complete === false)
  models.push({ id: 'yamnet', name: 'YAMNet（声学事件）', category: 'extensions',
    state: acousticLoading ? 'loading' : acoustic?.service === 'READY' ? 'ready' : acoustic?.service === 'DISABLED' ? 'disabled' : 'failed',
    status: acousticLoading ? '启动中' : serviceLabel(acoustic?.service), available: !acousticLoading && acoustic?.service === 'READY',
    runtime: acoustic?.runtime || 'ONNX Runtime（推理引擎）', location: 'CPU（中央处理器）',
    description: '每秒更新声学信箱；连续相同事件组合延长时间，普通请求和四秒检查均可消费。',
    enhancement: enhancement('acoustic_events', '声学事件'), resource: managedResource('yamnet'),
    error: acoustic?.load_error || undefined, facts: [],
    residentMemory: formatComponentMemory(acousticMetrics?.memory_rss_bytes),
    privateMemory: formatComponentMemory(acousticMetrics?.memory_private_commit_bytes),
    resourceBytes: { resident: acousticMetrics?.memory_rss_bytes ?? undefined,
      privateCommit: acousticMetrics?.memory_private_commit_bytes ?? undefined },
    note: '包含 YAMNet（声学事件）加载增量及共享分析进程内存余量；共享内存按已加载模型均分，显存计入报告 GPU（图形处理器）的模型。',
  })
  for (const [id, name, ownerId] of [
    ['hotwords', '热词修订（音素匹配）', 'voice'],
    ['punctuation', 'CTTransformerStreaming（流式标点）', 'asr'],
  ] as const) {
    const status = computeStatus.value?.auxiliary?.[id]
    const owner = componentFor(ownerId)
    const metrics = resources.value.get(id)
    const ownerStopped = owner && ['stopped', 'blocked', 'failed'].includes(owner.state) && !owner.pid
    const ownerLoading = Boolean(owner?.retrying || retrying.value.has(ownerId) || ['waiting', 'starting', 'stopping'].includes(owner?.state ?? ''))
    const disabled = !ownerLoading && (owner?.state === 'disabled' || status?.service === 'DISABLED')
    const loading = !ownerStopped && !disabled && (ownerLoading || !status && application.snapshot.value?.startup_complete !== true)
    const available = !ownerStopped && !loading && !disabled && Boolean(status && availableModelServices.has(status.service))
    models.push({
      id, name, category: id === 'punctuation' ? 'speech' : 'extensions', state: disabled ? 'disabled' : available ? 'ready' : loading ? 'loading' : 'failed',
      status: disabled ? '已停用或词表为空' : loading ? '启动中' : available ? serviceLabel(status?.service) : !status ? '未提供状态' : '不可用',
      available, retryable: !available && !loading && !disabled && Boolean(owner?.retry_allowed), busy: loading,
      runtime: status?.runtime || '未提供', location: deviceLabel(status?.device),
      residentMemory: formatComponentMemory(metrics?.memory_rss_bytes),
      privateMemory: formatComponentMemory(metrics?.memory_private_commit_bytes),
      resourceBytes: { resident: metrics?.memory_rss_bytes ?? undefined, privateCommit: metrics?.memory_private_commit_bytes ?? undefined, vram: metrics?.gpu_dedicated_bytes ?? undefined },
      description: loading ? owner?.phase || '等待组件启动' : id === 'hotwords' ? '在实时及整句识别之后修订热词；复用语音主服务。' : '对新增识别文字添加实时标点；复用识别工作进程。',
      error: loading ? undefined : ownerStopped ? owner?.error : status?.load_error || undefined,
      resource: id === 'punctuation' ? managedResource('streaming-punctuation') : undefined,
      note: metrics ? '加载及预热的增量估算，已从所属进程扣除；不代表独立进程精确用量。' : '尚无有效加载采样，内存暂计入所属进程。',
      facts: status?.resource_status?.pid ? [{ label: '所属进程', value: String(status.resource_status.pid) }] : [],
    })
  }
  const components = (application.snapshot.value?.components ?? []).filter(row => row.id === 'dsh').map<LocalComponentRow>(component => {
    const metrics = resources.value.get(component.id)
    const busy = component.retrying || retrying.value.has(component.id)
    const loading = busy || ['starting', 'waiting', 'stopping'].includes(component.state)
    const available = !loading && ['ready', 'external'].includes(component.state)
    return {
      id: component.id, name: component.name, category: 'inference',
      state: loading ? 'loading' : available ? 'ready' : component.state === 'disabled' ? 'disabled' : 'failed',
      status: loading ? component.state === 'stopping' ? '退出中' : '启动中' : available ? '运行中' : component.state === 'disabled' ? '已停用' : '不可用',
      available, retryable: !loading && component.retry_allowed, busy: loading,
      runtime: component.id === 'dsh' ? 'Node.js（执行环境）' : component.id === 'voice' ? 'Python（语音进程）' : component.id === 'web' ? '桌面宿主' : '本地进程',
      residentMemory: formatComponentMemory(metrics?.memory_rss_bytes), privateMemory: formatComponentMemory(metrics?.memory_private_commit_bytes),
      vram: metrics?.gpu_dedicated_bytes != null && metrics.gpu_dedicated_bytes > 0 ? formatComponentMemory(metrics.gpu_dedicated_bytes) : undefined,
      resourceBytes: { resident: metrics?.memory_rss_bytes ?? undefined, privateCommit: metrics?.memory_private_commit_bytes ?? undefined, vram: metrics?.gpu_dedicated_bytes != null && metrics.gpu_dedicated_bytes > 0 ? metrics.gpu_dedicated_bytes : undefined },
      description: busy && !component.retrying ? '正在重新启动组件' : component.phase, error: !loading && !available && component.state !== 'disabled' ? component.error : undefined,
      location: '本机', facts: facts(component),
      resource: managedResource('dsh'),
      note: component.shared_with ? '与应用宿主共享进程，内存总量只计一次。' : component.external ? '此组件使用外部调试进程。' : undefined,
    }
  })
  const system = systemComponents.value
  const localMetrics = resources.value.get('local-tts')
  const environment = props.resourceController?.resources.value.find(row => row.resource_id === 'voice-server')
  {
    const metrics = resources.value.get('voice-system')
    const failure = system.find(row => !row.retrying && !retrying.value.has(row.id) && ['failed', 'blocked', 'stopped'].includes(row.state))
    const busy = system.some(row => row.retrying || retrying.value.has(row.id))
    const loading = busy || system.some(row => ['waiting', 'starting', 'stopping'].includes(row.state))
    const active = system.find(row => row.retrying || retrying.value.has(row.id) || ['waiting', 'starting', 'stopping'].includes(row.state))
    components.push({
      id: 'voice-system', name: 'Voice Server（语音服务）', category: 'voice-server',
      state: failure ? 'failed' : loading ? 'loading' : !system.length && !environment?.can_start ? 'disabled' : 'ready',
      status: failure ? '部分不可用' : loading ? '启动中' : system.length ? '运行中' : environment?.can_start ? '已配置' : '待配置',
      available: !failure && !loading && Boolean(system.length || environment?.can_start), retryable: !loading && system.some(row => row.retry_allowed), busy: loading && !failure,
      runtime: '本地系统服务', location: '本机',
      residentMemory: formatComponentMemory(metrics?.memory_rss_bytes), privateMemory: formatComponentMemory(metrics?.memory_private_commit_bytes),
      vram: metrics?.gpu_dedicated_bytes != null && metrics.gpu_dedicated_bytes > 0 ? formatComponentMemory(metrics.gpu_dedicated_bytes) : undefined,
      resourceBytes: { resident: metrics?.memory_rss_bytes ?? undefined, privateCommit: metrics?.memory_private_commit_bytes ?? undefined, vram: metrics?.gpu_dedicated_bytes != null && metrics.gpu_dedicated_bytes > 0 ? metrics.gpu_dedicated_bytes : undefined },
      description: loading && !failure ? active && retrying.value.has(active.id) && !active.retrying ? '正在重新启动组件' : active?.phase || '等待系统服务启动' : '统一管理桌面窗口、界面服务、应用宿主、环境检查与语音服务。',
      error: failure ? `${failure.name}：${failure.error || failure.phase}` : undefined,
      resource: managedResource('voice-server'),
      runtimeDependencies: environment?.runtime_components?.flatMap(resource => {
        const managed = managedResource(resource.resource_id)
        return managed ? [{ ...managed, label: resource.label }] : []
      }),
      facts: [
        ...(environment?.python ? [{ label: 'Python（后端解释器）', value: environment.python }] : []),
        ...(environment?.version ? [{ label: '解释器版本', value: environment.version }] : []),
        ...system.map(row => ({ label: row.name, value: ['ready', 'external'].includes(row.state) ? row.id === 'setup' ? '已通过' : '运行中' : row.phase })),
      ],
      note: '已扣除单列模型的加载增量；尚未归属模型的分析进程资源暂计入本项，采样同步后自动分摊。应用总量取真实进程采样，共享进程只计一次。',
    })
  }
  const ttsResource = props.resourceController?.resources.value.find(row => row.resource_id === 'tts')
  const capability = application.snapshot.value?.unavailable_capabilities?.find(row => row.id === 'tts')
  const ttsLoading = Boolean(tts.status.value?.pending_selection || tts.status.value?.load_state === 'loading'
    || application.snapshot.value?.startup_complete === false && !application.snapshot.value?.startup_skipped
    || capability && ['STARTING', 'LOADING', 'WAITING', 'CONNECTING', 'RETRYING'].includes(capability.state))
  const ttsFailed = !ttsLoading && (Boolean(capability) || ttsResource?.can_start === false || tts.status.value?.load_state === 'error')
  const ttsBackend = tts.status.value?.backend ?? settings.tts.backend
  const selectedLocalModel = ttsBackend === 'voxcpm1' ? `tts-${tts.status.value?.local_model ?? settings.tts.local_model ?? 'voxcpm1'}` : undefined
  const localRuntime = managedResource('tts-audiocpp-runtime')
  const localRuntimeReady = localRuntime?.state === 'installed' && !localRuntime.updateAvailable
  const localModelResources = ['tts-voxcpm2', 'tts-voxcpm1']
    .map(id => managedResource(id)).filter((resource): resource is ManagedResourceRow => Boolean(resource))
    .map(resource => ({
      ...resource,
      status: resource.updateAvailable ? '需更新'
        : resource.state === 'installed' ? localRuntimeReady ? '已就绪' : '已安装，运行环境待配置'
          : resource.state === 'incomplete' ? '不完整' : '待安装',
    }))
  const loadedLocalModelId = ttsBackend === 'voxcpm1' && tts.status.value?.load_state === 'ready'
    && tts.status.value.loaded_model ? `tts-${tts.status.value.loaded_model}` : undefined
  components.push(...localModelResources.map((resource): LocalComponentRow => {
    const selected = resource.id === selectedLocalModel
    // local-tts（本地语音进程）的整进程采样只归当前已加载的 VoxCPM 模型，避免与 audio.cpp 依赖卡重复累计。
    const localModelMetrics = resource.id === loadedLocalModelId ? localMetrics : undefined
    const ready = resource.state === 'installed' && !resource.updateAvailable && localRuntimeReady
    const busy = selected && ttsLoading
    const failed = selected && ttsFailed
    const state = busy ? 'loading' : failed ? 'failed'
      : resource.updateAvailable ? 'failed' : ready ? 'ready' : 'disabled'
    const status = busy ? tts.status.value?.pending_selection ? '切换中' : '启动中'
      : failed ? '待配置或恢复'
        : resource.updateAvailable ? '需更新' : ready ? selected && application.snapshot.value?.startup_complete ? '运行中' : '已就绪'
          : resource.state === 'installed' ? '已安装，运行环境待配置' : resource.state === 'incomplete' ? '不完整' : '待安装'
    return {
      id: resource.id,
      name: resource.label,
      category: 'tts',
      state,
      status,
      available: ready && !busy && !failed,
      busy,
      retryable: failed && Boolean(capability?.retry),
      runtime: '',
      residentMemory: formatComponentMemory(localModelMetrics?.memory_rss_bytes),
      privateMemory: formatComponentMemory(localModelMetrics?.memory_private_commit_bytes),
      vram: localModelMetrics?.gpu_dedicated_bytes != null && localModelMetrics.gpu_dedicated_bytes > 0
        ? formatComponentMemory(localModelMetrics.gpu_dedicated_bytes) : undefined,
      resourceBytes: localModelMetrics ? {
        resident: localModelMetrics.memory_rss_bytes ?? undefined,
        privateCommit: localModelMetrics.memory_private_commit_bytes ?? undefined,
        vram: localModelMetrics.gpu_dedicated_bytes != null && localModelMetrics.gpu_dedicated_bytes > 0
          ? localModelMetrics.gpu_dedicated_bytes : undefined,
      } : undefined,
      description: `${resource.label} 本地语音模型；与另一款 VoxCPM 共用 audio.cpp 运行程序。`,
      error: failed ? capability?.error || ttsResource?.error || tts.status.value?.error || undefined : undefined,
      facts: [],
      resource,
      sharedResource: localRuntime,
      note: 'VoxCPM1 与 VoxCPM2 共用 audio.cpp；下载不会自动切换当前播放后端。',
    }
  }))
  const minimaxConfigured = providerConfigured.value.minimax
  const minimaxSelected = ttsBackend === 'minimax'
  components.push({
    id: 'minimax', name: 'MiniMax 在线 API（语音接口）', category: 'tts',
    state: minimaxSelected && ttsLoading ? 'loading' : minimaxSelected && ttsFailed ? 'failed' : minimaxConfigured ? 'ready' : 'failed',
    status: minimaxSelected && ttsLoading ? '启动中' : minimaxSelected && ttsFailed ? '待配置或恢复' : minimaxConfigured ? '已配置' : '待配置',
    available: minimaxConfigured && !(minimaxSelected && (ttsLoading || ttsFailed)), busy: minimaxSelected && ttsLoading,
    retryable: minimaxSelected && !ttsLoading && ttsFailed && Boolean(capability?.retry),
    runtime: '', location: '在线服务', description: 'MiniMax 在线语音服务。API 密钥在详情中配置；支持的账户用量会显示在组件摘要。',
    error: minimaxSelected && ttsFailed ? capability?.error || tts.status.value?.error || undefined : undefined,
    facts: [], provider: providerSettings('minimax'),
  })
  const startupLoading = application.snapshot.value?.startup_complete === false
  components.push({
    id: 'deepseek', name: 'DeepSeek 在线 API（推理接口）', category: 'inference',
    state: startupLoading || providerLoading.value ? 'loading' : providerConfigured.value.deepseek ? 'ready' : 'failed',
    status: startupLoading ? '启动中' : providerLoading.value ? '读取配置中' : providerConfigured.value.deepseek ? '已配置' : '待配置',
    available: providerConfigured.value.deepseek, busy: startupLoading || providerLoading.value,
    runtime: '', location: '在线服务',
    description: startupLoading ? '正在加载组件' : '配置 DeepSeek 接口密钥并查询账户余额。密钥由服务端安全保存，不会返回页面。',
    facts: [], provider: providerSettings('deepseek'),
  })
  // 能力行：模型行未覆盖的能力（音频、合成、主模型等）单独呈现，并可只恢复该能力。
  const coveredCapabilities = new Set(['asr', 'kws', 'speaker', 'tts'])
  for (const capability of application.snapshot.value?.unavailable_capabilities ?? []) {
    if (coveredCapabilities.has(capability.id)) continue
    const loading = ['STARTING', 'LOADING', 'WAITING', 'CONNECTING', 'RETRYING'].includes(capability.state)
    const ready = ['READY', 'RUNNING', 'STREAMING', 'CONNECTED'].includes(capability.state)
    const disabled = capability.state === 'DISABLED'
    components.push({
      id: `capability:${capability.id}`, name: capability.label,
      state: disabled ? 'disabled' : ready ? 'ready' : loading ? 'loading' : 'failed',
      status: disabled ? '已停用' : ready ? serviceLabel(capability.state) : loading ? '启动中' : '不可用',
      available: ready, retryable: !loading && !ready && !disabled && Boolean(capability.retry),
      busy: loading, runtime: '语音主服务内', location: '本机',
      description: loading ? `${capability.phase}，完成后可开始语音会话。` : capability.phase,
      error: !ready && !loading && !disabled ? capability.error || '能力不可用，可单独恢复。' : undefined,
      facts: capability.blocking ? [{ label: '语音门禁', value: '未就绪时暂不能开始会话' }] : [],
      note: '能力级恢复只重建该能力，不重启语音服务进程。',
    })
  }
  return [...models, ...components]
})
const model = computed<LocalComponentsModel>(() => {
  const snapshot = application.snapshot.value
  const hardware = computeStatus.value?.hardware
  const usableTts = rows.value.some(row => row.category === 'tts' && row.state === 'ready' && row.available)
  const failed = rows.value.some(row => row.state === 'failed' && (row.category === 'tts' ? !usableTts
    : row.id.startsWith('capability:') ? row.facts.some(fact => fact.label === '语音门禁')
      : row.resource?.required === true))
  const loading = rows.value.some(row => row.state === 'loading')
  // 总显存取自受管进程的去重计数，不把整卡占用当作应用占用。
  const gpu = hardware?.gpu
  return {
    status: failed ? '部分组件不可用' : loading ? '组件启动中' : '运行正常', state: failed ? 'failed' : loading ? 'loading' : 'ready',
    sampledAt: snapshot?.sampled_at == null ? '—' : new Date(snapshot.sampled_at * 1000).toLocaleString('zh-CN', { hour12: false }),
    resources: [
      { id: 'resident', label: '常驻内存', used: formatComponentMemory(snapshot?.total_memory_rss_bytes), total: formatComponentMemory(hardware?.system_memory.total_bytes) },
      { id: 'commit', label: '提交内存', used: formatComponentMemory(snapshot?.total_memory_private_commit_bytes), total: formatComponentMemory(hardware?.system_memory.commit_limit_bytes) },
      { id: 'vram', label: '显存', used: typeof snapshot?.total_gpu_dedicated_bytes === 'number' && snapshot.total_gpu_dedicated_bytes > 0 ? formatComponentMemory(snapshot.total_gpu_dedicated_bytes) : undefined, total: formatComponentMemory(gpu?.total_bytes) },
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
        downloadCompletedBytes: job.download_completed_bytes,
        downloadTotalBytes: job.download_total_bytes,
        verificationCompletedBytes: job.verification_completed_bytes,
        verificationTotalBytes: job.verification_total_bytes,
        error: job.error || undefined,
        canCancel: job.can_cancel !== false,
      }
    }),
    resourceActionsAvailable: props.resourceController?.resourcesCanManage.value ?? false,
    downloadResources: (props.resourceController?.resources.value ?? [])
      .map(resource => managedResource(resource.resource_id))
      .filter((resource): resource is ManagedResourceRow => Boolean(resource)),
    resourceLoading: Boolean(props.resourceController?.loading.value || props.resourceController?.refreshing.value
      || props.resourceController?.inventoryRefreshing.value || props.resourceController?.preparingDownloadBundle.value),
    resourceError: props.resourceController?.resourceError.value || undefined,
    resourceEventError: props.resourceController?.eventError.value || undefined,
    resourceTasksBusy: Boolean(props.resourceController?.startingResources.value.size
      || props.resourceController?.resourceJobs.value.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state))),
    resourceBatchRetry: props.resourceController?.resourceJobs.value.some(job => job.resource_id === 'batch' && job.state === 'failed') ?? false,
    rows: rows.value, error: enhancementError.value || application.error.value, refreshing: refreshing.value,
  }
})
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try { await Promise.allSettled([application.refresh(), refreshComputeStatus(true), props.resourceController?.refresh(), tts.refresh()]) }
  finally { refreshing.value = false }
}
async function retry(id: string) {
  if (id === 'tts') { await application.retryCapability('tts'); await tts.refresh(true); return }
  if (id === 'voice-system') {
    const component = ['setup', 'web', 'voice', 'desktop'].map(componentFor).find(row => row?.retry_allowed)
    if (component) await retry(component.id)
    return
  }
  // 能力行只恢复该能力，不重启语音服务进程；其余行沿用所属进程的恢复路径。
  if (id.startsWith('capability:')) {
    await application.retryCapability(id.slice('capability:'.length))
    await Promise.allSettled([application.refresh(), refreshComputeStatus(true)])
    return
  }
  const owner = id === 'hotwords' ? 'voice' : id === 'punctuation' ? 'asr' : owners[id as ProductLocalModelId] ?? id
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
function resourceBundleDownload(request: { title: string; entries: LocalDownloadItem[] }) {
  void props.resourceController?.prepareDownloadBundle(request.entries, request.title)
}
function resourceRefresh() { void props.resourceController?.refreshInventory() }
function resourceImport(id: string) { void props.resourceController?.importResource(id) }
function resourceImportAll() { void props.resourceController?.importResource() }
function openResourceSource(id: string) { void props.resourceController?.openResourceSource(id) }
function cancelResource(id: string) { void props.resourceController?.cancelJob(id) }
</script>
<template>
  <ResourceInstallDialog v-if="resourceController" :controller="resourceController" />
  <LocalComponents :model="model" :saving="saving" @refresh="refresh" @retry="retry" @device="device" @save="saveComputeSettings" @resource-download="resourceDownload" @resource-download-required="resourceController?.downloadRequiredResources()" @resource-bundle-download="resourceBundleDownload" @resource-refresh="resourceRefresh" @resource-import="resourceImport" @resource-import-all="resourceImportAll" @open-resource-source="openResourceSource" @cancel-resource="cancelResource" />
</template>

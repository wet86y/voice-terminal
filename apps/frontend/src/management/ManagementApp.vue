<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import AppFrame from '../interface/AppFrame.vue'
import SettingsFrame from '../interface/SettingsFrame.vue'
import LocalComponents from '../interface/LocalComponents.vue'
import { useStartupCredentials } from './useStartupCredentials'
import { startupIcon } from '../runtime/startup-screen'
import type { LocalComponentCategory, LocalComponentRow, LocalComponentsModel, ManagedResourceRow } from '../interface/local-components'
import ResourceInstallDialog from '../product/panels/ResourceInstallDialog.vue'
import WorkspaceSettings from '../product/panels/WorkspaceSettings.vue'
import { useWorkspaceResources } from '../product/useWorkspaceResources'
import { desktopPort, type DesktopPort, type DesktopEnvironmentPort, type DesktopCredentialsPort, type ResourceStatus } from '../product/desktop-port'

const section = ref('compute')
const resources = useWorkspaceResources()
const port = desktopPort() as DesktopPort & DesktopEnvironmentPort & DesktopCredentialsPort
const credentials = useStartupCredentials(port, () => resources.refreshInventory())
watch(() => resources.resources.value.find(item => item.resource_id === 'tts'), item => {
  credentials.setLocalTtsReady(item ? item.can_start === true && item.update_available !== true : null)
}, { immediate: true })
const starting = ref(false) // starting（已请求启动）：原生导航完成前防止重复点击。
const logExportError = ref('') // logExportError（日志打包错误）。
const logExportSuccess = ref(false) // logExportSuccess（日志包已保存）。
const updatesRequired = computed(() => resources.resources.value.some(item => item.required !== false && item.update_available === true))
const componentsReady = computed(() => resources.resources.value.length > 0
  && resources.resources.value.filter(item => item.required !== false).every(item => item.can_start && item.update_available !== true))
const componentBlockMessage = computed(() => updatesRequired.value
  ? '有组件需更新并通过校验后才能启动语音服务。'
  : '启动前组件校验未通过，请先下载或导入缺失组件。')
const tasksBusy = computed(() => resources.startingResources.value.size > 0
  || resources.resourceJobs.value.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state)))
const modelResourceIds = new Set(['kws-sherpa-onnx', 'paraformer-streaming', 'fsmn-vad', 'campplus-zh-cn-common', 'smart-turn', 'sensevoice-small', 'streaming-punctuation', 'tts-voxcpm1', 'tts-voxcpm2'])
const categoryByResource: Record<string, LocalComponentCategory> = {
  'kws-sherpa-onnx': 'extensions', 'paraformer-streaming': 'speech', 'sensevoice-small': 'speech',
  'smart-turn': 'speech', 'streaming-punctuation': 'speech', 'fsmn-vad': 'extensions',
  'campplus-zh-cn-common': 'extensions', dsh: 'inference', 'voice-server': 'voice-server',
}
const componentIdByResource: Record<string, string> = {
  'kws-sherpa-onnx': 'kws', 'paraformer-streaming': 'paraformer', 'sensevoice-small': 'sensevoice',
  'smart-turn': 'smart_turn', 'streaming-punctuation': 'punctuation', 'fsmn-vad': 'voice_vad',
  'campplus-zh-cn-common': 'campplus', dsh: 'dsh', 'voice-server': 'voice-system',
}

function resourceJobsFor(resourceId: string) { return resources.resourceJobs.value.filter(job => job.resource_id === resourceId || resourceId === 'voice-runtime-cpu' && job.resource_id === 'voice-server') }
function resourceIsBusy(resourceId: string) {
  return resources.startingResources.value.has(resourceId)
    || resources.pendingDownloadPlan.value?.resource_id === resourceId
    || resourceJobsFor(resourceId).some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state))
}
function toManagedResource(item: ResourceStatus): ManagedResourceRow {
  const failedJob = resourceJobsFor(item.resource_id).find(job => job.state === 'failed')
  return {
    id: item.resource_id,
    label: item.label,
    state: item.installation_state,
    status: item.update_available ? '需更新' : item.installation_state === 'installed' ? '已安装' : item.installation_state === 'incomplete' ? '不完整' : '缺失',
    updateAvailable: item.update_available === true,
    required: item.required !== false,
    installedVersion: item.installed_version,
    targetVersion: modelResourceIds.has(item.resource_id) ? item.version : undefined,
    error: item.error || failedJob?.error || undefined,
    retryable: Boolean(failedJob),
    busy: resourceIsBusy(item.resource_id),
  }
}
function resourceRow(item: ResourceStatus, category: LocalComponentCategory): LocalComponentRow {
  const managed = toManagedResource(item)
  const status = item.update_available ? '需更新' : item.installation_state === 'incomplete' ? '待修复' : item.files_installed ? item.runtime_ready ? '已就绪' : '运行环境待配置' : '资源未安装'
  return {
    id: componentIdByResource[item.resource_id] || item.resource_id,
    name: item.label,
    category,
    state: item.update_available ? 'failed' : item.can_start ? 'ready' : 'disabled',
    status,
    available: item.files_installed === true,
    runtime: '工作区独立组件',
    description: item.required === false ? '可选组件，缺失或待更新不阻塞启动；启用相关功能前需准备就绪。'
      : item.update_available ? '此组件需更新并通过校验后才能启动语音服务。'
        : item.resource_id === 'voice-server' ? '统一准备 Python（后端解释器）和语音运行依赖。'
          : item.resource_id === 'dsh' ? '下载或导入代理执行组件后使用。'
            : item.files_installed ? item.runtime_ready ? '模型文件和运行依赖已就绪。' : '模型文件已安装，请在 Voice Server（语音服务）配置运行依赖后启动。'
              : '下载或导入模型文件；运行依赖由 Voice Server（语音服务）统一配置。',
    facts: [
      { label: '组件要求', value: item.required === false ? '可选 · 不阻塞启动' : '必需' },
      { label: '文件状态', value: item.installation_state === 'installed' ? '完整' : item.installation_state === 'incomplete' ? '不完整' : '待安装' },
      ...(item.python ? [{ label: 'Python（后端解释器）', value: item.python }] : []),
      ...(item.resource_id === 'voice-server' && item.version ? [{ label: '解释器版本', value: item.version }] : []),
    ],
    resource: managed,
    runtimeDependencies: item.runtime_components?.map(toManagedResource),
  }
}
const model = computed<LocalComponentsModel>(() => {
  const inventory = resources.resources.value
  const findResource = (id: string) => inventory.find(item => item.resource_id === id)
  const knownRows = inventory.flatMap(item => {
    const category = categoryByResource[item.resource_id]
    if (!category || category === 'voice-server' || item.resource_id.startsWith('tts-') || item.resource_id === 'tts-audiocpp-runtime') return []
    return [resourceRow(item, category)]
  })
  const ttsResources = ['tts-voxcpm2', 'tts-voxcpm1'].map(findResource).filter((item): item is ResourceStatus => Boolean(item))
  const runtimeResource = findResource('tts-audiocpp-runtime')
  const minimaxReady = credentials.configured.value.minimax
  const ttsModelRows: LocalComponentRow[] = ttsResources.map((item): LocalComponentRow => {
    const model = toManagedResource(item)
    const runtimeReady = item.runtime_ready === true && runtimeResource?.can_start === true
    const ready = item.can_start === true && item.update_available !== true
    const installed = item.files_installed === true
    return {
      id: item.resource_id,
      name: item.label,
      category: 'tts',
      state: item.update_available ? 'failed' : ready ? 'ready' : 'disabled',
      status: item.update_available ? '需更新' : ready ? '已就绪'
        : installed && !runtimeReady ? '已安装，运行环境待配置'
          : item.installation_state === 'incomplete' ? '不完整' : '待安装',
      available: ready,
      runtime: '',
      description: `${item.label} 本地语音模型；需与共用 audio.cpp（原生音频推理程序）运行资源一并就绪。`,
      facts: [],
      resource: model,
      sharedResource: runtimeResource ? toManagedResource(runtimeResource) : undefined,
      note: 'VoxCPM1 与 VoxCPM2 共用 audio.cpp；下载不会自动切换当前播放后端。',
    }
  })
  const minimaxRow: LocalComponentRow = {
    id: 'minimax', name: 'MiniMax 在线 API（语音接口）', category: 'tts',
    state: credentials.loading.value ? 'loading' : minimaxReady ? 'ready' : 'failed',
    status: credentials.loading.value ? '读取配置中' : minimaxReady ? '已配置' : '待配置',
    available: minimaxReady, busy: credentials.loading.value, runtime: '',
    description: 'MiniMax 在线语音服务；API 密钥在详情中配置。启动程序后可查询套餐或账户用量。',
    facts: [], provider: credentials.providerSettings('minimax'),
  }
  const deepseekReady = credentials.configured.value.deepseek
  const deepseekRow: LocalComponentRow = {
    id: 'deepseek', name: 'DeepSeek 在线 API（推理接口）', category: 'inference',
    state: credentials.loading.value ? 'loading' : deepseekReady ? 'ready' : 'failed',
    status: credentials.loading.value ? '读取配置中' : deepseekReady ? '已配置' : '待配置',
    available: deepseekReady, busy: credentials.loading.value,
    runtime: '',
    description: '配置 DeepSeek 接口密钥并查询账户余额。密钥保存在本机系统凭据库，不会返回页面。',
    facts: [], provider: credentials.providerSettings('deepseek'),
  }
  const voiceServer = findResource('voice-server')
  const voiceRow: LocalComponentRow = voiceServer ? resourceRow(voiceServer, 'voice-server') : {
    id: 'voice-system', name: 'Voice Server（语音服务）', category: 'voice-server',
    state: 'disabled', status: '待配置', available: false, runtime: '本地系统服务',
    description: '统一准备 Python（后端解释器）和全部语音运行依赖。', facts: [],
  }
  const hotwordRow: LocalComponentRow = {
    id: 'hotwords', name: '热词修订', category: 'extensions',
    state: 'ready', status: '内置能力', available: true, runtime: '语音主服务内',
    description: '在实时及整句识别之后修订热词；复用语音主服务，不需要独立下载。', facts: [],
  }
  const rows = [...knownRows, ...ttsModelRows, minimaxRow, deepseekRow, hotwordRow, voiceRow]
  const failedJobs = resources.resourceJobs.value.filter(job => job.state === 'failed')
  return {
    status: '本地组件管理可用', state: 'ready',
    sampledAt: resources.inventoryUpdatedAt.value ? new Date(resources.inventoryUpdatedAt.value).toLocaleTimeString('zh-CN', { hour12: false }) + ' 更新' : '正在读取组件清单',
    resources: [],
    downloadResources: inventory.map(toManagedResource),
    resourceActionsAvailable: resources.resourcesCanManage.value,
    resourceLoading: resources.loading.value || resources.refreshing.value || resources.inventoryRefreshing.value || resources.preparingDownloadBundle.value,
    resourceTasksBusy: tasksBusy.value,
    resourceError: resources.resourceError.value || credentials.error.value || undefined,
    resourceEventError: resources.eventError.value || undefined,
    resourceBatchRetry: failedJobs.some(job => job.resource_id === 'batch'),
    managedResourceJobs: resources.resourceJobs.value.map(job => {
      const resource = inventory.find(item => item.resource_id === job.resource_id)
      return {
        id: job.job_id,
        resourceId: job.resource_id,
        label: job.resource_id === 'batch' ? '批量资源导入'
          : resource?.installation_state === 'installed' && resource.update_available ? `更新 ${resource.label}`
            : resource?.label || '资源任务',
        state: job.state, phase: job.phase, completedBytes: job.completed_bytes,
        totalBytes: job.total_bytes, downloadCompletedBytes: job.download_completed_bytes,
        downloadTotalBytes: job.download_total_bytes, verificationCompletedBytes: job.verification_completed_bytes,
        verificationTotalBytes: job.verification_total_bytes, error: job.error || undefined, canCancel: job.can_cancel === true,
      }
    }),
    rows,
  }
})

async function startProgram() {
  if (starting.value) return
  if (!componentsReady.value || tasksBusy.value) {
    resources.resourceError.value = updatesRequired.value
      ? '有组件需更新并通过校验后才能启动语音服务。'
      : '请先下载或导入缺失组件，全部组件完备后再启动。'
    section.value = 'compute'
    return
  }
  if (!credentials.ready.value) {
    credentials.error.value = credentials.localTts.value
      ? '请在“推理执行”分类的 DeepSeek 详情中填写接口密钥。'
      : '请配置 DeepSeek 接口密钥，并准备本地 TTS 模型或配置 MiniMax 在线 API。'
    section.value = 'compute'
    return
  }
  starting.value = true
  try { await port.startVoiceService() }
  catch (failure) { resources.resourceError.value = String(failure) }
  finally { starting.value = false }
}

function resourceBundleDownload(request: { title: string; entries: import('../interface/local-components').LocalDownloadItem[] }) {
  void resources.prepareDownloadBundle(request.entries, request.title)
}
function retry(id: string) {
  const row = model.value.rows.find(item => item.id === id)
  const resourceId = row?.resource?.id
  if (resourceId) void resources.downloadResource(resourceId)
}
function exportLogs() {
  logExportError.value = ''
  logExportSuccess.value = false
  void port.exportLogs().then(path => {
    logExportSuccess.value = Boolean(path)
  }).catch(error => {
    logExportError.value = String(error)
  })
}
</script>

<template>
  <main v-if="starting" class="startup-screen" role="status" aria-label="应用启动画面">
    <section class="startup-content"><img class="startup-icon" :src="startupIcon" alt="语音终端" width="72" height="72"><h1 class="startup-title">应用启动中</h1><p class="startup-status" aria-live="polite">正在准备启动并校验本地组件</p><div class="startup-progress" data-indeterminate="true" role="progressbar" aria-label="应用启动进度"><span class="startup-progress-fill"></span></div></section>
  </main>
  <AppFrame runtime="standby">
    <SettingsFrame :section="section" desktop @section="section = $event" @back="startProgram" @logs="exportLogs" @exit="port.exit()">
      <article class="settings-panel panel">
        <template v-if="section === 'compute' || section === 'system'">
          <div class="management-start">
            <p>{{ componentsReady ? credentials.ready.value ? '组件和接口密钥已完备，可以启动程序。' : '组件已完备，请在分类详情中配置 DeepSeek 与 TTS。' : componentBlockMessage }}</p>
            <div class="management-actions"><button class="outline-button start-program" :disabled="!componentsReady || !credentials.ready.value || tasksBusy || resources.loading.value || resources.inventoryRefreshing.value || starting" @click="startProgram">启动程序</button></div>
          </div>
          <LocalComponents :model="model" @refresh="resources.refresh" @retry="retry" @resource-download="resources.downloadResource" @resource-bundle-download="resourceBundleDownload" @resource-refresh="resources.refreshInventory" @resource-import="resources.importResource" @resource-import-all="resources.importResource()" @open-resource-source="resources.openResourceSource" @cancel-resource="resources.cancelJob" />
        </template>
        <WorkspaceSettings v-else-if="section === 'workspace'" :controller="resources" />
        <section v-else class="environment-required"><h2>请先补齐本地组件</h2><p>全部组件和接口密钥准备好后可以启动语音与业务服务。组件下载、导入和关于页可以在设置中使用。</p><button class="outline-button" @click="section = 'compute'">进入组件管理</button></section>
      </article>
    </SettingsFrame>
    <ResourceInstallDialog :controller="resources" />
    <template #overlays>
      <div v-if="logExportSuccess" class="save-notice" role="status">日志压缩包已保存到所选文件夹。</div>
      <div v-if="logExportError" class="save-notice error-notice" role="alert">
        <span>!</span>{{ logExportError }}<button aria-label="关闭错误" @click="logExportError = ''">×</button>
      </div>
    </template>
  </AppFrame>
</template>

<style scoped>.environment-required{padding:28px}.environment-required p{color:#99a4b7;line-height:1.8}.management-start{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}.management-start p{color:var(--muted);font-size:var(--font-body)}.management-start button{white-space:nowrap}.management-actions{display:flex;gap:8px}</style>

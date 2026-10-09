<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import AppFrame from '../interface/AppFrame.vue'
import SettingsFrame from '../interface/SettingsFrame.vue'
import LocalComponents from '../interface/LocalComponents.vue'
import FixedModelCredentials from '../interface/FixedModelCredentials.vue'
import SettingsPageHeader from '../interface/SettingsPageHeader.vue'
import { useStartupCredentials } from './useStartupCredentials'
import type { LocalComponentsModel } from '../interface/local-components'
import ResourceInstallDialog from '../product/panels/ResourceInstallDialog.vue'
import WorkspaceSettings from '../product/panels/WorkspaceSettings.vue'
import { useWorkspaceResources } from '../product/useWorkspaceResources'
import { desktopPort, type DesktopPort, type DesktopEnvironmentPort, type DesktopCredentialsPort } from '../product/desktop-port'
const section = ref('compute')
const resources = useWorkspaceResources()
const port = desktopPort() as DesktopPort & DesktopEnvironmentPort & DesktopCredentialsPort
const credentials = useStartupCredentials(port)
const componentsReady = computed(() => resources.resources.value.length > 0 && resources.resources.value.every(item => item.can_start))
const tasksBusy = computed(() => resources.startingResources.value.size > 0
  || resources.resourceJobs.value.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state)))
watch([componentsReady, credentials.loading], ([complete, loading]) => {
  if (complete && !loading && !credentials.ready.value && section.value === 'compute') section.value = 'models'
})
async function startProgram() {
  if (!componentsReady.value || tasksBusy.value) {
    resources.resourceError.value = '请先下载或导入缺失组件，全部组件完备后再启动。'
    section.value = 'compute'
    return
  }
  if (!credentials.ready.value) {
    credentials.error.value = credentials.disabled.value ? '接口配置正在处理，请稍后启动。' : '请先填写 DeepSeek（语言模型服务）和 MiniMax（语音合成服务）的接口密钥。'
    section.value = 'models'
    return
  }
  try { await port.startVoiceService() }
  catch (failure) { resources.resourceError.value = String(failure) }
}
const model = computed<LocalComponentsModel>(() => ({
  status: '本地组件管理可用', state: 'ready', sampledAt: '原生资源清单', resources: [],
  resourceActionsAvailable: resources.resourcesCanManage.value, resourceLoading: resources.loading.value,
  resourceTasksBusy: Boolean(resources.startingResources.value.size || resources.resourceJobs.value.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state))),
  resourceError: resources.resourceError.value, resourceEventError: resources.eventError.value,
  rows: resources.resources.value.map(item => ({ id: item.resource_id === 'voice-server' ? 'voice-system' : item.resource_id, name: item.label,
    state: item.can_start ? 'ready' : 'disabled', status: item.files_installed ? item.runtime_ready ? '已就绪' : '运行环境待配置' : '资源未安装',
    available: item.files_installed === true, runtime: '工作区独立组件', description: item.resource_id === 'voice-server' ? '统一准备全部语音运行依赖。' : item.resource_id === 'dsh' ? '下载或导入代理组件后使用。' : item.files_installed ? item.runtime_ready ? '模型文件和运行依赖已就绪。' : '模型文件已安装，请在 Voice Server（语音服务）配置运行依赖后启动。' : '下载或导入模型文件；运行依赖由 Voice Server（语音服务）统一配置。',
    facts: [{label:'文件状态',value:item.installation_state === 'installed' ? '完整' : '待安装'}, ...(item.python ? [{ label: 'Python（后端解释器）', value: item.python }] : []), ...(item.version ? [{ label: '解释器版本', value: item.version }] : [])],
    resource: {id:item.resource_id,label:item.label,state:item.installation_state,status:item.installation_state === 'installed' ? '已安装' : '缺失或不完整',error:item.error || undefined,
      busy:resources.startingResources.value.has(item.resource_id)},
  })),
  managedResourceJobs: resources.resourceJobs.value.map(job => ({id:job.job_id,resourceId:job.resource_id,label:job.resource_id === 'batch' ? '批量资源导入' : resources.resources.value.find(item => item.resource_id === job.resource_id)?.label || '资源任务',state:job.state,phase:job.phase,completedBytes:job.completed_bytes,totalBytes:job.total_bytes,error:job.error || undefined,canCancel:job.can_cancel === true})),
}))
</script>
<template>
  <AppFrame runtime="standby"><SettingsFrame :section="section" desktop @section="section = $event" @back="startProgram" @logs="port.openLogs()" @exit="port.exit()">
    <article class="settings-panel panel"><template v-if="section === 'compute' || section === 'system'"><div class="management-start"><p>{{ componentsReady ? credentials.ready.value ? '组件和接口密钥已完备，可以启动程序。' : '全部组件已完备，请先配置接口密钥再启动。' : '启动前组件校验未通过，请手动下载或导入缺失组件。下载可以并行进行。' }}</p><div class="management-actions"><button class="outline-button start-program" :disabled="!componentsReady || !credentials.ready.value || tasksBusy || resources.loading.value" @click="startProgram">启动程序</button><button class="outline-button" @click="section = 'models'">配置接口密钥</button></div></div><LocalComponents :model="model" @refresh="resources.refresh" @resource-download="resources.downloadResource" @resource-import="resources.importResource" @resource-import-all="resources.importResource()" @open-resource-source="resources.openResourceSource" @cancel-resource="resources.cancelJob" /></template>
      <section v-else-if="section === 'models'" class="startup-models"><SettingsPageHeader section="models"><template #actions><button class="outline-button" @click="section = 'compute'">查看组件下载</button><button class="outline-button start-program" :disabled="!componentsReady || !credentials.ready.value || tasksBusy || resources.loading.value" @click="startProgram">启动程序</button></template></SettingsPageHeader><p class="startup-models-hint">组件未就绪时也可以提前填写。组件和接口密钥完备后再启动程序。</p><p v-if="credentials.error.value" class="startup-credentials-error" role="alert">{{ credentials.error.value }}</p><p v-if="credentials.notice.value" class="startup-credentials-notice" role="status">{{ credentials.notice.value }}</p><FixedModelCredentials v-model:deepseek="credentials.deepseek.value" v-model:minimax="credentials.minimax.value" :configured="credentials.configured.value" :disabled="credentials.disabled.value" :saving="credentials.saving.value" startup @save="credentials.save" /></section>
      <WorkspaceSettings v-else-if="section === 'workspace'" :controller="resources" />
      <section v-else class="environment-required"><h2>请先补齐本地组件</h2><p>全部组件和接口密钥准备好后可以启动语音与业务服务。组件下载、导入、模型接口设置和关于页现在可以使用。</p><button class="outline-button" @click="section = 'compute'">进入组件管理</button></section>
    </article>
  </SettingsFrame><ResourceInstallDialog :controller="resources" /></AppFrame>
</template>
<style scoped>.environment-required,.startup-models{padding:28px}.environment-required p{color:#99a4b7;line-height:1.8}.management-start{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}.management-start p,.startup-models-hint{color:var(--muted);font-size:var(--font-body)}.management-start button{white-space:nowrap}.management-actions{display:flex;gap:8px}.startup-models-hint{margin:16px 0;line-height:1.8}.startup-credentials-error{color:var(--danger);margin:12px 0}.startup-credentials-notice{color:var(--mint);margin:12px 0}</style>

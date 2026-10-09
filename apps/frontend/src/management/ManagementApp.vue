<script setup lang="ts">
import { ref, computed } from 'vue'
import AppFrame from '../interface/AppFrame.vue'
import SettingsFrame from '../interface/SettingsFrame.vue'
import LocalComponents from '../interface/LocalComponents.vue'
import type { LocalComponentsModel } from '../interface/local-components'
import ResourceInstallDialog from '../product/panels/ResourceInstallDialog.vue'
import WorkspaceSettings from '../product/panels/WorkspaceSettings.vue'
import { useWorkspaceResources } from '../product/useWorkspaceResources'
import { desktopPort, type DesktopPort, type DesktopEnvironmentPort } from '../product/desktop-port'
const section = ref('compute')
const resources = useWorkspaceResources()
const port = desktopPort() as DesktopPort & DesktopEnvironmentPort
const model = computed<LocalComponentsModel>(() => ({
  status: '本地组件管理可用', state: 'ready', sampledAt: '原生资源清单', resources: [],
  resourceActionsAvailable: resources.resourcesCanManage.value, resourceLoading: resources.loading.value,
  resourceTasksBusy: Boolean(resources.startingResources.value.size || resources.resourceJobs.value.some(job => !['complete', 'completed', 'failed', 'cancelled', 'canceled'].includes(job.state))),
  resourceError: resources.resourceError.value, resourceEventError: resources.eventError.value,
  rows: resources.resources.value.map(item => ({ id: item.resource_id === 'voice-server' ? 'voice-system' : item.resource_id, name: item.label,
    state: item.can_start ? 'ready' : 'disabled', status: item.files_installed ? item.runtime_ready ? '已就绪' : '运行环境待配置' : '资源未安装',
    available: item.files_installed === true, runtime: '工作区独立组件', description: item.files_installed ? '文件已安装，可配置运行依赖后启动。' : '下载或导入固定资源，随后检查运行依赖。',
    facts: [{label:'文件状态',value:item.installation_state === 'installed' ? '完整' : '待安装'}, ...(item.python ? [{ label: 'Python（后端解释器）', value: item.python }] : []), ...(item.version ? [{ label: '解释器版本', value: item.version }] : [])],
    resource: {id:item.resource_id,label:item.label,state:item.installation_state,status:item.installation_state === 'installed' ? '已安装' : '缺失或不完整',error:item.error || undefined},
  })),
  managedResourceJobs: resources.resourceJobs.value.map(job => ({id:job.job_id,resourceId:job.resource_id,label:job.resource_id === 'batch' ? '批量资源导入' : resources.resources.value.find(item => item.resource_id === job.resource_id)?.label || '资源任务',state:job.state,phase:job.phase,completedBytes:job.completed_bytes,totalBytes:job.total_bytes,error:job.error || undefined,canCancel:job.can_cancel === true})),
}))
</script>
<template>
  <AppFrame runtime="standby"><SettingsFrame :section="section" desktop @section="section = $event" @back="port.startVoiceService()" @logs="port.openLogs()" @exit="port.exit()">
    <article class="settings-panel panel"><template v-if="section === 'compute' || section === 'system'"><LocalComponents :model="model" @refresh="resources.refresh" @resource-download="resources.downloadResource" @resource-import="resources.importResource" @resource-import-all="resources.importResource()" @open-resource-source="resources.openResourceSource" @cancel-resource="resources.cancelJob" /></template>
      <WorkspaceSettings v-else-if="section === 'workspace'" :controller="resources" />
      <section v-else class="environment-required"><h2>请先配置语音环境</h2><p>语音、业务设置、历史与任务管理需要 Voice Server（语音服务）。组件下载、导入和关于页现在可以使用。</p><button class="outline-button" @click="section = 'compute'">进入组件管理</button></section>
    </article>
  </SettingsFrame><ResourceInstallDialog :controller="resources" /></AppFrame>
</template>
<style scoped>.environment-required{padding:28px}.environment-required p{color:#99a4b7;line-height:1.8}</style>

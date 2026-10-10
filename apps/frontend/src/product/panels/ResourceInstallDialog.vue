<script setup lang="ts">
import { computed } from 'vue'
import type { useWorkspaceResources } from '../useWorkspaceResources'
const props = defineProps<{ controller: ReturnType<typeof useWorkspaceResources> }>()
const modelResourceIds = new Set(['kws-sherpa-onnx', 'paraformer-streaming', 'fsmn-vad', 'wespeaker-cnceleb-resnet34', 'smart-turn', 'sensevoice-small'])
const updatePending = computed(() => {
  const plan = props.controller.pendingDownloadPlan.value
  if (!plan) return false
  const resource = props.controller.resources.value.find(item => item.resource_id === plan.resource_id)
  return resource?.update_available === true
})
const voiceServerUpdatePending = computed(() => updatePending.value
  && props.controller.pendingDownloadPlan.value?.resource_id === 'voice-server')
const agentResourcePlan = computed(() => props.controller.pendingDownloadPlan.value?.resource_id === 'dsh')
const targetModelVersion = computed(() => {
  const plan = props.controller.pendingDownloadPlan.value
  if (!plan || !modelResourceIds.has(plan.resource_id)) return null
  return props.controller.resources.value.find(item => item.resource_id === plan.resource_id)?.version ?? null
})
const lightweightVadPlan = computed(() =>
  props.controller.pendingDownloadPlan.value?.environment?.group === 'onnx-vad',
)
</script>
<template>
  <div v-if="controller.pendingDownloadPlan.value" class="install-plan-backdrop">
    <section role="dialog" aria-modal="true" aria-labelledby="install-plan-title" class="install-plan panel">
      <h2 id="install-plan-title">{{ updatePending ? '准备更新' : '准备下载' }} {{ controller.pendingDownloadPlan.value.title }}</h2>
      <p v-if="controller.pendingDownloadPlan.value.model_bytes">固定资源最多 {{ (controller.pendingDownloadPlan.value.model_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）。完整且匹配的资源会跳过。</p>
      <p v-if="targetModelVersion">{{ updatePending ? '本次更新将安装目标模型版本' : '本次下载将安装当前目标模型版本' }}：{{ targetModelVersion }}。</p>
      <p v-if="!controller.pendingDownloadPlan.value.environment">{{ voiceServerUpdatePending ? '本次将更新语音运行环境依赖。' : agentResourcePlan ? `本次将${updatePending ? '更新' : '下载'}代理执行组件文件。` : `本次仅${updatePending ? '更新' : '下载'}模型文件。运行依赖请在 Voice Server（语音服务）中配置。` }}</p>
      <template v-if="controller.pendingDownloadPlan.value.environment">
        <p v-if="controller.pendingDownloadPlan.value.environment.already_ready">
          {{ lightweightVadPlan ? '轻量语音检测所需运行依赖已就绪，将检查并复用本地依赖。' : voiceServerUpdatePending ? '语音运行环境依赖已就绪，将按当前随包版本重新检查并复用本地依赖。' : '现用完整运行环境已就绪，将检测并复用已安装依赖。' }}
        </p>
        <template v-else>
          <p v-if="lightweightVadPlan">本次将补齐轻量语音检测所需运行依赖。优先复用符合条件的本地依赖，只下载缺失内容，最多 {{ (controller.pendingDownloadPlan.value.environment.total_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）。</p>
          <p v-else-if="voiceServerUpdatePending">本次将更新语音运行环境，并核对全部语音依赖的随包版本。优先复用符合条件的本地依赖，只下载缺失内容，最多 {{ (controller.pendingDownloadPlan.value.environment.total_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）。</p>
          <p v-else>一次检测并配置 Python（后端解释器）3.12.10 和全部语音依赖。优先复用符合条件的本地依赖，只下载缺失内容，最多 {{ (controller.pendingDownloadPlan.value.environment.total_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）。</p>
          <details>
            <summary>{{ controller.pendingDownloadPlan.value.environment.wheels.length }} 个运行依赖</summary>
            <p v-for="wheel in controller.pendingDownloadPlan.value.environment.wheels" :key="wheel.name">{{ wheel.name }}（依赖包） {{ wheel.version }}</p>
          </details>
        </template>
      </template>
      <div class="install-plan-actions">
        <button class="outline-button" @click="controller.confirmDownload">
          {{ controller.pendingDownloadPlan.value.environment ? updatePending ? (lightweightVadPlan ? '开始更新并补齐依赖' : '开始更新并配置') : (lightweightVadPlan ? '开始下载并补齐依赖' : '开始下载并配置') : updatePending ? '开始更新' : '开始下载' }}
        </button>
        <button class="outline-button" @click="controller.pendingDownloadPlan.value = null">返回</button>
      </div>
    </section>
  </div>
  <div v-if="controller.pendingImport.value" class="install-plan-backdrop"><section role="dialog" aria-modal="true" aria-label="确认导入内容" class="install-plan panel"><h2>已识别的资源</h2><p>整包文件与固定版本校验已通过。相同且完整的资源会跳过；导入未附带的运行依赖需要另行配置。</p><p v-for="item in controller.pendingImport.value.import_preview" :key="item.resource_id">{{ item.title }} · {{ item.version }}（固定版本）</p><div class="install-plan-actions"><button class="outline-button" @click="controller.confirmImport(true)">开始导入</button><button class="outline-button" @click="controller.confirmImport(false)">取消</button></div></section></div>
</template>
<style scoped>.install-plan-backdrop{position:fixed;inset:0;background:#000a;z-index:100;display:grid;place-items:center;padding:24px}.install-plan{padding:24px;max-width:640px;max-height:80vh;overflow:auto;width:100%}.install-plan p{font-size:12px;color:#a4aec0;line-height:1.8}.install-plan h2{font-size:17px}.install-plan-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}</style>

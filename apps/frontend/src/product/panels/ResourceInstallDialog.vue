<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { useWorkspaceResources } from '../useWorkspaceResources'
import type { LocalDownloadItem } from '../../interface/local-components'
const props = defineProps<{ controller: ReturnType<typeof useWorkspaceResources> }>()
const modelResourceIds = new Set(['kws-sherpa-onnx', 'paraformer-streaming', 'fsmn-vad', 'streaming-punctuation', 'campplus-zh-cn-common', 'smart-turn', 'sensevoice-small', 'tts-voxcpm1', 'tts-voxcpm2'])
const updatePending = computed(() => {
  const plan = props.controller.pendingDownloadPlan.value
  if (!plan) return false
  const resource = props.controller.resources.value.find(item => item.resource_id === plan.resource_id)
  return resource?.update_available === true
})
const voiceServerUpdatePending = computed(() => updatePending.value
  && ['voice-server', 'voice-runtime-cpu'].includes(props.controller.pendingDownloadPlan.value?.resource_id ?? ''))
const gpuDependencyPlan = computed(() => props.controller.pendingDownloadPlan.value?.resource_id === 'voice-runtime-gpu') // gpuDependencyPlan（可选显卡依赖安装）。
const agentResourcePlan = computed(() => props.controller.pendingDownloadPlan.value?.resource_id === 'dsh')
const targetModelVersion = computed(() => {
  const plan = props.controller.pendingDownloadPlan.value
  if (!plan || !modelResourceIds.has(plan.resource_id)) return null
  return props.controller.resources.value.find(item => item.resource_id === plan.resource_id)?.version ?? null
})
const lightweightVadPlan = computed(() =>
  props.controller.pendingDownloadPlan.value?.environment?.group === 'onnx-vad',
)
const bundlePlans = computed(() => props.controller.pendingDownloadBundlePlan.value?.plans ?? [])
const bundleEntries = computed(() => props.controller.pendingDownloadBundlePlan.value?.entries ?? [])
const selectedBundleIds = ref(new Set<string>()) // selectedBundleIds（确认下载的资源编号）。
watch(() => props.controller.pendingDownloadBundlePlan.value, bundle => {
  const entries = bundle?.entries ?? []
  selectedBundleIds.value = new Set((bundle?.plans ?? []).filter(plan => !entries.find(entry => entry.id === plan.resource_id)?.optional).map(plan => plan.resource_id))
}, { immediate: true })
const selectedBundlePlans = computed(() => bundlePlans.value.filter(plan => selectedBundleIds.value.has(plan.resource_id)))
const hasOptionalBundlePlans = computed(() => bundlePlans.value.some(plan => bundleEntries.value.find(entry => entry.id === plan.resource_id)?.optional))
const bundleOnlyBusy = computed(() => !bundlePlans.value.length && bundleEntries.value.some(entry => entry.busy))
const bundleEnvironment = computed(() => selectedBundlePlans.value.find(plan => plan.environment?.profile === 'cu128')?.environment
  ?? selectedBundlePlans.value.find(plan => plan.environment)?.environment ?? null)
const bundleSharedResources = computed(() => {
  const resources = new Map<string, { resource_id: string; title: string; download_bytes: number }>()
  for (const plan of selectedBundlePlans.value) {
    for (const resource of plan.shared_resources ?? []) resources.set(resource.resource_id, resource)
  }
  return [...resources.values()]
})
function planFor(entry: LocalDownloadItem) {
  return entry.id ? bundlePlans.value.find(plan => plan.resource_id === entry.id) : undefined
}
function toggleBundleResource(resourceId: string, checked: boolean) {
  const next = new Set(selectedBundleIds.value)
  if (checked) next.add(resourceId)
  else next.delete(resourceId)
  selectedBundleIds.value = next
}
function bundleEntryState(entry: LocalDownloadItem) {
  if (!entry.id) return entry.status || '无需下载'
  if (entry.busy) return '任务处理中'
  if (planFor(entry)) return selectedBundleIds.value.has(entry.id)
    ? entry.forcePlan && entry.state === 'installed' && !entry.updateAvailable ? '将准备共享运行程序' : entry.updateAvailable ? '将更新' : '将下载'
    : '可选，未选择'
  return entry.updateAvailable ? '等待更新' : entry.state === 'installed' ? '已就绪，跳过' : entry.status || '无需下载'
}
function confirmBundle() {
  void props.controller.confirmDownloadBundle([...selectedBundleIds.value])
}
</script>
<template>
  <div v-if="controller.pendingDownloadPlan.value" class="install-plan-backdrop">
    <section role="dialog" aria-modal="true" aria-labelledby="install-plan-title" class="install-plan panel">
      <h2 id="install-plan-title">{{ updatePending ? '准备更新' : '准备下载' }} {{ controller.pendingDownloadPlan.value.title }}</h2>
      <p v-if="controller.pendingDownloadPlan.value.model_bytes">固定资源最多 {{ (controller.pendingDownloadPlan.value.model_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）。完整且匹配的资源会跳过。</p>
      <p v-if="targetModelVersion">{{ updatePending ? '本次更新将安装目标模型版本' : '本次下载将安装当前目标模型版本' }}：{{ targetModelVersion }}。</p>
      <p v-for="shared in controller.pendingDownloadPlan.value.shared_resources" :key="shared.resource_id">先准备 {{ shared.title }}，最多 {{ (shared.download_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）。从固定官方归档下载并应用随包补丁；另一款模型可复用。</p>
      <p v-if="['tts-voxcpm1', 'tts-voxcpm2'].includes(controller.pendingDownloadPlan.value.resource_id)">本地模型依赖 Voice Server（语音服务）中的共享 audio.cpp（本地语音合成引擎）；此计划会先检查并准备运行依赖，再安装模型文件。默认参考音频由模型本体提供。</p>
      <p v-if="!controller.pendingDownloadPlan.value.environment && !controller.pendingDownloadPlan.value.resource_id.startsWith('tts-')">{{ voiceServerUpdatePending ? '本次将更新语音运行环境依赖。' : agentResourcePlan ? `本次将${updatePending ? '更新' : '下载'}代理执行组件文件。` : `本次仅${updatePending ? '更新' : '下载'}模型文件。运行依赖请在 Voice Server（语音服务）中配置。` }}</p>
      <template v-if="controller.pendingDownloadPlan.value.environment">
        <p v-if="gpuDependencyPlan">这是可选显卡运行依赖。将为现用语音环境安装配套的 PyTorch（张量框架）与 torchaudio（音频扩展）显卡版，继续复用已有模型；未安装时可继续使用 CPU（中央处理器）。安装后仍需通过本机显卡检查。</p>
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
  <div v-if="controller.pendingDownloadBundlePlan.value" class="install-plan-backdrop">
    <section role="dialog" aria-modal="true" aria-labelledby="install-bundle-title" class="install-plan panel">
      <h2 id="install-bundle-title">准备下载 {{ controller.pendingDownloadBundlePlan.value.title }}</h2>
      <p>清单包含本分类资源、在线配置项和内置能力。已安装资源会跳过；确认后所选资源会并行启动下载，共用运行环境由资源管理器协调与复用。</p>
      <ul class="install-bundle-list">
        <li v-for="entry in bundleEntries" :key="entry.id || entry.label">
          <label v-if="entry.optional && entry.id && planFor(entry)" class="install-bundle-choice">
            <input type="checkbox" :checked="selectedBundleIds.has(entry.id)" @change="toggleBundleResource(entry.id, ($event.target as HTMLInputElement).checked)">
            <strong>{{ entry.label }}</strong>
          </label>
          <strong v-else>{{ entry.label }}</strong>
          <span class="bundle-entry-state">{{ bundleEntryState(entry) }}</span>
          <span v-if="entry.note">{{ entry.note }}</span>
          <template v-if="planFor(entry)">
            <span v-if="entry.forcePlan && entry.state === 'installed' && !entry.updateAvailable">本地模型已就绪，此计划用于补齐缺失的共享运行程序。</span>
            <span v-else-if="planFor(entry)?.model_bytes">组件文件最多 {{ ((planFor(entry)?.model_bytes ?? 0) / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）</span>
            <span v-else>配置语音运行环境</span>
            <span v-for="shared in planFor(entry)?.shared_resources ?? []" :key="shared.resource_id">共用资源：{{ shared.title }}，最多 {{ (shared.download_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）</span>
          </template>
          <span v-if="entry.id && entry.busy">当前下载或修复任务会继续运行。</span>
        </li>
      </ul>
      <p v-for="shared in bundleSharedResources" :key="shared.resource_id">共用资源复用：{{ shared.title }}，最多 {{ (shared.download_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）。</p>
      <template v-if="bundleEnvironment">
        <p v-if="bundleEnvironment.already_ready">现有完整语音运行环境已就绪，将先核对并复用。</p>
        <template v-else>
          <p>本次会检查并准备共享语音环境，最多 {{ (bundleEnvironment.total_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节）；符合条件的已安装依赖会复用。</p>
          <details>
            <summary>{{ bundleEnvironment.wheels.length }} 个运行依赖</summary>
            <p v-for="wheel in bundleEnvironment.wheels" :key="wheel.name">{{ wheel.name }}（依赖包） {{ wheel.version }}</p>
          </details>
        </template>
      </template>
      <div class="install-plan-actions">
        <button class="outline-button" :disabled="!selectedBundlePlans.length" @click="confirmBundle">{{ selectedBundlePlans.length ? `开始下载 ${selectedBundlePlans.length} 项` : bundlePlans.length ? hasOptionalBundlePlans ? '请选择可选组件' : '暂无可下载资源' : bundleOnlyBusy ? '已有任务处理中' : '资源已就绪，无需下载' }}</button>
        <button class="outline-button" @click="controller.pendingDownloadBundlePlan.value = null">返回</button>
      </div>
    </section>
  </div>
  <div v-if="controller.pendingImport.value" class="install-plan-backdrop"><section role="dialog" aria-modal="true" aria-label="确认导入内容" class="install-plan panel"><h2>已识别的资源</h2><p>整包文件与固定版本校验已通过。相同且完整的资源会跳过；导入未附带的运行依赖需要另行配置。</p><p v-for="item in controller.pendingImport.value.import_preview" :key="item.resource_id">{{ item.title }} · {{ item.version }}（固定版本）</p><div class="install-plan-actions"><button class="outline-button" @click="controller.confirmImport(true)">开始导入</button><button class="outline-button" @click="controller.confirmImport(false)">取消</button></div></section></div>
</template>
<style scoped>.install-plan-backdrop{position:fixed;inset:0;background:#000a;z-index:100;display:grid;place-items:center;padding:24px}.install-plan{padding:24px;max-width:640px;max-height:80vh;overflow:auto;width:100%}.install-plan p{font-size:12px;color:#a4aec0;line-height:1.8}.install-plan h2{font-size:17px}.install-bundle-list{display:grid;gap:10px;padding:0;list-style:none}.install-bundle-list li{display:grid;gap:4px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-size:12px;color:#a4aec0;line-height:1.7}.install-bundle-list strong{font-size:13px;color:var(--text)}.install-bundle-choice{display:flex;align-items:center;gap:9px}.install-bundle-choice input{accent-color:var(--accent)}.bundle-entry-state{font-weight:600;color:var(--text)}.install-plan-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}</style>

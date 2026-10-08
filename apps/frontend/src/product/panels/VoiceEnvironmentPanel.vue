<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { desktopPort, RESOURCE_PROGRESS_EVENT, type DesktopPort, type DesktopWorkspacePort, type DesktopEnvironmentPort, type VoiceEnvironmentStatus, type EnvironmentPlan, type ResourceJob } from '../desktop-port'
const port = desktopPort() as DesktopPort & DesktopWorkspacePort & DesktopEnvironmentPort
const status = ref<VoiceEnvironmentStatus | null>(null)
const plan = ref<EnvironmentPlan | null>(null)
const error = ref(''), busy = ref(false), group = ref('voice-core')
const job = ref<ResourceJob | null>(null)
const active = computed(() => job.value && !['complete','failed','cancelled','canceled'].includes(job.value.state))
let unlisten: (() => void) | undefined
async function action(callback: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await callback() } catch (failure) { error.value = String(failure) } finally { busy.value = false }
}
async function refresh() {
  status.value = await port.environmentStatus()
  if (!job.value) job.value = status.value.jobs?.filter(item => ['voice-server', 'gpu-environment'].includes(item.resource_id)).at(-1) ?? null
}
async function registerGpu() { await port.registerGpu(); await refresh() }
const states: Record<string, string> = { running: '进行中', complete: '完成', failed: '失败', cancelled: '已取消', cancelling: '正在取消' }
async function preview() { plan.value = await port.environmentPlan(group.value) }
async function configure() { await port.configureEnvironment(group.value); plan.value = null }
onMounted(() => {
  void action(refresh)
  void port.listen<ResourceJob>(RESOURCE_PROGRESS_EVENT, event => {
    if (!['voice-server','gpu-environment'].includes(event.payload.resource_id)) return
    job.value = event.payload
    if (['complete','failed','cancelled'].includes(event.payload.state)) void refresh()
  }).then(dispose => { unlisten = dispose }).catch(failure => { error.value = String(failure) })
})
onBeforeUnmount(() => unlisten?.())
</script>
<template>
  <section class="voice-environment local-component-card" aria-labelledby="voice-environment-title">
    <div class="environment-heading"><div><h3 id="voice-environment-title">Voice Server（语音服务）</h3><p>{{ status?.service_ready ? '基础语音环境已就绪' : '先配置程序自己的 Python（后端环境），桌面管理功能已可使用。' }}</p></div><span>{{ status?.service_ready ? '已配置' : '待配置' }}</span></div>
    <p class="environment-path">{{ status?.python }}</p>
    <div class="environment-actions">
      <select v-model="group" aria-label="需要配置的共享运行依赖"><option value="voice-core">基础语音环境</option><option value="torch-cpu">识别 / 活动检测 / 精修依赖</option><option value="onnx-cpu">结束判断 / 声纹依赖</option><option value="sherpa-cpu">关键词唤醒依赖</option></select>
      <button class="outline-button" :disabled="busy || !!active" @click="action(preview)">配置</button>
      <button class="outline-button" :disabled="busy || !!active" @click="action(port.importEnvironment)">导入</button>
      <button class="outline-button" @click="action(port.environmentGuide)">说明</button>
      <button v-if="status?.service_ready" class="outline-button" :disabled="busy || !!active" @click="action(port.startVoiceService)">启动语音服务</button>
    </div>
    <div v-if="plan" class="environment-plan"><h4>安装内容</h4><p>CPython（官方解释器）3.12.10，Windows x64（64 位）。最多下载 {{ (plan.total_bytes / 1024 ** 2).toFixed(1) }} MiB（兆二进制字节），可复用内容会跳过。</p><p v-for="candidate in plan.system_candidates" :key="candidate.path">{{ candidate.accepted ? '可复用' : '不符合复用条件' }}：{{ candidate.path }}</p><details><summary>{{ plan.wheels.length }} 项固定依赖</summary><p v-for="wheel in plan.wheels" :key="wheel.name">{{ wheel.name }}（依赖包） {{ wheel.version }}</p></details><button class="outline-button" @click="action(configure)">开始配置</button><button class="outline-button" @click="plan = null">返回</button></div>
    <div v-if="job" class="environment-job" role="status"><p>{{ job.phase }} · {{ job.error || states[job.state] || '等待中' }}</p><progress v-if="job.total_bytes" :value="job.completed_bytes ?? 0" :max="job.total_bytes"></progress><button v-if="active && job.can_cancel" class="outline-button" @click="action(() => port.cancelResource(job!.job_id))">取消</button><button v-if="job.state === 'failed'" class="outline-button" @click="action(preview)">重试</button></div>
    <div class="environment-gpu"><h4>NVIDIA CUDA（英伟达显卡环境）</h4><p>{{ status?.gpu?.path || '尚未登记。请按说明在 runtimes/python-gpu（显卡环境目录）准备独立环境。' }}</p><div class="environment-actions"><button class="outline-button" :disabled="!!active" @click="action(registerGpu)">登记显卡环境</button><button class="outline-button" :disabled="!!active || !status?.gpu" @click="action(port.probeGpu)">重新检测</button><button class="outline-button" @click="action(refresh)">查看原因</button></div><ul v-if="status?.gpu"><li v-for="(capability, id) in status.gpu.models" :key="id">{{ id }}（模型） · {{ capability.gpu_available ? '实际显卡推理已通过' : capability.reason || '待检测' }}</li></ul></div>
    <p v-if="error" class="local-error" role="alert">{{ error }}</p>
  </section>
</template>
<style scoped>
.voice-environment{margin-bottom:20px;padding:20px}.environment-heading{display:flex;justify-content:space-between;gap:12px}.environment-heading h3{margin-top:0}.environment-heading p,.environment-path,.environment-gpu p{color:var(--muted,#96a3b8);font-size:12px;line-height:1.7;overflow-wrap:anywhere}.environment-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.environment-actions select{background:#202630;color:inherit;border:1px solid #3b4350;border-radius:8px;padding:8px;max-width:100%}.environment-plan,.environment-gpu{margin-top:18px;border-top:1px solid #343c50;padding-top:12px}.environment-plan p,.environment-gpu li,.environment-job p{font-size:12px;line-height:1.8}.environment-job progress{width:100%}.environment-plan button{margin:8px 8px 0 0}
</style>

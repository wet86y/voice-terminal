<script setup lang="ts">
import { useApplicationHost, applicationStateLabel } from './application-host'
const application = useApplicationHost()
const { managed, snapshot, error, connected } = application
const capabilityNames: Record<string, string> = {
  audio: '音频设备', kws: 'KWS（关键词唤醒）', asr: 'ASR（语音识别）', speaker: '声纹',
  llm: 'LLM（大语言模型）', tts: 'TTS（语音合成）', shadow_agent: '影子代理', web_search: '网络检索', analysis: '语音分析',
}
function bytes(value: number | null | undefined) {
  if (value == null) return '不可用'
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB（二进制吉字节）` : `${(value / 1024 ** 2).toFixed(1)} MiB（二进制兆字节）`
}
function uptime(value: number | null) {
  if (value == null) return '不可用'
  return `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分 ${Math.floor(value % 60)} 秒`
}
function date(value: number | null | undefined) {
  return value == null ? '不可用' : new Date(value * 1000).toLocaleString('zh-CN')
}
</script>

<template>
  <section class="application-system">
    <div class="application-heading"><div><h2>系统运行</h2><p>应用宿主统一管理各组件，关闭网页后应用继续运行。</p></div><button v-if="managed" type="button" @click="application.refresh">刷新状态</button></div>
    <p v-if="!managed" class="application-hint">当前页面使用独立后端调试模式。请通过独立启动程序打开页面，以查看整套系统的状态。</p>
    <template v-else>
      <div class="application-summary"><strong>{{ applicationStateLabel(snapshot?.state || 'starting') }}</strong><span>{{ snapshot?.phase || '连接启动程序' }}</span><span>{{ snapshot?.state === 'stopped' ? '连接已结束' : connected ? '实时更新' : '正在重新连接' }}</span></div>
      <div class="application-memory-summary">
        <div><span>当前常驻内存（工作集之和）</span><strong>{{ bytes(snapshot?.total_memory_rss_bytes) }}</strong><small>当前驻留物理内存的页面，包含共享页，不等于独占物理内存或正在执行任务的内存。</small></div>
        <div><span>私有提交内存之和</span><strong>{{ bytes(snapshot?.total_memory_private_commit_bytes) }}</strong><small>系统已承诺提供后备的私有内存，可能由物理内存或页面文件承载，不等于当前物理占用。</small></div>
      </div>
      <small>两项总量均按唯一受管进程去重，包含组件子进程。待机时工作集可回落，重新使用模型时可能升高。</small>
      <small v-if="snapshot && snapshot.total_memory_private_commit_bytes === undefined">当前启动程序尚未提供提交内存指标，更新启动程序并重启应用后可用。</small>
      <small>最近指标采样：{{ date(snapshot?.sampled_at) }}</small>
      <p v-if="error" role="alert" class="application-error">{{ error }}</p>
      <div class="application-component-list">
        <article v-for="component in snapshot?.components || []" :key="component.id" class="application-component" :class="component.state">
          <div class="application-component-title"><strong>{{ component.name }}</strong><span>{{ component.retrying ? '正在恢复' : applicationStateLabel(component.state) }}</span></div>
          <p>{{ component.phase }}</p><p v-if="component.error" class="application-error">{{ component.error }}</p>
          <div class="application-metrics"><span>PID（进程标识）<b>{{ component.pid ?? '未运行' }}</b></span><span>运行时长<b>{{ uptime(component.uptime_seconds) }}</b></span><span>常驻内存（工作集）<b>{{ bytes(component.memory_rss_bytes) }}</b></span><span>私有提交内存<b>{{ bytes(component.memory_private_commit_bytes) }}</b></span><span>CPU（中央处理器）<b>{{ component.cpu_percent == null ? '不可用' : component.cpu_percent.toFixed(1) + '%' }}</b></span></div>
          <small v-if="component.pid">进程创建时间：{{ date(component.created_at) }}</small>
          <small v-if="component.shared_with">与应用宿主共享进程，整体内存只计一次。</small><small v-if="component.external">外部调试进程，退出应用时不负责回收。</small>
          <div v-if="component.retry_allowed" class="application-retry"><span v-if="['asr', 'analysis', 'voice'].includes(component.id)">恢复会停止当前监听与播报，并重新连接语音业务。</span><button type="button" @click="application.retry(component.id)">重试{{ ['asr', 'analysis', 'voice'].includes(component.id) ? '并恢复语音服务' : '组件' }}</button></div>
        </article>
      </div>
      <div v-if="snapshot" class="application-capabilities"><h3>业务能力</h3><p>在线模型与语音合成展示能力状态，进程内存归属语音服务。</p><div v-for="(state, key) in snapshot.capabilities" :key="key"><span>{{ capabilityNames[key] || key }}</span><strong>{{ ({ READY: '已就绪', DISABLED: '已停用', ERROR: '不可用', DEGRADED: '降级', OFFLINE: '离线', STARTING: '启动中', STOPPED: '已停止' } as Record<string, string>)[state] || state }}</strong></div></div>
      <button class="application-exit" type="button" :disabled="snapshot?.stopping" @click="application.exit">退出整个应用</button>
    </template>
  </section>
</template>

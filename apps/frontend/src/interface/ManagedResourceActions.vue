<script setup lang="ts">
import type { ManagedResourceRow } from './local-components'

withDefaults(defineProps<{
  resource: ManagedResourceRow // resource（受管资源）：操作始终发送该资源编号。
  downloadLabel: string // downloadLabel（下载或更新文案）：包含失败后的重试状态。
  importAvailable?: boolean // importAvailable（当前目标是否提供本地导入操作）。
  sourceAvailable?: boolean // sourceAvailable（当前目标是否允许从此设备打开资源来源）。
  resetBlocked?: boolean // resetBlocked（其他组件任务运行时禁止重置）。
  loading?: boolean // loading（组件清单检查中）：暂时禁用安装操作。
}>(), { importAvailable: true })
const emit = defineEmits<{
  download: [id: string] // download（读取现有安装计划）。
  import: [id: string] // import（复用现有资源导入）。
  source: [id: string] // source（打开该资源的上游页面）。
}>()
</script>

<template>
  <div class="local-inline-resource-action managed-resource-buttons">
    <button class="outline-button" type="button" :aria-label="`${resource.label}：${downloadLabel}`" :disabled="resource.busy || loading" @click="emit('download', resource.id)">{{ resource.busy ? '处理中…' : downloadLabel }}</button>
    <button v-if="importAvailable !== false" class="outline-button" type="button" :aria-label="`${resource.label}：${resource.resetAvailable ? '重置' : '导入'}`" :disabled="resource.busy || loading || resource.resetAvailable && resetBlocked" @click="emit('import', resource.id)">{{ resource.resetAvailable ? '重置' : '导入' }}</button>
    <button v-if="sourceAvailable !== false" class="outline-button" type="button" :aria-label="`${resource.label}：跳转`" :disabled="loading" @click="emit('source', resource.id)">跳转</button>
    <small v-else class="resource-source-note">远程模式暂不支持从客户端打开服务端组件来源链接。</small>
  </div>
</template>

<style scoped>
.managed-resource-buttons{display:flex;align-items:center;justify-content:flex-end;gap:5px;flex-wrap:wrap}
.managed-resource-buttons button{min-height:var(--compact-control-height);padding:5px 8px;font-size:var(--font-caption);white-space:nowrap}
.resource-source-note{flex-basis:100%;color:var(--muted);font-size:var(--font-caption);line-height:1.5;text-align:right}
@container (max-width:440px){.managed-resource-buttons{justify-content:stretch;width:100%}.managed-resource-buttons button{flex:1}}
</style>

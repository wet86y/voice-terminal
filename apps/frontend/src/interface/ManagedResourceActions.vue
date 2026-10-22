<script setup lang="ts">
import type { ManagedResourceRow } from './local-components'

defineProps<{
  resource: ManagedResourceRow // resource（受管资源）：操作始终发送该资源编号。
  downloadLabel: string // downloadLabel（下载或更新文案）：包含失败后的重试状态。
  loading?: boolean // loading（组件清单检查中）：暂时禁用安装操作。
}>()
const emit = defineEmits<{
  download: [id: string] // download（读取现有安装计划）。
  import: [id: string] // import（复用现有资源导入）。
  source: [id: string] // source（打开该资源的上游页面）。
}>()
</script>

<template>
  <div class="local-inline-resource-action managed-resource-buttons">
    <button class="outline-button" type="button" :aria-label="`${resource.label}：${downloadLabel}`" :disabled="resource.busy || loading" @click="emit('download', resource.id)">{{ resource.busy ? '处理中…' : downloadLabel }}</button>
    <button class="outline-button" type="button" :aria-label="`${resource.label}：导入`" :disabled="resource.busy || loading" @click="emit('import', resource.id)">导入</button>
    <button class="outline-button" type="button" :aria-label="`${resource.label}：跳转`" :disabled="loading" @click="emit('source', resource.id)">跳转</button>
  </div>
</template>

<style scoped>
.managed-resource-buttons{display:flex;align-items:center;justify-content:flex-end;gap:5px;flex-wrap:wrap}
.managed-resource-buttons button{min-height:var(--compact-control-height);padding:5px 8px;font-size:var(--font-caption);white-space:nowrap}
@container (max-width:440px){.managed-resource-buttons{justify-content:stretch;width:100%}.managed-resource-buttons button{flex:1}}
</style>

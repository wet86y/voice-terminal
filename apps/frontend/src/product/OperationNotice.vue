<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { operationPhaseLabel, type RuntimeOperation } from '../runtime/runtime-operations'

const props = defineProps<{ operations: RuntimeOperation[] }>()
const rows = ref<RuntimeOperation[]>([])
const fading = ref(false)
const revisions = new Map<string, number>()
let timer: ReturnType<typeof setTimeout> | undefined
const active = computed(() => rows.value.some(row => row.state === 'running'))
const failed = computed(() => rows.value.some(row => row.state === 'failed'))
function clearTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined }
function dismissErrors() { rows.value = rows.value.filter(row => row.state === 'running') }
watch(() => props.operations, incoming => {
  let changed = false
  const next = new Map(rows.value.map(row => [row.id, row]))
  for (const row of incoming) {
    if ((revisions.get(row.id) ?? 0) >= row.revision) continue
    revisions.set(row.id, row.revision)
    // 首次读取的历史终态不重新弹出；活动操作可在页面刷新后恢复。
    if (row.state !== 'running' && !next.has(row.id)) continue
    if (row.state === 'running') {
      for (const [key, value] of next) if (value.state === 'succeeded' || value.state === 'cancelled') next.delete(key)
    }
    next.set(row.id, row)
    changed = true
  }
  for (const [key, row] of next) {
    if (row.state === 'running' && !incoming.some(value => value.id === key)) {
      next.set(key, { ...row, state: 'failed', detail: '服务状态已重建，请核对组件状态。' })
      changed = true
    }
  }
  if (!changed) return
  clearTimer()
  fading.value = false
  rows.value = [...next.values()]
  if (!active.value && !failed.value) {
    timer = setTimeout(() => {
      fading.value = true
      timer = setTimeout(() => { rows.value = []; timer = undefined }, 160)
    }, 300)
  }
  // 修订号只用于防重；后端终态也有有界保留，避免长期运行积累。
  if (revisions.size > 256) {
    for (const key of revisions.keys()) if (!next.has(key) && !incoming.some(row => row.id === key)) revisions.delete(key)
  }
}, { immediate: true, deep: true })
onBeforeUnmount(clearTimer)
</script>

<template>
  <Transition name="save-notice">
    <div v-if="rows.length" class="save-notice operation-notice" :class="{ 'is-fading': fading, 'operation-failed': failed }" role="status" aria-live="polite" :aria-busy="active">
      <span v-if="active" class="operation-spinner" aria-label="正在处理" />
      <span v-else aria-hidden="true">{{ failed ? '!' : '✓' }}</span>
      <div class="operation-content">
        <strong>{{ active ? '系统正在更新' : failed ? '操作未完成' : '系统已就绪' }}</strong>
        <div v-for="row in rows" :key="row.id" class="operation-row">{{ row.label }} · {{ row.state === 'cancelled' ? '已取消' : operationPhaseLabel(row) }}</div>
      </div>
      <button v-if="failed" aria-label="关闭操作错误" @click="dismissErrors">×</button>
    </div>
  </Transition>
</template>

<style scoped>
.operation-notice { align-items: flex-start; transition: opacity 160ms; }
.operation-content { display: grid; gap: 5px; min-width: 0; }
.operation-content strong { font-size: 12px; font-weight: 600; }
.operation-row { color: var(--muted); line-height: 1.6; }
.operation-spinner { width: 15px; height: 15px; flex: none; margin-top: 2px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: operation-spin .8s linear infinite; }
.operation-failed { border-color: #efa0b140; }
@keyframes operation-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .operation-spinner { animation-duration: 2s; } }
</style>

<script setup lang="ts">
import { computed, ref } from 'vue'
import SettingsPageHeader from './SettingsPageHeader.vue'
import type { LocalComponentRow, LocalComponentsModel } from './local-components'

const props = defineProps<{
  /** model（页面状态与指标）与 saving（保存中）。 */
  model: LocalComponentsModel
  saving?: boolean
}>()
const visibleResources = computed(() => props.model.resources.filter(resource => resource.used !== undefined))
const emit = defineEmits<{
  /** refresh（刷新指标）与 retry（重试指定组件）。 */
  refresh: []
  retry: [id: string]
  /** device（修改运行位置草稿）与 save（保存运行位置）。 */
  device: [id: string, value: string]
  save: [id: string]
}>()
const expanded = ref(new Set<string>())
function activate(row: LocalComponentRow) {
  if (!row.available) {
    if (row.retryable && !row.busy) emit('retry', row.id)
    return
  }
  if (expanded.value.has(row.id)) expanded.value.delete(row.id)
  else expanded.value.add(row.id)
}
function changeDevice(id: string, event: Event) {
  const value = (event.target as HTMLSelectElement).value
  const option = props.model.rows.find(row => row.id === id)?.device?.options.find(option => option.value === value)
  if (option && !option.disabled) emit('device', id, value)
}
function invalidDevice(row: LocalComponentRow) {
  return row.device?.options.find(option => option.value === row.device?.value)?.disabled
}
</script>

<template>
  <section class="local-components" aria-labelledby="local-components-title">
    <SettingsPageHeader section="compute" title-id="local-components-title">
      <template #actions>
        <button class="outline-button local-refresh" type="button" :disabled="model.refreshing" @click="emit('refresh')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 7A7 7 0 0 1 18 5l2 2M4 17l2 2a7 7 0 0 0 11.9-2" /></svg>
          {{ model.refreshing ? '刷新中' : '刷新' }}
        </button>
      </template>
    </SettingsPageHeader>

    <div class="local-overview">
      <div class="local-overview-line"><span class="local-state" :class="model.state"><i aria-hidden="true"></i>{{ model.status }}</span><small>最近采样：{{ model.sampledAt }}</small></div>
      <div v-if="visibleResources.length" class="local-memory-summary">
        <article v-for="resource in visibleResources" :key="resource.id" :data-resource="resource.id"><span>{{ resource.label }}</span><div class="local-resource-value" :aria-label="`${resource.label}，已用 ${resource.used}${resource.total ? `，全部 ${resource.total}` : ''}`"><strong>{{ resource.used }}</strong><span v-if="resource.total" class="local-resource-total">/ {{ resource.total }}</span></div></article>
      </div>
    </div>

    <p v-if="model.error" class="local-error" role="alert">{{ model.error }}</p>
    <div class="local-list-heading"><h3>模型与组件</h3><small>{{ model.rows.length }} 项</small></div>
    <div v-if="!model.rows.length" class="pool-empty">暂无本地组件信息。</div>
    <div v-else class="local-component-list">
      <article v-for="row in model.rows" :key="row.id" class="local-component-card" :class="row.state" :data-component="row.id">
        <div class="local-component-summary">
          <div class="local-component-body">
            <div class="local-component-identity"><h4 :title="row.name">{{ row.name }}</h4><span class="local-state" :class="row.state"><i aria-hidden="true"></i>{{ row.status }}</span></div>
            <p v-if="row.state === 'loading'" class="local-component-phase" aria-live="polite">{{ row.description }}</p>
            <dl class="local-component-metrics">
              <div v-if="row.runtime && row.runtime !== '未提供'" class="local-runtime-metric" data-metric="runtime"><dt>运行时</dt><dd>{{ row.runtime }}</dd></div>
              <div class="local-resource-metrics">
                <div v-if="row.residentMemory !== undefined || row.privateMemory !== undefined" data-metric="memory"><dt>{{ row.residentMemory !== undefined && row.privateMemory !== undefined ? '常驻内存／提交内存' : row.residentMemory !== undefined ? '常驻内存' : '提交内存' }}</dt><dd>{{ [row.residentMemory, row.privateMemory].filter(value => value !== undefined).join(' / ') }}</dd></div>
                <div v-if="row.vram !== undefined" data-metric="vram"><dt>显存</dt><dd>{{ row.vram }}</dd></div>
              </div>
            </dl>
            <p v-if="row.error && !row.busy" class="local-error">{{ row.error }}</p>
          </div>
          <button class="local-component-action" :class="{ 'retry-action': !row.available }" type="button" :disabled="row.busy || (!row.available && !row.retryable)" :aria-label="`${row.name}：${row.available ? expanded.has(row.id) ? '收起详情' : '详情' : '重试'}`" :aria-expanded="row.available ? expanded.has(row.id) : undefined" :aria-controls="row.available ? `local-detail-${row.id}` : undefined" @click="activate(row)">
            {{ row.available ? expanded.has(row.id) ? '收起' : '详情' : '重试' }}
            <svg v-if="row.available" viewBox="0 0 24 24" aria-hidden="true" :class="{ expanded: expanded.has(row.id) }"><path d="m6 9 6 6 6-6" /></svg>
          </button>
        </div>
        <div v-if="row.available && expanded.has(row.id)" :id="`local-detail-${row.id}`" class="local-component-detail">
          <p>{{ row.description }}</p>
          <dl v-if="row.facts.length" class="local-detail-facts"><div v-for="fact in row.facts" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ fact.value }}</dd></div></dl>
          <div v-if="row.device" class="local-device-editor">
            <label :for="`local-device-${row.id}`"><span>运行位置</span><select :id="`local-device-${row.id}`" :value="row.device.value" :disabled="saving || row.device.locked" @change="changeDevice(row.id, $event)"><option v-for="option in row.device.options" :key="option.value" :value="option.value" :disabled="option.disabled">{{ option.label }}</option></select></label>
            <button class="save-button" type="button" :disabled="saving || row.device.locked || invalidDevice(row)" @click="emit('save', row.id)">{{ saving ? '保存中' : '保存' }}</button>
            <small>实际运行：{{ row.device.current }}<span v-if="row.device.draft"> · 草稿尚未保存</span><span v-if="row.device.pending"> · 已保存配置下次启动生效</span></small>
          </div>
          <div v-else class="local-location"><span>运行位置</span><strong>{{ row.location || '本机' }}</strong></div>
          <small v-if="row.device?.hint || row.note" class="local-detail-note">{{ [row.device?.hint, row.note].filter(Boolean).join(' ') }}</small>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.local-component-phase{margin:6px 0 0;color:var(--muted);font-size:var(--font-caption);line-height:1.6}
.local-components{display:flex;flex-direction:column;gap:20px;min-width:0;container-type:inline-size}
.local-refresh{display:inline-flex;align-items:center;gap:6px;flex-shrink:0;min-height:var(--compact-control-height);padding:6px 10px;font-size:var(--font-body)}
svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.local-overview{display:grid;gap:12px}
.local-overview-line,.local-list-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
small,dt{font-size:var(--font-caption)}
.local-state{display:inline-flex;align-items:center;gap:5px;font-size:var(--font-caption);color:var(--muted);white-space:nowrap}
.local-state i{width:5px;height:5px;flex-shrink:0;border-radius:50%;background:currentColor}
.local-state.ready{color:var(--mint)}
.local-state.loading{color:var(--accent)}
.local-state.failed{color:var(--danger)}
.local-memory-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.local-memory-summary article{display:flex;flex-direction:column;gap:10px;padding:14px 16px;border:1px solid var(--line-strong);border-radius:12px;background:#ffffff02;min-width:0}
.local-memory-summary article>span{font-size:var(--font-caption);color:var(--muted);line-height:1.5}
.local-resource-value{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;font-variant-numeric:tabular-nums}
.local-resource-value strong{font-size:18px;line-height:1.3;white-space:nowrap}
.local-resource-total{font-size:var(--font-body);color:var(--muted);white-space:nowrap}
.local-list-heading h3{font-size:var(--font-section)}
.local-component-list{display:grid;gap:10px}
.local-component-card{min-width:0;border:1px solid var(--line);border-radius:12px;background:#ffffff02;overflow:hidden}
.local-component-card.failed{border-color:color-mix(in srgb,var(--danger) 28%,transparent)}
/* action column（操作列）：按钮文案或箭头变化不挤动名称与状态列。 */
.local-component-summary{--local-action-width:64px;display:grid;grid-template-columns:minmax(0,1fr) var(--local-action-width);gap:12px;align-items:center;padding:12px 16px}
.local-component-body{min-width:0}
/* name column（名称列）：统一预留宽度，使状态不随名称长度移动。 */
.local-component-identity{display:flex;justify-content:space-between;gap:12px;align-items:start;margin-bottom:8px}
.local-component-identity h4{font-size:var(--font-body);line-height:1.5;overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.local-component-identity>.local-state{line-height:18px;flex:none;justify-content:flex-end;text-align:right}
dl,dd{margin:0}
.local-component-metrics{display:grid;grid-template-columns:minmax(100px,1fr) auto;align-items:start;gap:8px 16px}
.local-runtime-metric,.local-resource-metrics>div{min-width:0;display:flex;flex-direction:column;gap:3px}
.local-resource-metrics{display:flex;justify-content:flex-end;gap:16px;text-align:right;min-width:0}
.local-resource-metrics dd{white-space:nowrap}
.local-component-metrics dt{white-space:nowrap}
.local-detail-facts>div{min-width:0;display:flex;flex-direction:column;gap:4px}
dt{color:var(--subtle)}
dd{font-size:var(--font-body);font-variant-numeric:tabular-nums;overflow-wrap:anywhere;line-height:1.5}
.local-component-action{display:inline-flex;align-self:center;align-items:center;justify-content:center;gap:4px;min-height:var(--compact-control-height);width:100%;min-width:0;padding:6px 10px;font-size:var(--font-body);background:#ffffff03}
.local-component-action svg{transition:transform 180ms}
.local-component-action svg.expanded{transform:rotate(180deg)}
.retry-action{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 30%,transparent)}
.local-error{color:var(--danger);font-size:var(--font-caption);overflow-wrap:anywhere;white-space:pre-wrap}
.local-component-body>.local-error{margin-top:10px}
.local-component-detail{display:grid;gap:14px;padding:16px;border-top:1px solid var(--line);background:#0000000b}
.local-component-detail>p{color:var(--muted);font-size:var(--font-body);overflow-wrap:anywhere}
.local-detail-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px}
.local-device-editor{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;align-items:end;max-width:420px}
.local-device-editor label{display:grid;gap:6px;min-width:0}
.local-device-editor label>span,.local-location>span{font-size:var(--font-caption);color:var(--muted)}
.local-device-editor select{font-size:var(--font-body);min-height:var(--compact-control-height);height:var(--compact-control-height);padding:5px 10px}
.local-device-editor button{min-height:var(--compact-control-height);height:var(--compact-control-height);padding:5px 14px;font-size:var(--font-body)}
.local-device-editor>small{grid-column:1/-1}
.local-detail-note{overflow-wrap:anywhere}
.local-location{display:flex;gap:12px;align-items:center;font-size:var(--font-body)}
@container (max-width:440px){.local-memory-summary{grid-template-columns:1fr;gap:8px}.local-memory-summary article{flex-direction:row;align-items:center;justify-content:space-between;padding:12px;gap:8px}.local-component-summary{--local-action-width:58px;padding:12px;gap:8px}.local-component-metrics{gap:6px}.local-component-detail{padding:12px}.local-component-action{padding-inline:8px}}
@container (max-width:520px){.local-component-metrics{grid-template-columns:1fr}.local-resource-metrics{gap:12px}.local-resource-metrics dt,.local-resource-metrics dd{font-size:var(--font-caption)}}
@media (prefers-reduced-motion:reduce){.local-component-action svg{transition:none}}
</style>

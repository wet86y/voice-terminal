<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { LocalProviderSettings } from './local-components'

const props = defineProps<{
  model: LocalProviderSettings
  /** refreshKey（密钥更新计数）：用于成功保存后重新查询用量。 */
  refreshKey?: number
}>()
const configured = ref(props.model.configured) // configured（本机是否已有已保存密钥）。
const checking = ref(false) // checking（正在读取账户用量）。
const result = ref<Record<string, unknown> | null>(null) // result（服务商返回的公开统计，不含密钥）。
const error = ref('') // error（用量查询错误）。
const name = computed(() => props.model.provider === 'deepseek' ? 'DeepSeek' : 'MiniMax')
// usagePayload（账户用量主体）：兼容服务端的 provider/data（服务商/账户数据）响应结构。
const usagePayload = computed<Record<string, unknown> | null>(() => {
  const data = result.value?.data
  return data && typeof data === 'object' && !Array.isArray(data)
    ? data as Record<string, unknown>
    : result.value
})
const billingMode = computed(() => usagePayload.value?.billing_mode) // billingMode（识别出的计费方式）。
const billingLabel = computed(() => billingMode.value === 'token_plan' ? 'Token Plan（订阅套餐）'
  : billingMode.value === 'pay_as_you_go' ? '按量计费 API'
    : props.model.provider === 'deepseek' ? '账户余额' : '套餐／按量用量') // billingLabel（计费方式中文名称）。
const usageNote = computed(() => typeof usagePayload.value?.usage_note === 'string' ? usagePayload.value.usage_note : '') // usageNote（服务商用量说明）。

// usageLabels（用量字段中文名）：服务商常见字段使用中文说明，未知字段也显示中文前缀。
const usageLabels: Record<string, string> = {
  is_available: '账户余额可用于接口调用', balance_infos: '余额明细', currency: '币种',
  total_balance: '可用余额', granted_balance: '赠送余额', topped_up_balance: '充值余额',
  base_resp: '服务商接口状态', status_code: '状态码', status_msg: '状态说明', data: '用量数据',
  model_remains: '模型余量', model_name: '模型名称', tokens_remains: '套餐剩余 Token（词元）',
  token_remains: '剩余 Token（词元）', current_interval_usage_count: '五小时已用次数',
  current_interval_total_count: '五小时总次数', current_interval_remains: '五小时剩余量',
  current_interval_reset_at: '五小时额度重置时间', current_weekly_usage_count: '本周已用次数',
  current_weekly_total_count: '本周总次数', current_weekly_remains: '本周剩余量',
  current_weekly_reset_at: '周额度重置时间', remain: '剩余量', remains: '剩余量',
  remaining: '剩余量', total: '总量', used: '已使用', usage: '用量', limit: '额度',
  five_hour_usage: '近五小时用量', five_hour_remains: '五小时剩余量',
  weekly_usage: '本周用量', weekly_remains: '本周剩余量', reset_at: '重置时间',
  expire_time: '到期时间', expires_at: '到期时间', start_time: '开始时间', end_time: '结束时间', window: '用量周期',
  billing_mode: '计费方式', usage_supported: '支持查询用量', usage_note: '用量说明',
}

function displayValue(value: unknown) {
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

const usageRows = computed(() => { // usageRows（转成中文标签的用量明细）。
  if (!usagePayload.value) return []
  const rows: Array<{ label: string; value: string }> = []
  const ignoredMetadata = new Set(['billing_mode', 'usage_supported', 'usage_note'])
  const visit = (value: unknown, path: string[], key?: string) => {
    if (value === null || value === undefined || key && ignoredMetadata.has(key)) return
    if (Array.isArray(value)) {
      value.forEach((item, index) => visit(item, [...path, `第 ${index + 1} 项`]))
      return
    }
    if (typeof value === 'object') {
      for (const [childKey, item] of Object.entries(value as Record<string, unknown>)) {
        visit(item, [...path, usageLabels[childKey] || `其他信息（${childKey}）`], childKey)
      }
      return
    }
    rows.push({ label: path.join(' · '), value: displayValue(value) })
  }
  visit(usagePayload.value, [])
  return rows.slice(0, 48)
})
const weeklyQuotaMissing = computed(() => billingMode.value === 'token_plan'
  && !usageRows.value.some(row => /本周|周额度|weekly/i.test(row.label)))

async function checkUsage() {
  if (!props.model.usage || checking.value || !configured.value) return
  checking.value = true
  error.value = ''
  try { result.value = await props.model.usage() }
  catch (failure) {
    result.value = null
    error.value = failure instanceof Error ? failure.message : '账户用量读取失败。'
  } finally { checking.value = false }
}

watch(() => props.model.configured, value => {
  configured.value = value
  if (value && props.model.usage && !result.value && !checking.value) void checkUsage()
})
onMounted(() => { if (props.model.usage && configured.value) void checkUsage() })
watch(() => props.refreshKey, () => {
  result.value = null
  if (props.model.usage && configured.value) void checkUsage()
})
</script>

<template>
  <section class="provider-usage-summary" :data-provider="model.provider" :aria-label="`${name}账户额度`">
    <div class="provider-usage-heading">
      <div><strong>{{ model.provider === 'deepseek' ? '账户余额' : '账户额度' }}</strong><span v-if="result" class="provider-billing-mode">{{ billingLabel }}</span></div>
      <button v-if="model.usage" class="outline-button" type="button" :disabled="!configured || checking || model.disabled" @click="checkUsage">{{ checking ? '查询中…' : result ? '刷新额度' : '查询额度' }}</button>
    </div>
    <p v-if="!configured" class="provider-usage-note">先在详情中配置 API Key（接口密钥）。</p>
    <p v-else-if="model.usageHint && !model.usage" class="provider-usage-note">{{ model.usageHint }}</p>
    <p v-if="error" class="provider-usage-error" role="alert">{{ error }}</p>
    <dl v-if="usageRows.length" class="provider-usage-values"><div v-for="(row, index) in usageRows" :key="`${row.label}-${index}`"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div></dl>
    <p v-else-if="result && usageNote" class="provider-usage-note" role="status">{{ usageNote }}</p>
    <p v-else-if="result" class="provider-usage-note" role="status">{{ model.provider === 'deepseek' ? '服务商暂未返回可展示的账户余额字段。' : '服务商已返回用量状态，当前套餐未提供可展示的额度字段。' }}</p>
    <p v-if="result && weeklyQuotaMissing" class="provider-usage-note">当前套餐未返回周额度字段。</p>
    <p v-if="result && usageRows.length && usageNote" class="provider-usage-note">{{ usageNote }}</p>
  </section>
</template>

<style scoped>
.provider-usage-summary{display:grid;gap:9px;min-width:0;margin-top:8px;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:#ffffff02}
.provider-usage-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.provider-usage-heading>div{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.provider-usage-heading strong{font-size:var(--font-caption)}
.provider-billing-mode{font-size:var(--font-caption);color:var(--muted)}
.provider-usage-heading .outline-button{min-height:var(--compact-control-height);padding:4px 9px;font-size:var(--font-caption)}
.provider-usage-values{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:7px}
.provider-usage-values>div{display:grid;gap:4px;min-width:0;padding:8px 9px;border:1px solid var(--line);border-radius:9px}
.provider-usage-values dt{font-size:var(--font-caption);color:var(--muted);line-height:1.4}
.provider-usage-values dd{font-size:var(--font-body);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.provider-usage-note,.provider-usage-error{font-size:var(--font-caption);line-height:1.6;overflow-wrap:anywhere}
.provider-usage-note{color:var(--muted)}.provider-usage-error{color:var(--danger)}
</style>

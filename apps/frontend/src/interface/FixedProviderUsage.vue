<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { LocalProviderSettings } from './local-components'

/** UsageRow（用量摘要项）：label（中文显示名称）与 value（服务端实际数值）。 */
type UsageRow = { label: string; value: string }
/** UsageRecord（服务商用量记录）：来自余额或套餐接口的单条公开数据。 */
type UsageRecord = Record<string, unknown>

const props = defineProps<{
  model: LocalProviderSettings
  /** refreshKey（密钥更新计数）：用于成功保存后重新查询用量。 */
  refreshKey?: number
}>()
const configured = ref(props.model.configured) // configured（本机是否已有已保存密钥）。
const checking = ref(false) // checking（正在读取账户用量）。
const result = ref<Record<string, unknown> | null>(null) // result（服务商返回的公开统计，不含密钥）。
const error = ref('') // error（用量查询错误）。
// usagePayload（用量数据主体）：兼容接口外层包装和直接返回统计对象两种形状。
const usagePayload = computed<UsageRecord | null>(() => {
  const data = result.value?.data
  return data && typeof data === 'object' && !Array.isArray(data)
    ? data as UsageRecord
    : result.value
})
const billingMode = computed(() => usagePayload.value?.billing_mode) // billingMode（计费方式）：由服务端按密钥类型自动识别。
const billingLabel = computed(() => billingMode.value === 'token_plan' ? 'Token Plan（订阅套餐）'
  : billingMode.value === 'pay_as_you_go' ? '按量计费'
    : undefined)

function record(value: unknown): value is UsageRecord {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function firstValue(source: UsageRecord, keys: string[]): unknown {
  for (const key of keys) {
    const value = source[key]
    if (value !== null && value !== undefined && value !== '') return value
  }
  return undefined
}

function textValue(value: unknown): string | undefined {
  if (typeof value === 'string' && value.trim()) return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return undefined
}

function displayNumber(value: unknown): string | undefined {
  const text = textValue(value)
  if (!text) return undefined
  const number = Number(text)
  if (!Number.isFinite(number)) return text
  return number.toLocaleString('zh-CN', { maximumFractionDigits: 6 })
}

function isZero(value: unknown) {
  const text = textValue(value)
  return text !== undefined && Number.isFinite(Number(text)) && Number(text) === 0
}

const deepseekBalances = computed<UsageRow[]>(() => {
  const payload = usagePayload.value
  if (!payload) return []
  const records = Array.isArray(payload.balance_infos)
    ? payload.balance_infos.filter(record)
    : [payload]
  const item = records.find(candidate => firstValue(candidate, [
    'available_balance', 'total_balance', 'balance', 'remain_balance',
    'granted_balance', 'bonus_balance', 'gift_balance',
  ]) !== undefined)
  if (!item) return []
  const currency = textValue(item.currency)
  const suffix = currency ? ` · ${currency}` : ''
  const rows: UsageRow[] = []
  const balance = firstValue(item, ['available_balance', 'total_balance', 'balance', 'remain_balance'])
  const bonus = firstValue(item, ['granted_balance', 'bonus_balance', 'gift_balance'])
  const balanceText = displayNumber(balance)
  const bonusText = displayNumber(bonus)
  if (balanceText !== undefined) rows.push({ label: `余额${suffix}`, value: balanceText })
  if (bonusText !== undefined && !isZero(bonus)) rows.push({ label: `赠金余额${suffix}`, value: bonusText })
  return rows
})

function usageWindow(item: UsageRecord, window: 'interval' | 'weekly') {
  const keys = window === 'interval'
    ? ['current_interval_remaining_percent', 'current_interval_remains_percent', 'five_hour_remaining_percent']
    : ['current_weekly_remaining_percent', 'current_weekly_remains_percent', 'weekly_remaining_percent']
  const percent = displayNumber(firstValue(item, keys))
  if (percent !== undefined) return `${percent}%`

  const usageKeys = window === 'interval'
    ? ['current_interval_usage_count', 'five_hour_usage_count', 'five_hour_usage']
    : ['current_weekly_usage_count', 'weekly_usage_count', 'weekly_usage']
  const totalKeys = window === 'interval'
    ? ['current_interval_total_count', 'five_hour_total_count']
    : ['current_weekly_total_count', 'weekly_total_count']
  const used = displayNumber(firstValue(item, usageKeys))
  const total = displayNumber(firstValue(item, totalKeys))
  if (used !== undefined && total !== undefined) return `${used} / ${total} 次`

  const remainKeys = window === 'interval'
    ? ['current_interval_remains', 'current_interval_remaining', 'five_hour_remains']
    : ['current_weekly_remains', 'current_weekly_remaining', 'weekly_remains']
  const remains = displayNumber(firstValue(item, remainKeys))
  return remains
}

function accountBalance(source: UsageRecord | null) {
  if (!source) return undefined
  const records = Array.isArray(source.balance_infos)
    ? source.balance_infos.filter(record)
    : [source]
  const item = records.find(candidate => firstValue(candidate, [
    'available_balance', 'total_balance', 'balance', 'remain_balance',
  ]) !== undefined)
  if (!item) return undefined
  const balance = displayNumber(firstValue(item, [
    'available_balance', 'total_balance', 'balance', 'remain_balance',
  ]))
  if (balance === undefined) return undefined
  const currency = textValue(item.currency)
  return currency ? `${balance} ${currency}` : balance
}

const minimaxUsage = computed<UsageRow[]>(() => {
  const payload = usagePayload.value
  if (!payload) return []
  const source = Array.isArray(payload.model_remains)
    ? payload.model_remains.filter(record)
    : [payload]
  const rows = source.flatMap((item, index) => {
    const name = textValue(firstValue(item, ['model_name', 'model', 'name'])) || `套餐 ${index + 1}`
    const periods = [
      ['五小时', usageWindow(item, 'interval')],
      ['周', usageWindow(item, 'weekly')],
    ].filter((entry): entry is [string, string] => Boolean(entry[1]))
    if (!periods.length) {
      const remainingTokens = displayNumber(firstValue(item, ['tokens_remains', 'token_remains']))
      return remainingTokens === undefined ? [] : [{ label: name, value: `${remainingTokens} Token（词元）` }]
    }
    return [{ label: name, value: periods.map(([label, value]) => `${label} ${value}`).join(' · ') }]
  })

  if (!rows.length) {
    const remainingTokens = displayNumber(firstValue(payload, ['tokens_remains', 'token_remains']))
    if (remainingTokens !== undefined) return [{ label: '套餐', value: `${remainingTokens} Token（词元）` }]
  }
  return rows
})

const minimaxBalance = computed(() => billingMode.value === 'pay_as_you_go' ? accountBalance(usagePayload.value) : undefined)
const minimaxSummary = computed(() => billingMode.value === 'pay_as_you_go'
  ? minimaxBalance.value
  : minimaxUsage.value.map(row => `${row.label}：${row.value}`).join(' · '))
const minimaxSummaryLabel = computed(() => billingMode.value === 'pay_as_you_go' ? '余额' : '限额')
const minimaxSummaryTitle = computed(() => [billingLabel.value, minimaxSummary.value].filter(Boolean).join(' · '))

const notice = computed(() => {
  if (!configured.value) return '配置 API Key（接口密钥）后显示用量。'
  if (!props.model.usage && props.model.usageHint) return props.model.usageHint
  if (error.value) return error.value
  if (!result.value) return ''
  if (props.model.provider === 'minimax' && billingMode.value === 'pay_as_you_go' && !minimaxBalance.value) {
    return '按量计费：余额和账单请在 MiniMax 控制台查看。'
  }
  const hasRows = props.model.provider === 'deepseek' ? deepseekBalances.value.length > 0 : Boolean(minimaxSummary.value)
  if (!hasRows) return props.model.provider === 'deepseek' ? '服务商暂未返回余额字段。' : '服务商暂未返回可显示的套餐额度。'
  return ''
})

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
  <section class="provider-usage-summary" :data-provider="model.provider" :aria-label="`${model.provider === 'deepseek' ? 'DeepSeek 余额' : 'MiniMax 套餐用量'}`">
    <dl v-if="model.provider === 'deepseek' && deepseekBalances.length" class="provider-usage-values">
      <div v-for="(row, index) in deepseekBalances" :key="`${row.label}-${index}`"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div>
    </dl>
    <dl v-else-if="model.provider === 'minimax' && minimaxSummary" class="provider-usage-values provider-minimax-summary" :title="minimaxSummaryTitle">
      <div><dt>{{ minimaxSummaryLabel }}</dt><dd>{{ minimaxSummary }}</dd></div>
    </dl>
    <span v-if="notice" class="provider-usage-note" :class="{ error: error }" :title="notice" role="status">{{ notice }}</span>
    <button v-if="model.usage" class="outline-button provider-usage-refresh" type="button" :disabled="!configured || checking || model.disabled" @click="checkUsage">
      {{ checking ? '查询中…' : result ? '刷新' : '查询' }}
    </button>
  </section>
</template>

<style scoped>
.provider-usage-summary{display:flex;align-items:center;justify-content:flex-end;gap:5px 8px;min-width:0;max-width:100%;flex-wrap:nowrap;overflow:hidden;white-space:nowrap}
.provider-billing-mode,.provider-usage-note{min-width:0;font-size:var(--font-caption);line-height:1.45;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.provider-usage-note.error{color:var(--danger)}
.provider-usage-values{display:flex;align-items:center;gap:5px 12px;flex:1 1 auto;min-width:0;margin:0;overflow:hidden;white-space:nowrap}
.provider-usage-values>div{display:inline-flex;align-items:baseline;gap:5px;min-width:0;font-size:var(--font-caption);white-space:nowrap;overflow:hidden}
.provider-usage-values dt{flex:none;max-width:45%;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.provider-usage-values dd{min-width:0;font-size:var(--font-caption);font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.provider-minimax-summary>div{flex:1 1 auto}
.provider-usage-refresh{flex:none;height:var(--compact-control-height);min-height:var(--compact-control-height);padding:3px 8px;font-size:var(--font-caption)}
</style>

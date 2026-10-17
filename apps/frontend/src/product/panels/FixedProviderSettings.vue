<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { LocalProviderSettings } from '../../interface/local-components'

const props = defineProps<{ model: LocalProviderSettings }>()
const configured = ref(props.model.configured) // configured（本机是否已有密钥）。
const draft = ref('') // draft（只保存在当前页面内存的密钥草稿）。
const saving = ref(false) // saving（正在保存密钥）。
const checking = ref(false) // checking（正在读取账户用量）。
const result = ref<Record<string, unknown> | null>(null) // result（不含密钥的服务商响应）。
const saveError = ref('') // saveError（保存密钥时的错误）。
const usageError = ref('') // usageError（查询账户用量时的错误）。
const notice = ref('') // notice（保存结果说明）。
const name = computed(() => props.model.provider === 'deepseek' ? 'DeepSeek' : 'MiniMax')
const usageLabel = computed(() => props.model.provider === 'deepseek' ? '查询账户余额' : '查询 Token Plan（订阅计划）余量')
const usageNote = computed(() => props.model.provider === 'deepseek'
  ? '通过服务端查询 DeepSeek 官方余额接口。'
  : '此接口仅显示 MiniMax Token Plan（订阅计划）余量；普通按量付费账户请在 MiniMax 控制台查看。')
watch(() => props.model.configured, value => {
  configured.value = value
  if (value && props.model.usage && !result.value && !checking.value) void checkUsage()
})

// usageLabels（用量字段中文名）：服务商常见字段使用中文说明，未知字段也以中文前缀展示。
const usageLabels: Record<string, string> = {
  is_available: '账户余额可用于接口调用', balance_infos: '余额明细', currency: '币种',
  total_balance: '可用余额', granted_balance: '赠送余额', topped_up_balance: '充值余额',
  base_resp: '服务商接口状态', status_code: '状态码', status_msg: '状态说明', data: '用量数据',
  model_remains: '模型余量', model_name: '模型名称', tokens_remains: '套餐剩余 Token（词元）',
  token_remains: '剩余 Token（词元）', current_interval_usage_count: '当前周期已用次数',
  current_interval_total_count: '当前周期总次数', remain: '剩余量', remains: '剩余量',
  remaining: '剩余量', total: '总量', used: '已使用', usage: '用量', limit: '额度',
  five_hour_usage: '近五小时用量', weekly_usage: '本周用量', reset_at: '重置时间',
  expire_time: '到期时间', expires_at: '到期时间', start_time: '开始时间', end_time: '结束时间', window: '用量周期',
}

const usageRows = computed(() => {
  if (!result.value) return []
  const rows: Array<{ label: string; value: string }> = []
  const visit = (value: unknown, path: string[]) => {
    if (value === null || value === undefined) return
    if (Array.isArray(value)) {
      value.forEach((item, index) => visit(item, [...path, `第 ${index + 1} 项`]))
      return
    }
    if (typeof value === 'object') {
      for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
        visit(item, [...path, usageLabels[key] || `其他信息（${key}）`])
      }
      return
    }
    const display = typeof value === 'boolean' ? value ? '是' : '否' : String(value)
    rows.push({ label: path.join(' · '), value: display })
  }
  visit(result.value, [])
  return rows.slice(0, 48)
})

async function save() {
  if (saving.value || props.model.disabled || !draft.value.trim()) return
  saving.value = true
  saveError.value = ''
  notice.value = ''
  usageError.value = ''
  try {
    const response = await props.model.save(draft.value)
    configured.value = response.configured
    draft.value = ''
    result.value = null
    notice.value = response.message || '接口密钥已安全保存。'
  } catch (failure) {
    saveError.value = failure instanceof Error ? failure.message : '接口密钥保存失败。'
  } finally {
    saving.value = false
  }
}

async function checkUsage() {
  if (!props.model.usage || checking.value || !configured.value) return
  checking.value = true
  usageError.value = ''
  notice.value = ''
  try { result.value = await props.model.usage() }
  catch (failure) {
    result.value = null
    usageError.value = failure instanceof Error ? failure.message : '账户用量读取失败。'
  } finally { checking.value = false }
}
onMounted(() => { if (props.model.usage && configured.value) void checkUsage() })
</script>

<template>
  <section class="provider-settings-detail" :data-provider="model.provider" :aria-label="`${name}（在线服务）设置`">
    <div class="provider-key-settings">
      <div class="provider-settings-heading">
        <div><h4>{{ name }}（在线服务）</h4><p>密钥保存在本机系统凭据库，不会返回到页面。</p></div>
        <span class="provider-key-state" :class="configured ? 'configured' : 'missing'">{{ model.disabled ? '正在读取' : configured ? '已配置' : '未配置' }}</span>
      </div>
      <label class="form-field"><span>{{ name }} API Key（接口密钥）</span><input v-model="draft" type="password" autocomplete="new-password" spellcheck="false" :disabled="model.disabled || saving" :placeholder="configured ? '已配置，填写可更新' : '填写接口密钥'" /></label>
      <div class="provider-settings-actions"><small>留空保留已有密钥；{{ model.provider === 'deepseek' ? '保存后供推理服务使用。' : '保存后可作为语音合成的在线方式。' }}</small><button class="save-button" type="button" :disabled="model.disabled || saving || !draft.trim()" @click="save">{{ saving ? '正在保存…' : '保存密钥' }}</button></div>
      <p v-if="notice" class="provider-settings-message notice" role="status">{{ notice }}</p>
      <p v-if="saveError" class="provider-settings-message error" role="alert">{{ saveError }}</p>
    </div>
    <div class="provider-usage-settings">
      <div class="provider-settings-heading"><div><h4>账号用量</h4><p>{{ model.usage ? usageNote : model.usageHint || '启动应用后可查询账户用量。' }}</p></div><button v-if="model.usage" class="outline-button" type="button" :disabled="!configured || checking || model.disabled" @click="checkUsage">{{ checking ? '查询中…' : usageLabel }}</button></div>
      <p v-if="usageError" class="provider-settings-message error" role="alert">{{ usageError }}</p>
      <dl v-if="usageRows.length" class="provider-usage-values"><div v-for="(row, index) in usageRows" :key="`${row.label}-${index}`"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div></dl>
      <p v-else-if="result" class="provider-settings-message" role="status">服务商已返回账户用量，当前响应中没有可展示的明细字段。</p>
    </div>
  </section>
</template>

<style scoped>
.provider-settings-detail{display:grid;gap:18px;width:100%;min-width:0}.provider-key-settings,.provider-usage-settings{display:grid;gap:12px;padding:16px;border:1px solid var(--line);border-radius:14px;background:#ffffff03}.provider-settings-heading,.provider-settings-actions{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}.provider-settings-heading>div{display:grid;gap:4px;min-width:0}.provider-settings-heading h4{font-size:var(--font-body)}.provider-settings-heading p,.provider-settings-actions small{font-size:var(--font-caption);line-height:1.6;color:var(--muted)}.provider-key-state{font-size:var(--font-caption);color:var(--muted)}.provider-key-state.configured{color:var(--mint)}.provider-key-state.missing{color:var(--accent)}.provider-key-settings>.form-field{max-width:520px}.provider-settings-actions .save-button{min-height:var(--compact-control-height);padding:5px 12px;font-size:var(--font-body)}.provider-usage-settings .outline-button{min-height:var(--compact-control-height);padding:5px 10px;font-size:var(--font-caption)}.provider-usage-values{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}.provider-usage-values>div{display:grid;gap:5px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;min-width:0}.provider-usage-values dt{font-size:var(--font-caption);color:var(--muted)}.provider-usage-values dd{font-size:var(--font-body);overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.provider-settings-message{font-size:var(--font-caption);line-height:1.6}.provider-settings-message.error{color:var(--danger)}.provider-settings-message.notice{color:var(--mint)}
</style>

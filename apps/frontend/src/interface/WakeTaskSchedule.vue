<script setup lang="ts" generic="Rule extends 'at' | 'after' | 'daily' | 'weekly' | 'every' | 'cron'">
import { computed } from 'vue'
import DatePicker from './DatePicker.vue'
import TimePicker from './TimePicker.vue'
import WeekdayPicker from './WeekdayPicker.vue'
import { validWakeTriggerCount, wakeIntervalSeconds } from './wake-task-interval'

const props = withDefaults(defineProps<{
  /** ruleKind（重复规则）：页面可选规则；cron 仅为内部类型，不在此控件暴露。 */
  ruleKind: Rule
  /** when（单次触发日期时间）：本地日期与时间。 */
  when: string
  /** time（每天或每周的触发时间）：可保留原有的秒数。 */
  time: string
  /** weekdays（每周触发日期）：1 至 7 对应周一至周日，逗号分隔。 */
  weekdays: string
  /** intervalHours（间隔小时数）：与分钟合成固定间隔。 */
  intervalHours?: string
  /** intervalMinutes（间隔分钟数）：与小时合成固定间隔。 */
  intervalMinutes?: string
  /** loop（是否持续循环）：关闭后显示总触发次数。 */
  loop?: boolean
  /** triggerCount（总触发次数）：包含第一次触发。 */
  triggerCount?: string
  /** disabled（保存期间禁用）。 */
  disabled?: boolean
}>(), {
  intervalHours: '1',
  intervalMinutes: '0',
  loop: true,
  triggerCount: '1',
})
const emit = defineEmits<{
  'update:ruleKind': [value: typeof props.ruleKind]
  'update:when': [value: string]
  'update:time': [value: string]
  'update:weekdays': [value: string]
  'update:intervalHours': [value: string]
  'update:intervalMinutes': [value: string]
  'update:loop': [value: boolean]
  'update:triggerCount': [value: string]
}>()
const selectedDays = computed(() => props.weekdays.split(',').filter(value => value.trim()).map(Number).filter(value => Number.isInteger(value) && value >= 1 && value <= 7))
const weekdays = computed({ get: () => props.weekdays, set: value => emit('update:weekdays', value) }) // weekdays（星期选择绑定）：沿用原有任务草稿字段。
const single = computed(() => ['at', 'after'].includes(props.ruleKind))
const validInterval = computed(() => wakeIntervalSeconds(props.intervalHours, props.intervalMinutes) !== null)
const validTriggerCount = computed(() => validWakeTriggerCount(props.triggerCount))
const intervalTime = computed({
  get: () => {
    const hours = props.intervalHours.trim()
    const minutes = props.intervalMinutes.trim()
    if (!/^\d+$/u.test(hours) || !/^\d+$/u.test(minutes)) return ''
    const hourValue = Number(hours)
    const minuteValue = Number(minutes)
    if (hourValue > 24 || minuteValue > 59 || (hourValue === 24 && minuteValue !== 0)) return ''
    return `${String(hourValue).padStart(2, '0')}:${String(minuteValue).padStart(2, '0')}`
  },
  // intervalTime（统一时间控件绑定）：拆回已有小时、分钟字段，保存逻辑继续复用原接口适配。
  set: value => {
    const match = /^(\d{2}):(\d{2})$/u.exec(value)
    if (!match) return
    emit('update:intervalHours', String(Number(match[1])))
    emit('update:intervalMinutes', String(Number(match[2])))
  },
})
const date = computed({
  get: () => props.when.split('T')[0] || '',
  set: value => emit('update:when', `${value}T${props.when.split('T')[1] || '09:00'}`),
})
const singleTime = computed({
  get: () => props.when.split('T')[1]?.slice(0, 5) || '',
  set: value => emit('update:when', `${date.value}T${value}${/^\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?$/.test(props.when.split('T')[1] || '') ? props.when.split('T')[1].slice(5) : ''}`),
})
const recurringTime = computed({
  get: () => props.time.slice(0, 5),
  // 秒数在已有任务中保持原值；时间面板仍以小时和分钟展示。
  set: value => emit('update:time', `${value}${/^\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?$/.test(props.time) ? props.time.slice(5) : ''}`),
})
function changeRule(event: Event) {
  const rule = (event.target as HTMLSelectElement).value as typeof props.ruleKind
  if (['daily', 'weekly'].includes(rule) && (single.value || !/^\d{1,2}:\d{2}/.test(props.time))) emit('update:time', singleTime.value || '09:00')
  if (rule === 'at' && ['daily', 'weekly'].includes(props.ruleKind) && /^\d{2}:\d{2}/.test(props.time)) emit('update:when', `${date.value}T${recurringTime.value}`)
  if (rule === 'weekly' && !selectedDays.value.length) {
    const chosen = new Date(`${date.value}T12:00:00`)
    emit('update:weekdays', String(Number.isNaN(chosen.getTime()) ? 1 : (chosen.getDay() + 6) % 7 + 1))
  }
  emit('update:ruleKind', rule)
}
</script>

<template>
  <div class="wake-task-schedule" :class="[`schedule-${ruleKind}`, { 'schedule-single': single }]">
    <label class="form-field task-rule-field"><span>重复规则</span><select :value="ruleKind === 'after' ? 'at' : ruleKind" aria-label="重复规则" :disabled="disabled" @change="changeRule"><option value="daily">每天</option><option value="weekly">每周</option><option value="at">单次</option><option value="every">间隔</option></select></label>
    <label v-if="single" class="form-field task-date-field"><span>触发日期</span><DatePicker v-model="date" label="触发日期" :disabled="disabled" /></label>
    <div v-if="ruleKind === 'weekly'" class="form-field task-weekdays-field"><span>星期</span><WeekdayPicker v-model="weekdays" :disabled="disabled" /></div>
    <label v-if="ruleKind === 'at' || ruleKind === 'after'" class="form-field task-time-field"><span>触发时间</span><TimePicker v-model="singleTime" label="触发时间" :disabled="disabled" /></label>
    <label v-if="ruleKind === 'daily' || ruleKind === 'weekly'" class="form-field task-time-field"><span>触发时间</span><TimePicker v-model="recurringTime" label="触发时间" :disabled="disabled" /></label>
    <label v-if="ruleKind === 'every'" class="form-field task-time-field"><span>间隔时间</span><TimePicker v-model="intervalTime" label="间隔时间" allow24 :invalid="!validInterval" :disabled="disabled" /></label>
    <div v-if="ruleKind === 'every'" class="form-field task-loop-field">
      <span>循环</span>
      <label class="instruction-toggle" :title="loop ? '关闭循环' : '开启循环'"><input type="checkbox" role="switch" aria-label="循环" :checked="loop" :disabled="disabled" @change="emit('update:loop', ($event.target as HTMLInputElement).checked)" /><i aria-hidden="true"></i></label>
    </div>
    <label v-if="ruleKind === 'every' && !loop" class="form-field task-trigger-count-field"><span>触发次数</span><input :value="triggerCount" type="number" min="1" step="1" inputmode="numeric" aria-label="触发次数" :aria-invalid="!validTriggerCount" :disabled="disabled" @input="emit('update:triggerCount', ($event.target as HTMLInputElement).value)" /></label>
  </div>
</template>

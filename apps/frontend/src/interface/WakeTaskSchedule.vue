<script setup lang="ts" generic="Rule extends 'at' | 'after' | 'daily' | 'weekly' | 'every' | 'cron'">
import { computed } from 'vue'
import DatePicker from './DatePicker.vue'
import TimePicker from './TimePicker.vue'
import WeekdayPicker from './WeekdayPicker.vue'

const props = defineProps<{
  /** ruleKind（重复规则）：单次、每天、每周，以及已有任务的高级规则。 */
  ruleKind: Rule
  /** when（单次触发日期时间）：本地日期与时间。 */
  when: string
  /** time（每天或每周的触发时间）：可保留原有的秒数。 */
  time: string
  /** weekdays（每周触发日期）：1 至 7 对应周一至周日，逗号分隔。 */
  weekdays: string
  /** disabled（保存期间禁用）。 */
  disabled?: boolean
  /** advanced（保留已有固定间隔和定时表达式的编辑入口）。 */
  advanced?: boolean
}>()
const emit = defineEmits<{
  'update:ruleKind': [value: typeof props.ruleKind]
  'update:when': [value: string]
  'update:time': [value: string]
  'update:weekdays': [value: string]
}>()
const selectedDays = computed(() => props.weekdays.split(',').filter(value => value.trim()).map(Number).filter(value => Number.isInteger(value) && value >= 1 && value <= 7))
const weekdays = computed({ get: () => props.weekdays, set: value => emit('update:weekdays', value) }) // weekdays（星期选择绑定）：沿用原有任务草稿字段。
const single = computed(() => !['daily', 'weekly', 'cron'].includes(props.ruleKind))
const date = computed({
  get: () => props.when.split('T')[0] || '',
  set: value => emit('update:when', `${value}T${singleTime.value || '09:00'}`),
})
const singleTime = computed({
  get: () => props.when.split('T')[1]?.slice(0, 5) || '',
  set: value => emit('update:when', `${date.value}T${value}`),
})
const recurringTime = computed({
  get: () => props.time.slice(0, 5),
  // 秒数在已有任务中保持原值；时间面板仍以小时和分钟展示。
  set: value => emit('update:time', `${value}${/^\d{2}:\d{2}:\d{2}$/.test(props.time) ? props.time.slice(5) : ''}`),
})
function changeRule(event: Event) {
  const rule = (event.target as HTMLSelectElement).value as typeof props.ruleKind
  if (['daily', 'weekly'].includes(rule) && (single.value || !/^\d{1,2}:\d{2}/.test(props.time))) emit('update:time', singleTime.value || '09:00')
  if (['at', 'every'].includes(rule) && !single.value && /^\d{2}:\d{2}/.test(props.time)) emit('update:when', `${date.value}T${recurringTime.value}`)
  if (rule === 'weekly' && !selectedDays.value.length) {
    const chosen = new Date(`${date.value}T12:00:00`)
    emit('update:weekdays', String(Number.isNaN(chosen.getTime()) ? 1 : (chosen.getDay() + 6) % 7 + 1))
  }
  emit('update:ruleKind', rule)
}
</script>

<template>
  <div class="wake-task-schedule" :class="[`schedule-${ruleKind}`, { 'schedule-single': single }]">
    <label class="form-field task-rule-field"><span>重复规则</span><select :value="ruleKind === 'after' ? 'at' : ruleKind" aria-label="重复规则" :disabled="disabled" @change="changeRule"><option value="daily">每天</option><option value="weekly">每周</option><option value="at">单次</option><template v-if="advanced"><option value="every">固定间隔</option><option value="cron">定时表达式</option></template></select></label>
    <label v-if="single" class="form-field task-date-field"><span>触发日期</span><DatePicker v-model="date" label="触发日期" :disabled="disabled" /></label>
    <div v-if="ruleKind === 'weekly'" class="form-field task-weekdays-field"><span>星期</span><WeekdayPicker v-model="weekdays" :disabled="disabled" /></div>
    <label v-if="ruleKind !== 'cron'" class="form-field task-time-field"><span>触发时间</span><TimePicker v-if="single" v-model="singleTime" label="触发时间" :disabled="disabled" /><TimePicker v-else v-model="recurringTime" label="触发时间" :disabled="disabled" /></label>
    <slot v-if="ruleKind === 'cron'" name="cron" />
    <slot v-if="ruleKind === 'every'" name="interval" />
  </div>
</template>

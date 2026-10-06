<script setup lang="ts">
import { computed, nextTick, ref, useId, watch } from 'vue'
import { containPickerWheel } from './picker-scroll'

const props = defineProps<{
  /** modelValue（日期值）：使用 YYYY-MM-DD（年-月-日）格式保存。 */
  modelValue: string
  /** label（控件名称）：用于输入框与日历的无障碍说明。 */
  label: string
  /** disabled（是否禁用）。 */
  disabled?: boolean
}>()
const emit = defineEmits<{
  /** update:modelValue（更新日期值）。 */
  'update:modelValue': [value: string]
}>()
const pad = (value: number) => String(value).padStart(2, '0')
const dateValue = (date: Date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
function parseDate(value: string) {
  const match = value.trim().match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/)
  if (!match) return null
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3])
  const date = new Date(year, month - 1, day, 12)
  return year >= 1000 && year <= 9999 && date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null
}
const today = () => { const date = new Date(); date.setHours(12, 0, 0, 0); return date }
const current = computed(() => parseDate(props.modelValue) ?? today()) // current（当前有效日期）。
const display = computed(() => parseDate(props.modelValue) ? dateValue(current.value).replaceAll('-', '/') : '')
const draft = ref(display.value) // draft（直接输入的日期文字）。
const editing = ref(false), opened = ref(false) // editing（输入中）、opened（日历已展开）。
const month = ref(current.value), focused = ref(dateValue(current.value)) // month（展示月份）、focused（键盘所在日期）。
const field = ref<HTMLInputElement>(), trigger = ref<HTMLButtonElement>(), panel = ref<HTMLElement>()
const instance = useId(), panelId = `date-picker-${instance}`, monthId = `${panelId}-month`
const anchorName = `--date-picker-${instance}` // anchorName（日历定位锚点）。
const weekdays = ['一', '二', '三', '四', '五', '六', '日']
const monthTitle = computed(() => `${month.value.getFullYear()}年${month.value.getMonth() + 1}月`)
const weeks = computed(() => {
  const first = new Date(month.value.getFullYear(), month.value.getMonth(), 1, 12)
  const offset = (first.getDay() + 6) % 7
  return Array.from({ length: 6 }, (_, week) => Array.from({ length: 7 }, (_, day) => {
    const date = new Date(first.getFullYear(), first.getMonth(), 1 - offset + week * 7 + day, 12)
    return { date, value: dateValue(date), outside: date.getMonth() !== first.getMonth(), today: dateValue(date) === dateValue(today()) }
  }))
})
watch(display, value => { if (!editing.value) draft.value = value })
watch(() => props.disabled, value => { if (value) closePanel() })

function publish(date: Date) {
  const value = dateValue(date)
  if (!parseDate(value) || props.disabled) return
  draft.value = value.replaceAll('-', '/')
  if (value !== props.modelValue) emit('update:modelValue', value)
}
function inputDate(event: Event) {
  draft.value = (event.target as HTMLInputElement).value
  const date = parseDate(draft.value)
  if (date && draft.value.length === 10) publish(date)
}
function commitEdit() {
  editing.value = false
  const date = parseDate(draft.value)
  if (date) publish(date)
  else draft.value = display.value
}
function closePanel(focusInput = false) {
  panel.value?.hidePopover?.()
  if (focusInput) field.value?.focus({ preventScroll: true })
}
function beforeToggle(event: Event) {
  const opening = (event as ToggleEvent).newState === 'open'
  if (opening && props.disabled) { event.preventDefault(); return }
  if (opening) { commitEdit(); month.value = current.value; focused.value = dateValue(current.value) }
  opened.value = opening
}
async function focusDate(date: Date) {
  if (!parseDate(dateValue(date))) return
  month.value = date
  focused.value = dateValue(date)
  await nextTick()
  panel.value?.querySelector<HTMLButtonElement>(`[data-date="${focused.value}"]`)?.focus({ preventScroll: true })
}
async function afterToggle(event: Event) {
  if ((event as ToggleEvent).newState === 'open') await focusDate(current.value)
}
function shiftedMonth(date: Date, offset: number) {
  const start = new Date(date.getFullYear(), date.getMonth() + offset, 1, 12)
  const last = new Date(start.getFullYear(), start.getMonth() + 1, 0, 12).getDate()
  start.setDate(Math.min(date.getDate(), last))
  return start
}
function moveMonth(offset: number) {
  const next = shiftedMonth(parseDate(focused.value) ?? current.value, offset)
  if (!parseDate(dateValue(next))) return
  month.value = next
  focused.value = dateValue(next)
}
function choose(date: Date) {
  publish(date)
  closePanel(true)
}
async function fieldKeydown(event: KeyboardEvent) {
  if (props.disabled) return
  if (event.key === 'Enter' || (event.altKey && event.key === 'ArrowDown')) {
    event.preventDefault(); commitEdit(); panel.value?.showPopover()
  } else if (event.key === 'Escape') {
    draft.value = display.value; closePanel(true)
  } else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    event.preventDefault()
    const position = field.value?.selectionStart ?? 8, delta = event.key === 'ArrowUp' ? 1 : -1
    const date = parseDate(draft.value) ?? current.value
    const next = position < 7 ? shiftedMonth(date, delta * (position < 4 ? 12 : 1)) : new Date(date.getFullYear(), date.getMonth(), date.getDate() + delta, 12)
    publish(next)
    await nextTick()
    field.value?.setSelectionRange(position < 4 ? 0 : position < 7 ? 5 : 8, position < 4 ? 4 : position < 7 ? 7 : 10)
  }
}
function calendarKeydown(event: KeyboardEvent) {
  if (props.disabled) return
  const date = parseDate((event.target as HTMLElement).dataset.date ?? focused.value) ?? current.value
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    choose(date)
    return
  }
  let next: Date | undefined
  if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
    const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[event.key]!
    next = new Date(date.getFullYear(), date.getMonth(), date.getDate() + delta, 12)
  } else if (event.key === 'Home' || event.key === 'End') {
    const day = (date.getDay() + 6) % 7
    next = new Date(date.getFullYear(), date.getMonth(), date.getDate() + (event.key === 'Home' ? -day : 6 - day), 12)
  } else if (event.key === 'PageUp' || event.key === 'PageDown') {
    next = shiftedMonth(date, (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1))
  }
  if (next) { event.preventDefault(); void focusDate(next) }
}
function panelKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePanel(true) }
  else if (event.key === 'Tab') {
    const buttons = [...(panel.value?.querySelectorAll<HTMLButtonElement>('button:not([disabled])') ?? [])].filter(button => button.tabIndex >= 0)
    if ((event.shiftKey && event.target === buttons[0]) || (!event.shiftKey && event.target === buttons.at(-1))) {
      closePanel(); trigger.value?.focus({ preventScroll: true })
    }
  }
}
</script>

<template>
  <div class="date-picker" :class="{ opened, disabled }" :style="{ 'anchor-name': anchorName }">
    <input ref="field" :value="draft" type="text" placeholder="年/月/日" maxlength="10" autocomplete="off" spellcheck="false" :aria-label="label" role="combobox" aria-haspopup="dialog" aria-autocomplete="none" :aria-expanded="opened" :aria-controls="panelId" :disabled="disabled" @focus="editing = true" @input="inputDate" @blur="commitEdit" @keydown="fieldKeydown" />
    <button ref="trigger" class="date-picker-trigger" type="button" :popovertarget="panelId" :aria-label="`选择${label}`" :title="`选择${label}`" aria-haspopup="dialog" :aria-expanded="opened" :aria-controls="panelId" :disabled="disabled"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="6" width="16" height="14" rx="2" /><path d="M8 3v6m8-6v6M4 11h16" /></svg></button>
  </div>
  <Teleport to="body">
    <div :id="panelId" ref="panel" class="date-picker-panel" popover="auto" role="dialog" :aria-label="`${label}选择`" :style="{ 'position-anchor': anchorName }" @beforetoggle="beforeToggle" @toggle="afterToggle" @keydown="panelKeydown" @wheel="containPickerWheel">
      <div class="date-picker-heading">
        <button type="button" aria-label="上个月" :disabled="month.getFullYear() === 1000 && month.getMonth() === 0" @click="moveMonth(-1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 6-6 6 6 6" /></svg></button>
        <strong :id="monthId" aria-live="polite">{{ monthTitle }}</strong>
        <button type="button" aria-label="下个月" :disabled="month.getFullYear() === 9999 && month.getMonth() === 11" @click="moveMonth(1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m10 6 6 6-6 6" /></svg></button>
      </div>
      <div class="date-picker-calendar" role="grid" :aria-labelledby="monthId" @keydown="calendarKeydown">
        <div class="date-picker-week" role="row"><span v-for="day in weekdays" :key="day" role="columnheader">{{ day }}</span></div>
        <div v-for="(week, index) in weeks" :key="index" class="date-picker-week" role="row">
          <button v-for="day in week" :key="day.value" type="button" role="gridcell" :data-date="day.value" :class="{ outside: day.outside }" :aria-label="`${day.date.getFullYear()}年${day.date.getMonth() + 1}月${day.date.getDate()}日`" :aria-selected="day.value === modelValue" :aria-current="day.today ? 'date' : undefined" :tabindex="day.value === focused ? 0 : -1" :disabled="disabled || !parseDate(day.value)" @focus="focused = day.value" @click="choose(day.date)">{{ day.date.getDate() }}</button>
        </div>
      </div>
      <div class="date-picker-footer"><button type="button" @click="choose(today())">今天</button><button type="button" @click="closePanel(true)">完成</button></div>
    </div>
  </Teleport>
</template>

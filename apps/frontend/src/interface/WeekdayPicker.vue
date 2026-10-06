<script setup lang="ts">
import { computed, nextTick, ref, useId, watch } from 'vue'
import { containPickerWheel } from './picker-scroll'

const props = defineProps<{
  /** modelValue（已选星期）：1 至 7 对应周一至周日，沿用逗号分隔的保存格式。 */
  modelValue: string
  /** disabled（保存期间禁用）。 */
  disabled?: boolean
}>()
const emit = defineEmits<{
  /** update:modelValue（更新已选星期）。 */
  'update:modelValue': [value: string]
}>()
const labels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const selectedDays = computed(() => [...new Set(props.modelValue.split(',').filter(value => value.trim()).map(Number).filter(value => Number.isInteger(value) && value >= 1 && value <= 7))].sort((a, b) => a - b)) // selectedDays（已选星期）：保持排序与唯一性。
const summary = computed(() => selectedDays.value.length === 7 ? '周一至周日' : selectedDays.value.map(day => labels[day - 1]).join('、')) // summary（收起后的选择摘要）。
const opened = ref(false), focusedDay = ref(1) // opened（面板已展开）、focusedDay（键盘所在星期）。
const trigger = ref<HTMLButtonElement>(), panel = ref<HTMLElement>() // trigger（选择框）、panel（浮动面板）。
const instance = useId(), panelId = `weekday-picker-${instance}`
const anchorName = `--weekday-picker-${instance}` // anchorName（面板定位锚点）。

watch(() => props.disabled, value => { if (value) closePanel() })
function toggleDay(day: number) {
  if (props.disabled) return
  const days = new Set(selectedDays.value)
  if (days.has(day)) days.delete(day)
  else days.add(day)
  emit('update:modelValue', [...days].sort((a, b) => a - b).join(','))
}
function closePanel(focusTrigger = false) {
  panel.value?.hidePopover?.()
  if (focusTrigger) trigger.value?.focus({ preventScroll: true })
}
function beforeToggle(event: Event) {
  const opening = (event as ToggleEvent).newState === 'open'
  if (opening && props.disabled) { event.preventDefault(); return }
  opened.value = opening
}
async function focusDay(day: number) {
  focusedDay.value = Math.max(1, Math.min(7, day))
  await nextTick()
  panel.value?.querySelector<HTMLButtonElement>(`[data-day="${focusedDay.value}"]`)?.focus({ preventScroll: true })
}
async function afterToggle(event: Event) {
  if ((event as ToggleEvent).newState === 'open') await focusDay(selectedDays.value[0] ?? 1)
}
function triggerKeydown(event: KeyboardEvent) {
  if (!props.disabled && event.key === 'ArrowDown') {
    event.preventDefault()
    panel.value?.showPopover()
  }
}
function optionsKeydown(event: KeyboardEvent) {
  if (props.disabled) return
  const day = Number((event.target as HTMLElement).dataset.day ?? focusedDay.value)
  if (['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
    event.preventDefault()
    void focusDay(event.key === 'Home' ? 1 : event.key === 'End' ? 7 : day + (event.key === 'ArrowDown' ? 1 : -1))
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    toggleDay(day)
  } else if (event.key === 'Tab' && event.shiftKey) {
    event.preventDefault()
    closePanel(true)
  }
}
function panelKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault(); event.stopPropagation(); closePanel(true)
  } else if (event.key === 'Tab' && !event.shiftKey && event.target === panel.value?.querySelector('.weekday-picker-done')) {
    closePanel()
    // Tab（制表键）：从原选择框的位置继续进入下一个表单控件。
    trigger.value?.focus({ preventScroll: true })
  }
}
</script>

<template>
  <button ref="trigger" class="weekday-picker" :class="{ opened, empty: !summary }" type="button" :style="{ 'anchor-name': anchorName }" :popovertarget="panelId" :title="summary || '选择星期'" :aria-label="`星期：${summary || '未选择'}`" aria-haspopup="dialog" :aria-expanded="opened" :aria-controls="panelId" :disabled="disabled" @keydown="triggerKeydown">
    <span class="weekday-picker-value">{{ summary || '选择星期' }}</span><i aria-hidden="true"></i>
  </button>
  <Teleport to="body">
    <div :id="panelId" ref="panel" class="weekday-picker-panel" popover="auto" role="dialog" aria-label="选择星期" :style="{ 'position-anchor': anchorName }" @beforetoggle="beforeToggle" @toggle="afterToggle" @keydown="panelKeydown" @wheel="containPickerWheel">
      <header class="weekday-picker-heading"><span>选择星期</span><small>可多选</small></header>
      <div class="weekday-picker-options" role="listbox" aria-label="每周触发日期" aria-multiselectable="true" @keydown="optionsKeydown">
        <button v-for="(label, index) in labels" :key="label" type="button" role="option" :data-day="index + 1" :aria-selected="selectedDays.includes(index + 1)" :tabindex="focusedDay === index + 1 ? 0 : -1" :disabled="disabled" @focus="focusedDay = index + 1" @click="toggleDay(index + 1)">{{ label }}</button>
      </div>
      <footer class="weekday-picker-footer"><button class="weekday-picker-done" type="button" @click="closePanel(true)">完成</button></footer>
    </div>
  </Teleport>
</template>

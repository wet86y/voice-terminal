<script setup lang="ts">
import { computed, nextTick, ref, useId, watch } from "vue";
import { containPickerWheel } from "./picker-scroll";

const props = defineProps<{
  modelValue: string; // modelValue（时间值）：沿用 HH:mm（时:分）格式。
  label: string; // label（控件名称）：区分开始时间和结束时间。
  allow24?: boolean; // allow24（允许24:00）：仅供间隔时间使用，24点时分钟固定为00。
  invalid?: boolean; // invalid（时间值无效）：供间隔设置显示字段校验状态。
  disabled?: boolean; // disabled（是否禁用）。
}>();
const emit = defineEmits<{
  "update:modelValue": [value: string]; // update:modelValue（更新时间值）。
}>();
type TimePart = "hour" | "minute"; // TimePart（正在调整的时间部分）。

function parseTime(value: string, allow24 = false) {
  const match = value.trim().replace("：", ":").match(/^(\d{1,2}):(\d{1,2})$/);
  if (!match) return null;
  const hour = Number(match[1]), minute = Number(match[2]);
  return (hour <= 23 || (allow24 && hour === 24 && minute === 0)) && minute <= 59 ? { hour, minute } : null;
}
const pad = (value: number) => String(value).padStart(2, "0");
const formatted = (time: { hour: number; minute: number }) => `${pad(time.hour)}:${pad(time.minute)}`;
const current = computed(() => parseTime(props.modelValue, props.allow24) ?? { hour: 0, minute: 0 });
const normalized = computed(() => {
  const time = parseTime(props.modelValue, props.allow24);
  return time ? formatted(time) : "";
});
const draft = ref(normalized.value); // draft（输入中的时间文字）。
const editing = ref(false); // editing（是否正在直接输入）。
const opened = ref(false); // opened（时间面板是否展开）。
const field = ref<HTMLInputElement>(); // field（时间输入框）。
const trigger = ref<HTMLButtonElement>(); // trigger（时钟按钮）。
const panel = ref<HTMLElement>(); // panel（浮动时间面板）。
const instanceId = useId(); // instanceId（每个时间控件的唯一标识）。
const panelId = `time-picker-${instanceId}`;
const anchorName = `--time-picker-${instanceId}`; // anchorName（面板对应的输入框位置锚点）。
const parts: TimePart[] = ["hour", "minute"];
const options = computed(() => ({
  hour: Array.from({ length: props.allow24 ? 25 : 24 }, (_, value) => value),
  minute: Array.from({ length: 60 }, (_, value) => value),
}));

watch(normalized, value => { if (!editing.value) draft.value = value; });
watch(() => props.disabled, value => { if (value) closePanel(); });

function publish(time: { hour: number; minute: number }) {
  draft.value = formatted(time);
  if (draft.value !== props.modelValue) emit("update:modelValue", draft.value);
}
function inputTime(event: Event) {
  draft.value = (event.target as HTMLInputElement).value;
  const time = parseTime(draft.value, props.allow24);
  if (time && draft.value.length === 5) publish(time);
}
function commitEdit() {
  editing.value = false;
  const time = parseTime(draft.value, props.allow24);
  if (time) publish(time);
  else draft.value = normalized.value;
}
function optionElement(part: TimePart, value = current.value[part]) {
  return panel.value?.querySelector<HTMLButtonElement>(`[data-part="${part}"][data-value="${value}"]`);
}
function centerOptions() {
  // centerOptions（居中已选时间）：两列的选中行保持在同一高度。
  for (const part of parts) {
    const option = optionElement(part), list = option?.parentElement;
    if (option && list) list.scrollTop = option.offsetTop - (list.clientHeight - option.offsetHeight) / 2;
  }
}
function focusOption(part: TimePart) {
  const option = optionElement(part);
  option?.focus({ preventScroll: true });
  option?.scrollIntoView({ block: "nearest" });
}
function beforeToggle(event: Event) {
  const opening = (event as ToggleEvent).newState === "open";
  if (opening && props.disabled) { event.preventDefault(); return; }
  if (opening) commitEdit();
  opened.value = opening;
}
async function afterToggle(event: Event) {
  if ((event as ToggleEvent).newState !== "open") return;
  await nextTick();
  centerOptions();
  focusOption("hour");
}
function showPanel() {
  if (props.disabled) return;
  commitEdit();
  panel.value?.showPopover();
}
function closePanel(focusInput = false) {
  panel.value?.hidePopover?.();
  if (focusInput) field.value?.focus({ preventScroll: true });
}
async function choose(part: TimePart, value: number, focus = false) {
  if (props.disabled) return;
  if (part === "hour" && value === 24) publish({ hour: 24, minute: 0 });
  else if (part === "minute" && current.value.hour === 24) return;
  else publish({ ...current.value, [part]: value });
  await nextTick();
  if (focus) focusOption(part);
}
async function fieldKeydown(event: KeyboardEvent) {
  if (props.disabled) return;
  if (event.key === "Enter" || (event.altKey && event.key === "ArrowDown")) {
    event.preventDefault();
    showPanel();
  } else if (event.key === "Escape") {
    draft.value = normalized.value;
    closePanel(true);
  } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    event.preventDefault();
    const part: TimePart = (field.value?.selectionStart ?? 0) < 3 ? "hour" : "minute";
    const time = parseTime(draft.value, props.allow24) ?? current.value;
    const delta = (event.key === "ArrowUp" ? 1 : -1) * (part === "hour" ? 60 : 1);
    const rawTotal = time.hour * 60 + time.minute + delta;
    const total = props.allow24 ? Math.max(0, Math.min(1440, rawTotal)) : (rawTotal + 1440) % 1440;
    publish({ hour: Math.floor(total / 60), minute: total % 60 });
    await nextTick();
    field.value?.setSelectionRange(part === "hour" ? 0 : 3, part === "hour" ? 2 : 5);
  }
}
function columnKeydown(event: KeyboardEvent, part: TimePart) {
  const value = Number((event.target as HTMLElement).dataset.value ?? current.value[part]);
  const last = part === "hour" ? (props.allow24 ? 24 : 23) : current.value.hour === 24 ? 0 : 59;
  if (event.key === "ArrowUp" || event.key === "ArrowDown" || event.key === "Home" || event.key === "End") {
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? last : Math.max(0, Math.min(last, value + (event.key === "ArrowDown" ? 1 : -1)));
    void choose(part, next, true);
  } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    focusOption(event.key === "ArrowLeft" ? "hour" : "minute");
  } else if (event.key === "Enter" || event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closePanel(true);
  } else if (event.key === "Tab") {
    if ((part === "hour" && !event.shiftKey) || (part === "minute" && event.shiftKey)) {
      event.preventDefault();
      focusOption(part === "hour" ? "minute" : "hour");
    } else {
      closePanel();
      // Tab（制表键）：回到原控件的位置，再由浏览器移向前后表单字段。
      trigger.value?.focus({ preventScroll: true });
    }
  }
}
</script>

<template>
  <div class="time-picker" :class="{ opened, disabled }" :style="{ 'anchor-name': anchorName }">
    <input
      ref="field"
      :value="draft"
      type="text"
      placeholder="时:分"
      maxlength="5"
      autocomplete="off"
      spellcheck="false"
      :aria-label="label"
      role="combobox"
      aria-haspopup="dialog"
      aria-autocomplete="none"
      :aria-expanded="opened"
      :aria-controls="panelId"
      :aria-invalid="invalid ? 'true' : undefined"
      :disabled="disabled"
      @focus="editing = true"
      @input="inputTime"
      @blur="commitEdit"
      @keydown="fieldKeydown"
    />
    <button
      ref="trigger"
      class="time-picker-trigger"
      type="button"
      :popovertarget="panelId"
      :aria-label="`选择${label}`"
      :title="`选择${label}`"
      aria-haspopup="dialog"
      :aria-expanded="opened"
      :aria-controls="panelId"
      :disabled="disabled"
    ><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M12 7v5l3 2" /></svg></button>
  </div>
  <Teleport to="body">
    <div
      :id="panelId"
      ref="panel"
      class="time-picker-panel"
      popover="auto"
      role="dialog"
      :aria-label="`${label}选择`"
      :style="{ 'position-anchor': anchorName }"
      @beforetoggle="beforeToggle"
      @toggle="afterToggle"
      @wheel="containPickerWheel"
    >
      <div v-for="part in parts" :key="part" class="time-picker-column">
        <span aria-hidden="true">{{ part === 'hour' ? '小时' : '分钟' }}</span>
        <div class="time-picker-options" role="listbox" :aria-label="`${label}${part === 'hour' ? '小时' : '分钟'}`" @keydown="columnKeydown($event, part)">
          <button
            v-for="value in options[part]"
            :key="value"
            type="button"
            role="option"
            :data-part="part"
            :data-value="value"
            :aria-selected="current[part] === value"
            :tabindex="current[part] === value ? 0 : -1"
            :disabled="disabled || (allow24 && part === 'minute' && current.hour === 24 && value !== 0)"
            @click="choose(part, value)"
          >{{ pad(value) }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import {
  normalizeSilenceSeconds,
  silenceDurationDisplay,
  silenceSecondsToSlider,
  sliderToSilenceSeconds,
  stepSilenceSeconds,
  type SilenceUnit,
} from "./silence-duration";

const props = defineProps<{
  modelValue: number; // modelValue（静默时限的整数秒数）。
  disabled?: boolean; // disabled（是否禁用输入）。
}>();
const emit = defineEmits<{
  "update:modelValue": [seconds: number]; // update:modelValue（更新静默时限秒数）。
}>();

const display = computed(() => silenceDurationDisplay(props.modelValue));
const sliderPosition = computed(() => silenceSecondsToSlider(props.modelValue));
const numberFocused = ref(false); // numberFocused（数值框是否正在编辑）。
const draftUnit = ref<SilenceUnit>("seconds"); // draftUnit（编辑期间保持的单位）。
const draftValue = ref(""); // draftValue（尚在编辑的输入文字）。
const currentUnit = computed(() => numberFocused.value ? draftUnit.value : display.value.unit);
const unitLabel = computed(() => currentUnit.value === "seconds" ? "秒" : "分钟");
const numberValue = computed(() => numberFocused.value ? draftValue.value : display.value.value);

function startNumberEdit() {
  draftUnit.value = display.value.unit;
  draftValue.value = String(display.value.value);
  numberFocused.value = true;
}
function updateNumber(event: Event) {
  const input = event.target as HTMLInputElement;
  draftValue.value = input.value;
  // 编辑多位数字时保持原单位，避免输入第一位后突然在秒和分钟之间跳转。
  if (input.value === "" || !Number.isFinite(input.valueAsNumber)) return;
  const seconds = input.valueAsNumber * (currentUnit.value === "minutes" ? 60 : 1);
  emit("update:modelValue", normalizeSilenceSeconds(seconds));
}
function finishNumberEdit(event: FocusEvent) {
  numberFocused.value = false;
  (event.target as HTMLInputElement).value = String(display.value.value);
}
function applyDuration(seconds: number) {
  // applyDuration（同步两侧控件）：滚轮、键盘或滑条调整时立即切换数值与单位。
  const normalized = normalizeSilenceSeconds(seconds);
  const nextDisplay = silenceDurationDisplay(normalized);
  draftUnit.value = nextDisplay.unit;
  draftValue.value = String(nextDisplay.value);
  emit("update:modelValue", normalized);
}
function stepDuration(direction: -1 | 1) {
  if (props.disabled) return;
  applyDuration(stepSilenceSeconds(props.modelValue, direction));
}
function stepFromWheel(event: WheelEvent) {
  if (props.disabled || event.deltaY === 0) return;
  // 阻止浏览器原生步进及页面滚动，避免重复增减或卡在当前显示单位的上限。
  event.preventDefault();
  stepDuration(event.deltaY < 0 ? 1 : -1);
}
function updateSlider(event: Event) {
  applyDuration(sliderToSilenceSeconds(Number((event.target as HTMLInputElement).value)));
}
</script>

<template>
  <div class="silence-timeout-control">
    <span class="silence-timeout-label">最终静默等待</span>
    <div class="silence-timeout-row">
      <div class="silence-timeout-range" @wheel="stepFromWheel">
        <input
          :value="sliderPosition"
          min="0"
          max="100"
          step="any"
          type="range"
          aria-label="调整静默等待"
          :aria-valuetext="`${display.value} ${display.label}`"
          :disabled="disabled"
          @input="updateSlider"
          @keydown.up.prevent="stepDuration(1)"
          @keydown.right.prevent="stepDuration(1)"
          @keydown.down.prevent="stepDuration(-1)"
          @keydown.left.prevent="stepDuration(-1)"
        />
      </div>
      <label class="silence-timeout-number" @wheel="stepFromWheel">
        <input
          :value="numberValue"
          :min="currentUnit === 'seconds' ? 5 : 3"
          :max="currentUnit === 'seconds' ? 180 : 30"
          step="1"
          type="number"
          :aria-label="`最终静默等待${currentUnit === 'seconds' ? '秒' : '分钟'}数`"
          :disabled="disabled"
          @focus="startNumberEdit"
          @input="updateNumber"
          @blur="finishNumberEdit"
          @keydown.up.prevent="stepDuration(1)"
          @keydown.down.prevent="stepDuration(-1)"
        />
        <span>{{ unitLabel }}</span>
      </label>
      <div class="silence-timeout-scale" aria-hidden="true">
        <span>5 秒</span><span title="180 秒，即 3 分钟">180 秒</span><span>30 分钟</span>
      </div>
    </div>
  </div>
</template>

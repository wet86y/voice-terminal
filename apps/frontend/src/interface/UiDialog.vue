<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from "vue";
const props = defineProps<{
  open: boolean;
  title: string;
  busy?: boolean;
  /** kicker（标题上方说明）：为空时沿用确认弹窗文案。 */
  kicker?: string;
  /** wide（宽弹窗）：为编辑器等长内容提供更宽的可读区域。 */
  wide?: boolean;
}>();
const emit = defineEmits<{ close: [] }>();
const panel = ref<HTMLElement | null>(null);
let previous: HTMLElement | null = null;
function key(event: KeyboardEvent) {
  if (event.key === "Escape" && !props.busy) emit("close");
  if (event.key === "Tab" && panel.value) {
    const focus = [
      ...panel.value.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input, select, textarea, [tabindex="0"]',
      ),
    ];
    if (!focus.length) {
      event.preventDefault();
      return;
    }
    if (event.shiftKey && document.activeElement === focus[0]) {
      event.preventDefault();
      focus.at(-1)?.focus();
    } else if (!event.shiftKey && document.activeElement === focus.at(-1)) {
      event.preventDefault();
      focus[0]?.focus();
    }
  }
}
watch(
  () => props.open,
  async (open) => {
    if (open) {
      previous = document.activeElement as HTMLElement;
      document.addEventListener("keydown", key);
      await nextTick();
      panel.value?.focus();
    } else {
      document.removeEventListener("keydown", key);
      previous?.focus();
    }
  },
  { immediate: true },
);
onBeforeUnmount(() => document.removeEventListener("keydown", key));
</script>
<template>
  <Transition name="dialog"
    ><div
      v-if="open"
      class="modal-backdrop"
      @mousedown.self="!busy && emit('close')"
    >
      <section
        ref="panel"
        class="apply-dialog"
        :class="{ 'apply-dialog-wide': props.wide }"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        tabindex="-1"
      >
        <p class="panel-kicker">{{ props.kicker ?? "请确认" }}</p>
        <h2 id="dialog-title">{{ title }}</h2>
        <slot />
        <footer><slot name="actions" /></footer>
      </section></div
  ></Transition>
</template>

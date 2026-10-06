<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
defineProps<{
  runtime?: string; // runtime（当前运行状态）。
  preview?: boolean; // preview（是否显示演示辅助界面）。
}>();
const hidden = ref(false);
function visibility() {
  hidden.value = document.visibilityState === "hidden";
}
onMounted(() => {
  visibility();
  document.addEventListener("visibilitychange", visibility);
});
onBeforeUnmount(() =>
  document.removeEventListener("visibilitychange", visibility),
);
</script>
<template>
  <main
    class="app-shell"
    :class="[
      `runtime-${runtime || 'standby'}`,
      { 'app-hidden': hidden, 'is-preview': preview },
    ]"
  >
    <slot name="banner" />
    <slot />
    <slot name="overlays" />
  </main>
</template>

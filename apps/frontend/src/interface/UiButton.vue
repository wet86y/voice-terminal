<script setup lang="ts">
withDefaults(
  defineProps<{
    variant?: "primary" | "quiet" | "danger"; // variant（视觉层级）：主要、次要或危险操作。
    busy?: boolean; // busy（处理中）：同时禁用按钮并向辅助技术报告忙碌状态。
    disabled?: boolean; // disabled（禁用状态）。
  }>(),
  { variant: "primary", busy: false, disabled: false },
);
</script>
<template>
  <button
    type="button"
    :class="['ui-button', `button-${variant}`]"
    :disabled="disabled || busy"
    :aria-busy="busy"
  >
    <slot />
  </button>
</template>

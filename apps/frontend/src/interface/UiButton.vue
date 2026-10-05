<script setup lang="ts">
withDefaults(
  defineProps<{
    variant?: "primary" | "quiet" | "danger";
    busy?: boolean;
    disabled?: boolean;
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

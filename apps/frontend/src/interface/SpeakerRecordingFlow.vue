<script setup lang="ts">
defineProps<{ completed: number; total: number }>(); // completed（已完成段数）、total（所需段数）。
</script>
<template>
  <div class="speaker-recording-flow">
    <slot />
    <footer class="speaker-flow-footer">
      <div class="speaker-flow-progress"><span role="status">已完成 {{ completed }} / {{ total }} 段</span><div class="speaker-progress-marks" aria-hidden="true"><i v-for="index in total" :key="index" :class="{ completed: index <= completed }"></i></div></div>
      <div class="speaker-flow-actions"><slot name="actions" /></div>
    </footer>
  </div>
</template>

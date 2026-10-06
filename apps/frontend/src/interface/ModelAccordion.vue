<script setup lang="ts">
// open（展开状态）：正式页由根层保存，演示页使用同一原生折叠交互。
const open = defineModel<boolean>('open', { default: true })
// index（序号）、title（标题）与 hint（简短说明）：沿用演示卡片抬头。
defineProps<{ index: string; title: string; hint: string }>()
function toggle(event: Event) { open.value = (event.target as HTMLDetailsElement).open }
</script>
<template>
  <details class="model-accordion-card" :open="open" @toggle="toggle">
    <summary class="preview-accordion-summary"><span class="accordion-index">{{ index }}</span><strong>{{ title }}</strong><span>{{ hint }}</span></summary>
    <div v-if="open" class="model-card-content"><slot /></div>
  </details>
</template>

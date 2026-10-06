<script setup lang="ts">
// name（显示名称）、identifier（模型标识与来源）、badge（用途）、status（真实状态）、statusClass（状态样式）。
// facts（策略说明）包含 label（名称）及 value（实际值）；状态和编辑动作由调用方提供。
defineProps<{ name: string; identifier: string; badge: string; status: string; statusClass?: string; facts: Array<{ label: string; value: string }> }>()
</script>
<template>
  <article class="pool-entry-card">
    <div class="pool-entry-topline"><span class="provider-badge managed">{{ badge }}</span><div class="pool-entry-heading"><div class="pool-entry-title"><strong :title="name">{{ name }}</strong><span class="entry-status" :class="statusClass">{{ status }}</span></div><span class="pool-entry-id">{{ identifier }}</span></div></div>
    <div class="pool-entry-policy"><span v-for="fact in facts" :key="fact.label"><small>{{ fact.label }}</small><strong>{{ fact.value }}</strong></span></div>
    <div v-if="$slots.actions" class="pool-entry-actions"><slot name="actions" /></div>
    <slot />
  </article>
</template>

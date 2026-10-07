<script setup lang="ts">
defineProps<{
  pending: number; // pending（待执行任务数）。
  archived: number; // archived（归档及取消记录数）。
}>();
const view = defineModel<'pending' | 'archived'>({ required: true }); // view（当前任务列表）。
</script>
<template>
  <div class="wake-task-tabs" role="group" aria-label="任务分类">
    <button type="button" :class="{ active: view === 'pending' }" :aria-pressed="view === 'pending'" @click="view = 'pending'">待执行 {{ pending }} 项</button>
    <button type="button" :class="{ active: view === 'archived' }" :aria-pressed="view === 'archived'" @click="view = 'archived'">已归档 {{ archived }} 项</button>
  </div>
</template>

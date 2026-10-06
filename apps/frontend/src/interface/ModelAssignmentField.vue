<script setup lang="ts">
// model（角色分配草稿）：保存、验证和应用时点由调用方管理。
const model = defineModel<string>({ required: true })
// label（角色名称）、disabled（禁用）、hint（策略）、status（实际状态）。
// options（候选项）的 value（模型标识）与 label（显示名称）只来自调用方的模型池。
defineProps<{ label: string; disabled?: boolean; hint?: string; status?: string; options: Array<{ value: string; label: string }> }>()
</script>
<template>
  <label class="form-field model-assignment-field"><span>{{ label }}</span><select v-model="model" :aria-label="label" :disabled="disabled"><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select><small v-if="hint || status">{{ [hint, status].filter(Boolean).join(' · ') }}</small></label>
</template>

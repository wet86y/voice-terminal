<script setup lang="ts">
// row（指令行）：演示与正式程序使用同一字段结构，持久化由调用方负责。
defineProps<{ row: { trigger: string; target: string; instruction: string; enabled: boolean }; disabled?: boolean; removable?: boolean }>();
defineEmits<{ remove: [] }>();
</script>
<template>
  <div class="instruction-row instruction-editor-row" :class="{ removable }">
    <label class="form-field"><span>触发语义</span><input v-model="row.trigger" type="text" maxlength="200" :disabled="disabled" /></label>
    <label class="form-field"><span>执行位置</span><select v-model="row.target" :disabled="disabled"><option value="voice">主语音助手</option><option value="shadow">Shadow（影子代理）</option></select></label>
    <label class="form-field instruction-target-field"><span>执行目标</span><textarea v-model="row.instruction" rows="1" maxlength="2000" :disabled="disabled"></textarea></label>
    <label class="instruction-toggle" :title="row.enabled ? '停用这条指令' : '启用这条指令'"><input v-model="row.enabled" type="checkbox" role="switch" :aria-label="`启用指令：${row.trigger}`" :disabled="disabled" /><i aria-hidden="true"></i></label>
    <button v-if="removable" class="ghost-action instruction-delete" type="button" :aria-label="`删除指令：${row.trigger}`" :disabled="disabled" @click="$emit('remove')">删除</button>
  </div>
</template>
<style scoped>
.removable{grid-template-columns:var(--instruction-columns) 36px 32px}
.instruction-delete{min-width:32px;height:42px;padding-inline:0;margin-top:var(--instruction-label-space)}
@container (max-width:560px){.removable{grid-template-columns:minmax(0,1fr) minmax(0,1fr) 36px 32px}}
@container (max-width:380px){.removable{grid-template-columns:minmax(0,1fr) 36px 32px}}
</style>

<script setup lang="ts">
import { computed, ref } from 'vue';
// row（独立指令草稿）：正式程序和演示复用同一条目组件。
const props = withDefaults(defineProps<{
  row: { id?: string; name?: string; trigger: string; keywords?: string[]; target: string; instruction: string; enabled: boolean };
  disabled?: boolean; removable?: boolean; saved?: boolean; error?: string;
}>(), { saved: true, error: '' });
const emit = defineEmits<{ save: []; remove: []; toggle: [enabled: boolean] }>();
const expanded = defineModel<boolean>('expanded', { default: false }); // expanded（当前条目是否展开）。
const displayName = computed(() => props.row.name?.trim() || props.row.trigger.trim() || '新指令');
const editorId = computed(() => `instruction-editor-${props.row.id ?? 'draft'}`);
const keywordDraft = ref(''); // keywordDraft（尚未加入列表的输入）。
const validKeyword = (word: string) => /^[\u3400-\u4dbf\u4e00-\u9fff]{2,4}$/u.test(word);
function addKeywords(event?: KeyboardEvent | FocusEvent) {
  if (props.disabled || (event && 'isComposing' in event && event.isComposing)) return;
  if (event instanceof KeyboardEvent) event.preventDefault();
  const words = keywordDraft.value.split(/[，,、;；\s]+/u).map(word => word.trim()).filter(Boolean);
  if (words.length) props.row.keywords = [...new Set([...(props.row.keywords ?? []), ...words])];
  keywordDraft.value = '';
}
function removeKeyword(index: number) {
  props.row.keywords = (props.row.keywords ?? []).filter((_, position) => position !== index);
}
function save() { addKeywords(); emit('save'); }
function toggle(event: Event) {
  const input = event.target as HTMLInputElement;
  const enabled = input.checked;
  input.checked = props.row.enabled; // 启停仅在服务器确认后显示生效，失败保留原状态。
  emit('toggle', enabled);
}
</script>
<template>
  <article class="instruction-card" :class="{ enabled: row.enabled, expanded }" :aria-label="`指令：${displayName}`">
    <header class="instruction-heading">
      <input v-if="expanded" v-model="row.name" class="instruction-name-input" aria-label="指令名称（选填）" title="指令名称（选填，留空时显示触发语义）" maxlength="80" :placeholder="row.trigger.trim() || '指令名称（选填）'" :disabled="disabled" />
      <button v-else type="button" class="instruction-name" :title="displayName" :aria-expanded="expanded" :aria-controls="editorId" @click="expanded = true">{{ displayName }}</button>
      <button type="button" class="ghost-action instruction-collapse" :aria-expanded="expanded" :aria-controls="editorId" @click="expanded = !expanded">{{ expanded ? '收起' : '展开' }}</button>
      <label class="instruction-toggle" :title="saved ? (row.enabled ? '停用这条指令' : '启用这条指令') : '保存后可启停'">
        <input type="checkbox" role="switch" :aria-label="`启用指令：${displayName}`" :checked="row.enabled" :disabled="disabled || !saved" @change="toggle" /><i aria-hidden="true"></i>
      </label>
    </header>
    <div v-if="expanded" :id="editorId" class="instruction-editor">
      <div class="instruction-main-fields">
        <label class="form-field"><span>触发语义</span><textarea v-model="row.trigger" class="compact-textarea" rows="1" maxlength="200" :disabled="disabled" placeholder="描述什么情况下执行"></textarea></label>
        <label class="form-field"><span>执行目标</span><textarea v-model="row.instruction" class="compact-textarea" rows="1" maxlength="2000" :disabled="disabled" placeholder="描述需要完成什么"></textarea></label>
      </div>
      <div class="instruction-secondary-fields">
        <div class="form-field instruction-keywords">
          <label><span>触发关键词（选填）</span><input v-model="keywordDraft" type="text" :disabled="disabled" aria-label="触发关键词（选填）" placeholder="每词 2–4 字，回车添加" @keydown.enter="addKeywords" @blur="addKeywords" /></label>
          <div v-if="row.keywords?.length" class="keyword-tags">
            <button v-for="(word, index) in row.keywords" :key="word" type="button" :disabled="disabled" :class="{ invalid: !validKeyword(word) }" :aria-label="`移除触发关键词 ${word}`" @click="removeKeyword(index)">{{ word }} ×</button>
          </div>
          <small v-if="row.keywords?.some(word => !validKeyword(word))" class="instruction-error" role="alert">每个触发关键词须为 2～4 个汉字。</small>
        </div>
        <label class="form-field"><span>执行位置</span><select v-model="row.target" :disabled="disabled"><option value="voice">主语音助手</option><option value="shadow">Shadow（影子代理）</option></select></label>
        <div class="instruction-entry-actions"><button type="button" class="save-button" :disabled="disabled" @click="save">{{ disabled ? '正在保存…' : '保存' }}</button><button v-if="removable" type="button" class="danger-action" :aria-label="`删除指令：${displayName}`" :disabled="disabled" @click="emit('remove')">删除</button></div>
      </div>
    </div>
    <p v-if="error" class="instruction-error" role="alert">{{ error }}</p>
  </article>
</template>
<style scoped>
/* 复用音色条目的卡片尺寸、颜色与标题布局，字段沿用共享表单控件。 */
.instruction-card{background:var(--panel);border:1px solid var(--line-strong);border-radius:18px;padding:12px 20px;display:grid;gap:16px;min-width:0;container:instruction-card / inline-size}
.instruction-card.enabled{border-color:color-mix(in srgb,var(--mint) 42%,transparent)}
.instruction-heading{display:flex;align-items:center;gap:10px;min-width:0}
.instruction-name{flex:1;min-width:0;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;background:transparent;border:0;padding:6px 0;font-weight:570;color:var(--text);cursor:pointer}
.instruction-name:hover{color:var(--accent)}
.instruction-name:focus-visible{outline:2px solid var(--accent);outline-offset:4px;border-radius:4px}
.instruction-name-input{flex:1;min-width:0;max-width:100%;height:32px;min-height:32px;padding:5px 9px;font-weight:570}
.instruction-collapse{flex-shrink:0;padding:6px 11px}
.instruction-toggle{margin-left:auto;flex-shrink:0}
.instruction-editor{display:grid;gap:16px;border-top:1px solid var(--line);padding-top:16px;padding-bottom:6px}
.instruction-main-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start}
.instruction-secondary-fields{display:grid;grid-template-columns:minmax(0,1fr) 160px auto;gap:14px;align-items:start}
.form-field{min-width:0}
.form-field>span,.instruction-keywords label>span{line-height:1.6;margin-bottom:6px}
.instruction-keywords{display:grid;gap:6px}
.instruction-keywords label{display:grid;min-width:0}
.instruction-keywords label>span{font-size:var(--font-caption);color:var(--muted)}
.instruction-entry-actions{display:flex;align-items:center;gap:8px;padding-top:calc(var(--font-caption) * 1.6 + 6px);white-space:nowrap}
.instruction-entry-actions button{min-height:42px}
.keyword-tags{display:flex;flex-wrap:wrap;gap:6px}
.keyword-tags button{font-size:var(--font-caption);padding:3px 9px;border:1px solid var(--line);border-radius:12px;background:transparent;color:var(--muted);cursor:pointer}
.keyword-tags button.invalid{color:var(--danger,#f48787);border-color:currentColor}
@container instruction-card (max-width:560px){.instruction-secondary-fields{grid-template-columns:minmax(0,1fr) auto}.instruction-keywords{grid-column:1/-1}}
@container instruction-card (max-width:380px){.instruction-main-fields{grid-template-columns:minmax(0,1fr)}.instruction-secondary-fields{grid-template-columns:minmax(0,1fr)}.instruction-entry-actions{padding-top:0;justify-content:flex-end}}
</style>

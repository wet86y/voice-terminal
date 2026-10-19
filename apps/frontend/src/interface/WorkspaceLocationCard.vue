<script setup lang="ts">
withDefaults(defineProps<{
  /** root（当前工作区根目录）。 */ root: string
  /** canManage（是否允许迁移工作区）。 */ canManage?: boolean
  /** disabled（正在执行互斥操作）。 */ disabled?: boolean
  /** choosing（正在选择迁移目标）。 */ choosing?: boolean
}>(), { canManage: false, disabled: false, choosing: false })
const emit = defineEmits<{
  /** migrate（选择目录并确认迁移工作区）。 */ migrate: []
}>()
</script>

<template>
  <section class="workspace-card" aria-label="工作区">
    <div class="workspace-location">
      <span class="workspace-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5h6l2 2h10v13H3V7Z" /></svg></span>
      <div class="workspace-copy">
        <p class="section-label">WORKSPACE（工作区）</p>
        <dl class="workspace-paths"><div><dt>工作区根目录</dt><dd :title="root">{{ root }}</dd></div></dl>
      </div>
    </div>
    <button v-if="canManage" class="outline-button workspace-migrate-action" type="button" :disabled="disabled" @click="emit('migrate')">{{ choosing ? '正在选择…' : '迁移工作区' }}</button>
  </section>
</template>

<style scoped>
.workspace-card{display:flex;align-items:center;justify-content:space-between;gap:18px;min-width:0;padding:20px;border:1px solid var(--line);border-radius:14px;background:#ffffff02}
.workspace-location{display:flex;align-items:center;gap:12px;min-width:0;flex:1}
.workspace-icon{display:grid;place-items:center;flex:none;width:36px;height:36px;border-radius:11px;background:#b2baff12;color:var(--accent)}
.workspace-icon svg{width:21px;height:21px}
.workspace-copy{display:grid;gap:8px;min-width:0}
.workspace-paths{margin:0;min-width:0}
.workspace-paths>div{display:grid;gap:5px;min-width:0}
.workspace-paths dt{font-size:var(--font-body);font-weight:600;color:var(--text)}
.workspace-paths dd{margin:0;font-size:var(--font-body);line-height:1.55;color:var(--muted);overflow-wrap:anywhere;word-break:break-word}
.workspace-migrate-action{flex:none;min-height:var(--compact-control-height);padding:8px 16px;font-size:var(--font-body);white-space:nowrap}
@media(max-width:560px){.workspace-card{padding:16px;gap:12px}.workspace-icon{display:none}.workspace-migrate-action{padding:8px 12px}}
</style>

<script setup lang="ts">
import { computed } from 'vue'
import { parseReleaseNotes } from './release-notes'
const props = defineProps<{ notes: string }>() // notes（完整发行说明）。
const blocks = computed(() => parseReleaseNotes(props.notes)) // blocks（排版后的段落）。
</script>

<template>
  <div class="release-notes-content">
    <template v-for="(block, index) in blocks" :key="index">
      <h5 v-if="block.kind === 'heading'">{{ block.lines[0] }}</h5>
      <ul v-else-if="block.kind === 'list'"><li v-for="(line, row) in block.lines" :key="row">{{ line }}</li></ul>
      <p v-else>{{ block.lines.join('\n') }}</p>
    </template>
  </div>
</template>

<style scoped>
.release-notes-content{display:grid;gap:12px;min-width:0;color:var(--muted);font-size:var(--font-body);line-height:1.85;overflow-wrap:anywhere}
h5{margin:8px 0 0;color:var(--text);font-size:var(--font-body);font-weight:600}
h5:first-child{margin-top:0}
ul{margin:0;padding-left:1.4em;display:grid;gap:8px}
p{margin:0;white-space:pre-wrap}
</style>

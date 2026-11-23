<script setup lang="ts">
import type { DisplayDocument } from './floating-display'

defineProps<{
  document: DisplayDocument // document（已经校验的图文正文）。
}>()
</script>

<template>
  <div class="floating-display-document" :class="{ 'floating-display-picture-only': document.blocks.length === 1 && document.blocks[0]?.kind === 'image' }">
    <template v-for="(block, index) in document.blocks" :key="index">
      <figure v-if="block.kind === 'image'" class="floating-display-picture">
        <img :src="block.src" :alt="block.description || '展示图片'" referrerpolicy="no-referrer" draggable="false">
        <figcaption v-if="block.description" class="floating-display-line">{{ block.description }}</figcaption>
      </figure>
      <component
        :is="block.kind === 'title' ? 'h2' : 'p'"
        v-else
        class="floating-display-line"
        :class="['floating-display-' + block.kind]"
      >
        <span v-if="block.kind === 'bullet'" aria-hidden="true">• </span>
        <template v-for="(run, runIndex) in block.runs" :key="runIndex">
          <strong v-if="run.bold">{{ run.text }}</strong><template v-else>{{ run.text }}</template>
        </template>
      </component>
    </template>
  </div>
</template>

<style>
.floating-display-document {
  font-size: var(--display-font-size, var(--font-section));
  line-height: 1.42;
  min-width: 0;
  overflow-wrap: anywhere;
}
.floating-display-document .floating-display-line { margin: 0; font-size: inherit; line-height: inherit; }
.floating-display-document .floating-display-title { font-weight: 700; }
.floating-display-picture { margin: 3px 0; }
.floating-display-picture img { display: block; width: 100%; height: 60px; object-fit: contain; }
.floating-display-picture figcaption { color: var(--muted); text-align: center; }
.floating-display-picture-only img { height: 107px; }
</style>

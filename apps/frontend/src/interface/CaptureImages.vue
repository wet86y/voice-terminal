<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import UiDialog from './UiDialog.vue'
import type { ImageAttachment } from '../types'
import { acquireImage, releaseImage, onImageReconnect } from './image-channel'
const props = defineProps<{ attachments: ImageAttachment[] }>()
const urls = ref<Record<string, string>>({})
const errors = ref<Record<string, string>>({})
const selected = ref<number | null>(null)
const zoom = ref(1), actual = ref(false), offset = ref({ x: 0, y: 0 })
const active = computed(() => selected.value === null ? null : props.attachments[selected.value])
let retained: ImageAttachment[] = [], generation = 0
let drag: { x: number; y: number; ox: number; oy: number } | null = null
const label = (a: ImageAttachment) => a.role === 'global' ? '全局画面' : '局部区域'
async function load(a: ImageAttachment) {
  const current = generation
  errors.value[a.attachment_id] = ''
  retained.push(a)
  try { const url = await acquireImage(a); if (current === generation) urls.value[a.attachment_id] = url }
  catch (e) { if (current === generation) errors.value[a.attachment_id] = e instanceof Error ? e.message : '图片加载失败' }
}
watch(() => props.attachments.map(a => `${a.conversation_id}:${a.attachment_id}`).join('|'), () => {
  generation++; retained.forEach(releaseImage); retained = []; urls.value = {}; errors.value = {}
  props.attachments.forEach(load)
}, { immediate: true })
function reset() { zoom.value = 1; actual.value = false; offset.value = { x: 0, y: 0 } }
function open(index: number) { selected.value = index; reset() }
function close() { selected.value = null }
function begin(e: PointerEvent) { drag = { x: e.clientX, y: e.clientY, ox: offset.value.x, oy: offset.value.y }; (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId) }
function move(e: PointerEvent) { if (drag) offset.value = { x: drag.ox + e.clientX - drag.x, y: drag.oy + e.clientY - drag.y } }
const stopReconnect = onImageReconnect(() => { props.attachments.filter(a => errors.value[a.attachment_id]).forEach(load) })
onBeforeUnmount(() => { stopReconnect(); generation++; retained.forEach(releaseImage) })
</script>
<template>
  <div class="capture-grid">
    <figure v-for="(a, index) in attachments" :key="a.attachment_id">
      <button v-if="urls[a.attachment_id]" type="button" class="capture-thumbnail" :aria-label="`放大${label(a)}`" @click="open(index)">
        <img :src="urls[a.attachment_id]" :alt="label(a)" :width="a.width" :height="a.height" />
      </button>
      <div v-else class="capture-placeholder" role="status"><span>{{ errors[a.attachment_id] || '图片加载中…' }}</span><button v-if="errors[a.attachment_id]" type="button" @click="load(a)">重试</button></div>
      <figcaption>{{ label(a) }} · {{ a.width }} × {{ a.height }}</figcaption>
    </figure>
  </div>
  <Teleport to="body">
    <UiDialog v-if="active" open class="image-viewer-dialog" :title="label(active)" kicker="屏幕图片" @close="close">
      <template #header-actions><div class="image-tools">
          <button v-for="(a, i) in attachments" :key="a.attachment_id" type="button" :aria-pressed="selected === i" @click="selected = i; reset()">{{ label(a) }}</button>
          <button type="button" aria-label="缩小图片" @click="zoom = Math.max(.25, zoom / 1.25)">−</button><span>{{ Math.round(zoom * 100) }}%</span>
          <button type="button" aria-label="放大图片" @click="zoom = Math.min(8, zoom * 1.25)">＋</button>
          <button type="button" @click="actual = !actual; zoom = 1; offset = { x: 0, y: 0 }">{{ actual ? '适应窗口' : '实际像素' }}</button><button type="button" @click="reset">复位</button>
      </div></template>
        <div class="image-stage" @pointerdown="begin" @pointermove="move" @pointerup="drag = null" @pointercancel="drag = null">
          <img :src="urls[active.attachment_id]" :alt="label(active)" :class="{ actual }" :style="{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`, width: actual ? `${active.width}px` : undefined }" draggable="false" />
        </div>
      <template #actions><button class="outline-button" type="button" aria-label="关闭图片查看" @click="close">关闭</button></template>
    </UiDialog>
  </Teleport>
</template>
<style scoped>
.capture-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:10px}
.capture-grid figure{margin:0;min-width:0}
.capture-thumbnail{display:block;width:100%;padding:0;border:1px solid var(--line-strong);border-radius:10px;overflow:hidden;background:var(--bg);cursor:zoom-in}
.capture-thumbnail img{display:block;width:100%;height:auto;max-height:220px;object-fit:contain}
.capture-grid figcaption{font-size:var(--font-body);color:var(--muted);margin-top:7px}
.capture-placeholder{min-height:110px;display:grid;place-items:center;font-size:var(--font-body)}
.image-viewer-dialog{--dialog-width:1500px}
.image-viewer-dialog :deep(.apply-dialog){height:100%}
.image-viewer-dialog :deep(.dialog-body){flex:1;display:flex;overflow:hidden}
.image-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:var(--font-body)}
.image-tools button{min-height:var(--compact-control-height);padding:6px 10px;font-size:var(--font-body)}
.image-tools button[aria-pressed=true]{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 30%,transparent);background:color-mix(in srgb,var(--accent) 12%,transparent)}
.image-stage{flex:1;min-width:0;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;touch-action:none;cursor:grab}
.image-stage img{max-width:100%;max-height:100%;object-fit:contain;flex-shrink:0;user-select:none}
.image-stage img.actual{max-width:none;max-height:none;height:auto}
@media(max-width:650px){.capture-grid{grid-template-columns:1fr}}
</style>

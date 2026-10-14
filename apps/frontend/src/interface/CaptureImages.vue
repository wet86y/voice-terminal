<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { ImageAttachment } from '../types'
import { acquireImage, releaseImage, onImageReconnect } from './image-channel'
const props = defineProps<{ attachments: ImageAttachment[] }>()
const urls = ref<Record<string, string>>({})
const errors = ref<Record<string, string>>({})
const selected = ref<number | null>(null)
const zoom = ref(1), actual = ref(false), offset = ref({ x: 0, y: 0 })
const panel = ref<HTMLElement | null>(null)
const active = computed(() => selected.value === null ? null : props.attachments[selected.value])
let retained: ImageAttachment[] = [], generation = 0, previous: HTMLElement | null = null
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
async function open(index: number) { previous = document.activeElement as HTMLElement; selected.value = index; reset(); await nextTick(); panel.value?.focus() }
function close() { selected.value = null; previous?.focus() }
function key(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
  if (e.key === 'Tab' && panel.value) {
    const items = [...panel.value.querySelectorAll<HTMLElement>('button:not(:disabled)')]
    if (e.shiftKey && (document.activeElement === items[0] || document.activeElement === panel.value)) { e.preventDefault(); items.at(-1)?.focus() }
    else if (!e.shiftKey && (document.activeElement === items.at(-1) || document.activeElement === panel.value)) { e.preventDefault(); items[0]?.focus() }
  }
}
function begin(e: PointerEvent) { drag = { x: e.clientX, y: e.clientY, ox: offset.value.x, oy: offset.value.y }; (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId) }
function move(e: PointerEvent) { if (drag) offset.value = { x: drag.ox + e.clientX - drag.x, y: drag.oy + e.clientY - drag.y } }
const stopReconnect = onImageReconnect(() => { props.attachments.filter(a => errors.value[a.attachment_id]).forEach(load) })
onBeforeUnmount(() => { stopReconnect(); generation++; retained.forEach(releaseImage); if (selected.value !== null) previous?.focus() })
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
    <div v-if="active" class="image-viewer-backdrop" @click.self="close">
      <section ref="panel" class="image-viewer" role="dialog" aria-modal="true" aria-label="屏幕图片查看" tabindex="-1" @keydown="key">
        <header><strong>{{ label(active) }}</strong><div class="image-tools">
          <button v-for="(a, i) in attachments" :key="a.attachment_id" :aria-pressed="selected === i" @click="selected = i; reset()">{{ label(a) }}</button>
          <button aria-label="缩小图片" @click="zoom = Math.max(.25, zoom / 1.25)">−</button><span>{{ Math.round(zoom * 100) }}%</span>
          <button aria-label="放大图片" @click="zoom = Math.min(8, zoom * 1.25)">＋</button>
          <button @click="actual = !actual; zoom = 1; offset = { x: 0, y: 0 }">{{ actual ? '适应窗口' : '实际像素' }}</button><button @click="reset">复位</button><button aria-label="关闭图片查看" @click="close">×</button>
        </div></header>
        <div class="image-stage" @pointerdown="begin" @pointermove="move" @pointerup="drag = null" @pointercancel="drag = null">
          <img :src="urls[active.attachment_id]" :alt="label(active)" :class="{ actual }" :style="{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`, width: actual ? `${active.width}px` : undefined }" draggable="false" />
        </div>
      </section>
    </div>
  </Teleport>
</template>
<style scoped>
.capture-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:10px}.capture-grid figure{margin:0;min-width:0}.capture-thumbnail{display:block;width:100%;padding:0;border:1px solid #ffffff20;border-radius:10px;overflow:hidden;background:#111827;cursor:zoom-in}.capture-thumbnail img{display:block;width:100%;height:auto;max-height:220px;object-fit:contain}.capture-grid figcaption{font-size:12px;opacity:.7;margin-top:7px}.capture-placeholder{min-height:110px;display:grid;place-items:center;font-size:13px}.image-viewer-backdrop{position:fixed;inset:0;z-index:10000;background:#030712dd;display:flex;align-items:center;justify-content:center;padding:3vw}.image-viewer{width:min(1500px,94vw);height:90vh;background:#151b26;color:#f8fafc;border:1px solid #ffffff30;border-radius:16px;display:flex;flex-direction:column;overflow:hidden}.image-viewer header{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;padding:14px;border-bottom:1px solid #ffffff20}.image-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.image-tools button{background:#ffffff10;color:inherit;border:1px solid #ffffff25;border-radius:6px;padding:6px 10px;cursor:pointer}.image-tools button[aria-pressed=true]{background:#5b74c755}.image-stage{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;touch-action:none;cursor:grab;padding:16px}.image-stage img{max-width:100%;max-height:100%;object-fit:contain;flex-shrink:0;user-select:none}.image-stage img.actual{max-width:none;max-height:none;height:auto}@media(max-width:650px){.capture-grid{grid-template-columns:1fr}.image-viewer-backdrop{padding:8px}.image-viewer{height:96vh;width:100%}}
</style>

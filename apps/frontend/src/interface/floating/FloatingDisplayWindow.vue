<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import FloatingDisplayDocument from './FloatingDisplayDocument.vue'
import {
  DISPLAY_BOUNDS, displayClockStatus, parseFloatingDisplay, setDisplayFocus, startDisplayClock,
  type DisplayClock, type DisplayDocument, type DisplayEvent, type DisplayFrame, type DisplayRequest,
} from './floating-display'

const props = defineProps<{
  request: DisplayRequest | null // request（当前正文与时长）。
  frame?: DisplayFrame | null // frame（固定画布内的卡片区域）。
  resolveImage: (reference: string) => string // resolveImage（解析当前窗口可加载的图片引用）。
  dispatch: (event: DisplayEvent) => Promise<{ visible: number | null; focused: boolean } | null> // dispatch（向宿主报告状态并取得实际回执）。
}>()
const measure = ref<HTMLElement | null>(null) // measure（按紧凑尺寸排版的测量区域）。
const candidate = shallowRef<DisplayDocument | null>(null)
const current = shallowRef<{ id: number; document: DisplayDocument; font: number } | null>(null)
const ratio = ref(0) // ratio（边框着色的剩余时间比例）。
const focused = ref(false)
const fading = ref(false)
let clock: DisplayClock | null = null
let tickTimer: ReturnType<typeof setTimeout> | undefined
let hoverSequence = Promise.resolve()

function chooseFont(element: HTMLElement, document: DisplayDocument) {
  for (const pixels of [18, 16, 14]) {
    element.style.setProperty('--display-selected-font', pixels + 'px')
    if (element.getBoundingClientRect().height > DISPLAY_BOUNDS.height || element.scrollWidth > DISPLAY_BOUNDS.width) continue
    let lines = 0
    for (const line of element.querySelectorAll('.floating-display-line')) {
      const height = Number.parseFloat(getComputedStyle(line).lineHeight)
      lines += Math.max(1, Math.round(line.getBoundingClientRect().height / height))
    }
    if (lines <= document.lineLimit) return pixels
  }
  throw new Error('在紧凑展示框内排不下，请缩短正文或图注。')
}

async function decodeImage(image: HTMLImageElement, signal: AbortSignal) {
  let timeout: ReturnType<typeof setTimeout> | undefined
  let interrupt: (() => void) | undefined
  try {
    await Promise.race([
      image.decode(),
      new Promise<never>((_, reject) => {
        interrupt = () => reject(new Error('本次图片加载已取消。'))
        signal.addEventListener('abort', interrupt, { once: true })
        timeout = setTimeout(() => reject(new Error('图片加载超时。')), 3000)
      }),
    ])
  } catch {
    throw new Error('图片加载失败，请检查图片地址。')
  } finally {
    clearTimeout(timeout)
    if (interrupt) signal.removeEventListener('abort', interrupt)
  }
}

function clear() {
  clearTimeout(tickTimer)
  current.value = null
  clock = null
  focused.value = false
  fading.value = false
}

function updateClock() {
  clearTimeout(tickTimer)
  if (!clock || !current.value) return
  const status = displayClockStatus(clock, performance.now())
  ratio.value = status.ratio
  fading.value = status.expired
  if (status.expired) {
    const id = current.value.id
    tickTimer = setTimeout(async () => {
      if (current.value?.id !== id || clock?.focused) return
      await props.dispatch({ kind: 'display_status', id, status: 'expired' })
      if (current.value?.id === id && !clock?.focused) clear()
    }, 160)
  } else if (status.ratio > 0) {
    tickTimer = setTimeout(updateClock, 80)
  }
}

function setFocus(next: boolean) {
  if (!clock || !current.value || clock.focused === next) return
  clock = setDisplayFocus(clock, next, performance.now())
  const id = current.value.id
  focused.value = next
  updateClock()
  // 同一个窗口的连续移入和移出按原顺序调整原生窗口。
  hoverSequence = hoverSequence.then(async () => {
    if (current.value?.id === id) await props.dispatch({ kind: 'hover', id, focused: next })
  })
}

async function prepare(request: DisplayRequest, signal: AbortSignal) {
  try {
    const document = parseFloatingDisplay(request.content, props.resolveImage)
    candidate.value = document
    await nextTick()
    if (signal.aborted || !measure.value) return
    const font = chooseFont(measure.value, document)
    const image = measure.value.querySelector('img')
    if (image) await decodeImage(image, signal)
    if (signal.aborted) return
    const result = await props.dispatch({ kind: 'display_status', id: request.id, status: 'visible' })
    if (signal.aborted || result?.visible !== request.id) return
    const keepFocus = focused.value || result.focused
    current.value = { id: request.id, document, font }
    focused.value = keepFocus
    clock = startDisplayClock(performance.now(), request.duration_seconds, keepFocus)
    updateClock()
    if (keepFocus !== result.focused) await props.dispatch({ kind: 'hover', id: request.id, focused: keepFocus })
  } catch (error) {
    if (!signal.aborted) {
      const reason = error instanceof Error ? error.message : '图文排版未通过校验。'
      await props.dispatch({ kind: 'display_status', id: request.id, status: 'rejected', reason })
    }
  } finally {
    if (!signal.aborted) candidate.value = null
  }
}

watch(() => props.request?.id, (_id, _old, onCleanup) => {
  const request = props.request
  if (!request) {
    clear()
    candidate.value = null
    return
  }
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  void prepare(request, controller.signal)
}, { immediate: true })
onBeforeUnmount(clear)
</script>

<template>
  <article v-if="current" class="floating-display-card" :class="{ focused, fading }" aria-label="图文展示"
    :style="{
      '--display-selected-font': current.font + 'px', '--display-time': (ratio * 100) + '%',
      '--display-x': (frame?.x ?? 0) + 'px', '--display-y': (frame?.y ?? 0) + 'px',
      '--display-scale': frame?.scale ?? 1,
    }"
    @pointerenter="setFocus(true)" @pointerleave="setFocus(false)">
    <FloatingDisplayDocument :document="current.document" />
  </article>
  <div v-if="candidate" ref="measure" class="floating-display-measure" aria-hidden="true">
    <FloatingDisplayDocument :document="candidate" />
  </div>
</template>

<style>
:root, html, body, #app { margin: 0; min-width: 0; min-height: 0; width: 100%; height: 100%; overflow: hidden; background: transparent; }
.floating-display-card, .floating-display-measure {
  box-sizing: border-box;
  width: 256px;
  padding: 11px 13px;
  --display-font-size: var(--display-selected-font, var(--font-section));
}
.floating-display-card {
  /* 图文固定排版，只动画合成层。展示窗的实际画布不会在悬停时反复改变尺寸。 */
  position: absolute;
  left: 0;
  top: 0;
  height: 160px;
  display: grid;
  align-content: center;
  color: var(--text);
  background: var(--panel-soft);
  border-radius: 12px;
  transform-origin: top left;
  transform: translate3d(var(--display-x, 0), var(--display-y, 0), 0) scale(var(--display-scale, 1));
  will-change: transform;
  transition: transform var(--duration) cubic-bezier(.3333, 1, .6667, 1), opacity 140ms ease;
}
.floating-display-card::before,
.floating-display-card::after {
  content: "";
  position: absolute;
  inset: 0;
  border: 1px solid var(--line-strong);
  border-radius: inherit;
  pointer-events: none;
}
.floating-display-card::after {
  border-color: var(--accent);
  opacity: var(--display-time, 0%);
  transition: opacity 120ms linear;
}
.floating-display-card.focused::after { opacity: 1; }
.floating-display-card.fading { opacity: 0; }
.floating-display-measure { position: absolute; inset: 0 auto auto 0; visibility: hidden; pointer-events: none; }
@media (prefers-reduced-motion: reduce) {
  .floating-display-card, .floating-display-card::after { transition: none; }
}
</style>

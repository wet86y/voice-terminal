<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { FloatingCaption } from './floating-window'

const props = defineProps<{ caption: FloatingCaption /* caption（只包含语音文本的单行字幕）。 */ }>()
const viewport = ref<HTMLElement | null>(null) // viewport（单行可见区域）。
const focus = ref<HTMLElement | null>(null) // focus（当前识别或播报片段）。
const singleLine = computed(() => props.caption.text.replace(/[\r\n\t]/g, ' ')) // singleLine（单行正文）：保留字符串下标。
const parts = computed(() => ({
  before: singleLine.value.slice(0, props.caption.focusStart),
  current: singleLine.value.slice(props.caption.focusStart, props.caption.focusEnd),
  after: singleLine.value.slice(props.caption.focusEnd),
}))
let previousKey = ''
let observer: ResizeObserver | undefined

function centerFocus() {
  if (!viewport.value || !focus.value) return
  const target = focus.value.offsetLeft + focus.value.offsetWidth / 2 - viewport.value.clientWidth / 2
  const left = Math.max(0, Math.min(target, viewport.value.scrollWidth - viewport.value.clientWidth))
  const behavior = previousKey === props.caption.key ? 'smooth' : 'instant'
  previousKey = props.caption.key
  if (typeof viewport.value.scrollTo === 'function') viewport.value.scrollTo({ left, behavior })
  else viewport.value.scrollLeft = left
}

watch(() => [props.caption.key, props.caption.text, props.caption.focusStart, props.caption.focusEnd],
  () => { void nextTick(centerFocus) }, { immediate: true, flush: 'post' })
onMounted(() => {
  if (typeof ResizeObserver === 'undefined' || !viewport.value) return
  observer = new ResizeObserver(centerFocus)
  observer.observe(viewport.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <!-- floating-caption（悬浮字幕气泡）：沿用界面配色，独立绘制向上斜指主体的尾巴。 -->
  <article class="floating-caption" :class="`floating-caption-${caption.role}`"
    :aria-label="caption.role === 'user' ? '你的语音字幕' : '助手的语音字幕'" role="status">
    <div class="floating-caption-bubble">
      <svg class="floating-caption-tail" viewBox="0 0 24 16" aria-hidden="true">
        <path d="M2 16C6 14 9 9 11 6C12.5 3.8 14.5 3.2 16 4.4C17.5 5.6 16.5 7.2 16.5 8.5C16.5 11.6 18.7 14.1 22 16" />
      </svg>
      <div ref="viewport" class="floating-caption-viewport">
        <span class="floating-caption-track"><span>{{ parts.before }}</span><span ref="focus" class="floating-caption-focus">{{ parts.current }}</span><span>{{ parts.after }}</span></span>
      </div>
    </div>
  </article>
</template>

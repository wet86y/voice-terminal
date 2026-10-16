<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { FloatingCaption as Caption, FloatingWindowModel } from './floating-window'
import FloatingCaption from './FloatingCaption.vue'

const props = defineProps<{
  model: FloatingWindowModel // model（悬浮窗扩展数据模型）。
}>()

const emit = defineEmits<{
  returnToWorkbench: [] // returnToWorkbench（切回主工作台）。
  drag: [] // drag（拖动悬浮窗）。
  toggleSilence: [] // toggleSilence（切换静默与语音识别）。
}>()

const DRAG_THRESHOLD_PX = 6 // DRAG_THRESHOLD_PX（拖动判定位移阈值）：越过后立即交给原生窗口拖动。
const control = ref<HTMLButtonElement | null>(null)
const caption = ref<Caption | null>(null)
const dragging = ref(false)
const actionLabel = computed(() => props.model.silenced ? '恢复语音识别' : '进入静默')
let press: { id: number; x: number; y: number; moved: boolean } | null = null
let clickTimer: ReturnType<typeof setTimeout> | undefined
let captionTimer: ReturnType<typeof setTimeout> | undefined
let suppressClick = false

function clearClick() {
  if (clickTimer !== undefined) clearTimeout(clickTimer)
  clickTimer = undefined
}
function endPress() {
  if (press && control.value?.hasPointerCapture?.(press.id)) control.value.releasePointerCapture(press.id)
  press = null
  dragging.value = false
}
function beginPress(event: PointerEvent) {
  if (event.button !== 0 || event.isPrimary === false) return
  endPress()
  clearClick()
  suppressClick = false
  press = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false }
  control.value?.setPointerCapture?.(event.pointerId)
}
function movePress(event: PointerEvent) {
  if (!press || event.pointerId !== press.id) return
  if (press.moved || Math.hypot(event.clientX - press.x, event.clientY - press.y) <= DRAG_THRESHOLD_PX) return
  press.moved = true
  suppressClick = true
  dragging.value = true
  clearClick()
  // 越过系统拖动阈值后立即释放网页捕获，让原生窗口接管仍按住的鼠标。
  if (control.value?.hasPointerCapture?.(press.id)) control.value.releasePointerCapture(press.id)
  emit('drag')
}
function finishPress() {
  suppressClick ||= Boolean(press?.moved || dragging.value)
  endPress()
}
function cancelPress() {
  suppressClick = true
  endPress()
  clearClick()
}
function click(event: MouseEvent) {
  if (event.button !== 0 || (suppressClick && event.detail !== 0)) return
  if (event.detail === 0) {
    clearClick()
    if (props.model.canControl) emit('toggleSilence')
    return
  }
  // 第二次点击先取消待决单击；仅双击事件负责进入工作台。
  if (event.detail > 1 || clickTimer !== undefined) { clearClick(); return }
  clickTimer = setTimeout(() => {
    clickTimer = undefined
    if (props.model.canControl) emit('toggleSilence')
  }, props.model.doubleClickMs)
}
function doubleClick(event: MouseEvent) {
  if (event.button !== 0 || suppressClick) return
  clearClick()
  endPress()
  emit('returnToWorkbench')
}

watch([
  () => props.model.caption?.key, () => props.model.caption?.text,
  () => props.model.caption?.focusStart, () => props.model.caption?.focusEnd, () => props.model.caption?.active,
], () => {
  const value = props.model.caption
  if (captionTimer !== undefined) clearTimeout(captionTimer)
  captionTimer = undefined
  if (value) caption.value = value
  if (!value?.active && caption.value) captionTimer = setTimeout(() => { caption.value = null }, 2500)
}, { immediate: true })
onMounted(() => window.addEventListener('blur', cancelPress))
onBeforeUnmount(() => {
  cancelPress()
  if (captionTimer !== undefined) clearTimeout(captionTimer)
  window.removeEventListener('blur', cancelPress)
})
defineSlots<{
  content(props: { model: FloatingWindowModel }): unknown // content（接收模型的内容插槽）。
}>()
</script>

<template>
  <main
    class="floating-window-shell"
    aria-label="语音悬浮窗"
    @contextmenu.prevent
  >
    <button ref="control" type="button" class="floating-orb-control" :class="{ dragging }"
      :aria-label="actionLabel" :aria-pressed="model.silenced"
      :title="`${actionLabel} · 双击打开工作台 · 按住拖动`"
      @pointerdown="beginPress" @pointermove="movePress" @pointerup="finishPress"
      @pointercancel="cancelPress" @click="click" @dblclick="doubleClick">
      <slot name="content" :model="model" />
    </button>
    <Transition name="floating-caption"><FloatingCaption v-if="caption" :caption="caption" /></Transition>
  </main>
</template>

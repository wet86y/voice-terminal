<script setup lang="ts">
import type { FloatingWindowModel } from './floating-window'

defineProps<{
  model: FloatingWindowModel // model（悬浮窗扩展数据模型）。
}>()

const emit = defineEmits<{
  returnToWorkbench: [] // returnToWorkbench（切回主工作台）。
  drag: [] // drag（拖动悬浮窗）。
}>()

function beginDrag(event: PointerEvent) {
  if (event.button !== 0 || (event.target as HTMLElement).closest('.voice-orb')) return
  emit('drag')
}

defineSlots<{
  content(props: { model: FloatingWindowModel }): unknown // content（接收模型的内容插槽）。
}>()
</script>

<template>
  <main
    class="floating-window-shell"
    aria-label="语音悬浮窗"
    @pointerdown="beginDrag"
    @dblclick="emit('returnToWorkbench')"
  >
    <slot name="content" :model="model" />
  </main>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, shallowRef } from 'vue'
import FloatingDisplayWindow from '../interface/floating/FloatingDisplayWindow.vue'
import {
  FLOATING_LAYOUT_EVENT, resolveFloatingImage, type DisplayEvent, type DisplayFrame, type DisplayRequest,
} from '../interface/floating/floating-display'
import { desktopPort } from './desktop-port'
import '../interface/design.css'

interface DisplayLayout {
  revision: number // revision（布局版本）。
  active: boolean // active（是否处于悬浮模式）。
  request: DisplayRequest | null // request（待显示的图文）。
  visible: number | null // visible（当前显示的编号）。
  focused: boolean // focused（鼠标关注状态）。
  display_frame: DisplayFrame | null // display_frame（画布内的展示区域）。
}

const port = desktopPort()
const snapshot = shallowRef<DisplayLayout | null>(null)
let unlisten: (() => void) | undefined
let disposed = false

function accept(layout: DisplayLayout) {
  if (!disposed && layout.revision >= (snapshot.value?.revision ?? 0)) snapshot.value = layout
}

async function dispatch(action: DisplayEvent | { kind: 'snapshot' }) {
  if (!port.floatingWindow || disposed) return null
  try {
    const layout = await port.floatingWindow<DisplayLayout>(action)
    accept(layout)
    return layout
  } catch {
    // 窗口已关闭或切换模式时，迟到的展示回执不再取得控制权。
    return null
  }
}

onMounted(async () => {
  try {
    unlisten = await port.listen?.<DisplayLayout>(FLOATING_LAYOUT_EVENT, event => accept(event.payload))
    if (disposed) { unlisten?.(); return }
    await dispatch({ kind: 'snapshot' })
  } catch {
    // 本页无独立业务状态，日后由桌面重新加载恢复。
  }
})
onBeforeUnmount(() => { disposed = true; unlisten?.() })
</script>

<template>
  <FloatingDisplayWindow :request="snapshot?.active ? snapshot.request : null" :frame="snapshot?.display_frame"
    :resolve-image="resolveFloatingImage" :dispatch="dispatch" />
</template>

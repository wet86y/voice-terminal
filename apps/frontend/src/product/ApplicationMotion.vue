<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { startupIcon } from '../runtime/startup-screen'
import { APPLICATION_EXIT_EVENT, APPLICATION_MOTION_MS, readLogoRect, takeStartupLogo, type LogoRect } from '../runtime/application-motion'
import './application-motion.css'

const props = defineProps<{
  blocked: boolean // blocked（当前需要显示加载或故障页面）。
  stopping: boolean // stopping（宿主正在退出）。
}>()
type MotionPhase = 'loading' | 'entering' | 'shown' | 'exiting' // MotionPhase（界面过渡阶段）。
const phase = ref<MotionPhase>(props.stopping ? 'exiting' : props.blocked ? 'loading' : 'entering')
const root = ref<HTMLElement | null>(null) // root（本窗口的动效容器）。
const flight = ref<HTMLImageElement | null>(null) // flight（从加载页移动到主页的图标副本）。
const source = ref<LogoRect | null>(null) // source（移动起点的真实位置与大小）。
const entering = computed(() => phase.value === 'entering')
const exiting = computed(() => phase.value === 'exiting')
const flightStyle = computed(() => source.value ? {
  left: `${source.value.x}px`, top: `${source.value.y}px`,
  width: `${source.value.width}px`, height: `${source.value.height}px`,
} : undefined)
let completed = false // completed（本窗口已完成入场）：运行中恢复组件不重复播放启动动效。
let mounted = false // mounted（图标与目标节点已经挂载）。
let generation = 0 // generation（动效代次）：退出时废弃尚未执行的入场任务。
let timer: ReturnType<typeof setTimeout> | undefined // timer（过渡结束后的状态收尾）。
let animation: Animation | undefined // animation（移动图标的网页动画句柄）。

function cancelMotion() {
  generation++
  if (timer !== undefined) clearTimeout(timer)
  timer = undefined
  animation?.cancel()
  animation = undefined
  source.value = null
}

function finishEntry() {
  cancelMotion()
  completed = true
  phase.value = props.blocked ? 'loading' : 'shown'
}

async function enter() {
  const bounds = takeStartupLogo() ?? readLogoRect(root.value?.querySelector('.startup-icon') ?? null)
  cancelMotion()
  const current = generation
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    finishEntry()
    return
  }
  phase.value = 'entering'
  source.value = bounds
  await nextTick()
  if (generation !== current || exiting.value) return
  const target = root.value?.querySelector<HTMLElement>('[data-home-logo]')
  const destination = readLogoRect(target ?? null)
  // SVG（矢量图标）主体边长为 200/256；主页主体已旋转，按未旋转边长换算以免放大两次。
  const size = target ? Number.parseFloat(getComputedStyle(target).width) * 256 / 200 : 0
  if (bounds && destination && size > 0 && typeof flight.value?.animate === 'function') {
    const x = destination.x + destination.width / 2 - size / 2 - bounds.x
    const y = destination.y + destination.height / 2 - size / 2 - bounds.y
    const transform = `translate(${x}px, ${y}px) scale(${size / bounds.width}, ${size / bounds.height})`
    animation = flight.value.animate([
      { transform: 'translate(0, 0) scale(1)', opacity: 1, offset: 0, easing: 'cubic-bezier(.22, 1, .36, 1)' },
      { transform, opacity: 1, offset: .8 },
      { transform, opacity: 0, offset: 1 },
    ], { duration: APPLICATION_MOTION_MS, easing: 'linear', fill: 'forwards' })
  } else {
    // 目标没有布局或动画接口不可用时仍完成淡入，避免残留图标挡住主页。
    source.value = null
  }
  timer = setTimeout(finishEntry, APPLICATION_MOTION_MS)
}

function exit() {
  cancelMotion()
  phase.value = 'exiting'
}

function reconcile() {
  if (props.stopping || exiting.value) { exit(); return }
  if (props.blocked) { cancelMotion(); phase.value = 'loading'; return }
  if (!completed) void enter()
  else phase.value = 'shown'
}

// flush（刷新时机）使用同步：加载浮层拆除前先读取起点，移动时不猜测坐标。
watch(() => [props.blocked, props.stopping], () => { if (mounted) reconcile() }, { flush: 'sync' })
function resize() { if (entering.value) finishEntry() }
onMounted(() => {
  mounted = true
  if (props.blocked) takeStartupLogo()
  window.addEventListener(APPLICATION_EXIT_EVENT, exit)
  window.addEventListener('resize', resize)
  reconcile()
})
onBeforeUnmount(() => {
  mounted = false
  cancelMotion()
  window.removeEventListener(APPLICATION_EXIT_EVENT, exit)
  window.removeEventListener('resize', resize)
})
</script>

<template>
  <div ref="root" class="application-presentation" :data-motion="phase" :style="{ '--application-motion-duration': `${APPLICATION_MOTION_MS}ms` }" :inert="entering || exiting ? true : undefined">
    <slot :entering="entering" :exiting="exiting" />
    <img v-if="source && entering" ref="flight" class="application-logo-flight" :src="startupIcon" :style="flightStyle" alt="" aria-hidden="true">
  </div>
</template>

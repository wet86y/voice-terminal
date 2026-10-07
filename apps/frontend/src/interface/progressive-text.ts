import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

// grapheme（完整可见字符）：表情、组合音标和中文字都不会在中间被拆开。
const segmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : null
const characters = (text: string) => segmenter
  ? Array.from(segmenter.segment(text), item => item.segment)
  : Array.from(text)

/** useProgressiveText（快速逐字呈现）：保留已显示前缀，后续帧始终追随最新正文。 */
export function useProgressiveText(source: () => string, animateOnMount = true, staticText: () => boolean = () => false) {
  const displayed = ref(animateOnMount ? '' : source()) // displayed（当前可见正文）。
  const motion = typeof window === 'undefined' ? undefined : window.matchMedia?.('(prefers-reduced-motion: reduce)')
  let target = characters(source()) // target（最新完整正文的字符序列）。
  let shown = characters(displayed.value) // shown（已呈现的完整字符）。
  let frame: number | undefined // frame（唯一待执行的动画帧）。
  let previousTime = 0, credit = 0, rate = 120 / 1000 // rate（每毫秒字符数）：普通短句约每秒 120 字。
  let disposed = false

  function cancel() {
    if (frame !== undefined) cancelAnimationFrame(frame)
    frame = undefined
  }
  function finish() {
    cancel()
    shown = target
    displayed.value = target.join('')
    credit = 0
  }
  function immediate() {
    return staticText() || motion?.matches || typeof requestAnimationFrame !== 'function'
      || (typeof document !== 'undefined' && document.hidden)
  }
  function advance(now: number) {
    frame = undefined
    if (disposed) return
    if (immediate()) { finish(); return }
    credit += Math.min(100, Math.max(0, now - previousTime)) * rate
    previousTime = now
    const count = Math.floor(credit)
    if (count > 0) {
      credit -= count
      shown = target.slice(0, shown.length + count)
      displayed.value = shown.join('')
    }
    if (shown.length < target.length) frame = requestAnimationFrame(advance)
    else credit = 0
  }
  function update() {
    target = characters(source())
    if (immediate()) { finish(); return }
    let prefix = 0
    while (prefix < shown.length && prefix < target.length && shown[prefix] === target[prefix]) prefix++
    if (prefix < shown.length) {
      shown = shown.slice(0, prefix)
      displayed.value = shown.join('')
      credit = 0
    }
    if (shown.length === target.length) { finish(); return }
    // backlog（积压字符）较多时加速，长段落约 650 毫秒追上，避免落后于播报。
    rate = Math.max(120 / 1000, (target.length - shown.length) / 650)
    if (frame === undefined) {
      previousTime = performance.now()
      frame = requestAnimationFrame(advance)
    }
  }
  const onVisibility = () => { if (document.hidden) finish() }
  const onMotion = () => { if (motion?.matches) finish() }
  watch([source, staticText], update, { immediate: true })
  onMounted(() => {
    document.addEventListener('visibilitychange', onVisibility)
    motion?.addEventListener?.('change', onMotion)
  })
  onBeforeUnmount(() => {
    disposed = true
    cancel()
    document.removeEventListener('visibilitychange', onVisibility)
    motion?.removeEventListener?.('change', onMotion)
  })
  return displayed
}

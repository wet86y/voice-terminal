import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

// grapheme（完整可见字符）：表情、组合音标和中文字都不会在中间被拆开。
const segmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : null
const characters = (text: string) => segmenter
  ? Array.from(segmenter.segment(text), item => item.segment)
  : Array.from(text)

interface ProgressiveTextOptions {
  correctInPlace?: () => boolean // correctInPlace（原位修订）：用户改字保留已呈现范围，不撤回整段正文。
  streaming?: () => boolean // streaming（识别流仍在更新）：按批次节奏平滑新增尾部。
}

/** useProgressiveText（逐字呈现）：保留显示进度，识别改字原位更新，新增正文在有界时间内追上。 */
export function useProgressiveText(source: () => string, animateOnMount = true, staticText: () => boolean = () => false,
  options: ProgressiveTextOptions = {}) {
  const correctInPlace = options.correctInPlace ?? (() => false)
  const streaming = options.streaming ?? (() => false)
  const displayed = ref(animateOnMount ? '' : source()) // displayed（当前可见正文）。
  const motion = typeof window === 'undefined' ? undefined : window.matchMedia?.('(prefers-reduced-motion: reduce)')
  let latestText = source() // latestText（最新已接收的正文）：重复事件不改变显示节奏。
  let target = characters(latestText) // target（最新完整正文的字符序列）。
  let shown = characters(displayed.value) // shown（已呈现的完整字符）。
  let frame: number | undefined // frame（唯一待执行的动画帧）。
  let previousTime = 0, credit = 0, rate = 120 / 1000 // rate（每毫秒字符数）：普通短句约每秒 120 字。
  let lastArrival: number | undefined // lastArrival（识别正文上次变化时刻）。
  let arrivalInterval = 240 // arrivalInterval（识别批次间隔的平滑估计，毫秒）。
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
    const nextText = source()
    const changed = nextText !== latestText
    const previousTarget = target // previousTarget（修订前的全文）：用于保留修订后的已显示尾部。
    if (changed) {
      latestText = nextText
      target = characters(nextText)
    }
    if (streaming() && (changed || lastArrival === undefined)) {
      const now = performance.now()
      if (lastArrival !== undefined) {
        const interval = now - lastArrival
        if (interval > 1500) arrivalInterval = 240
        else if (interval >= 32) arrivalInterval += (interval - arrivalInterval) * 0.35
      }
      lastArrival = now
    } else if (!streaming()) lastArrival = undefined
    if (immediate()) { finish(); return }
    let prefix = 0
    while (prefix < shown.length && prefix < target.length && shown[prefix] === target[prefix]) prefix++
    if (prefix < shown.length) {
      if (correctInPlace()) {
        // suffix（未改变的末尾）：插入、删除或改字时，将显示位置映射到新正文，避免正确后缀消失。
        let suffix = 0
        while (suffix < previousTarget.length - prefix && suffix < target.length - prefix
          && previousTarget[previousTarget.length - 1 - suffix] === target[target.length - 1 - suffix]) suffix++
        const visibleLength = suffix > 0 && shown.length >= previousTarget.length - suffix
          ? shown.length + target.length - previousTarget.length : shown.length
        shown = target.slice(0, Math.max(0, visibleLength))
      } else shown = shown.slice(0, prefix)
      displayed.value = shown.join('')
      credit = 0
    }
    if (shown.length === target.length) { finish(); return }
    const backlog = target.length - shown.length // backlog（尚未呈现的字符数）。
    // 识别批次最多用 280 毫秒呈现；不等待下一包文字，也不拖慢助手输出。
    const revealWindow = Math.max(100, Math.min(280, arrivalInterval * 0.65))
    rate = streaming() ? Math.max(30 / 1000, backlog / revealWindow)
      : Math.max(120 / 1000, backlog / 650)
    if (frame === undefined) {
      previousTime = performance.now()
      frame = requestAnimationFrame(advance)
    }
  }
  const onVisibility = () => { if (document.hidden) finish() }
  const onMotion = () => { if (motion?.matches) finish() }
  watch([source, staticText, streaming, correctInPlace], update, { immediate: true })
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

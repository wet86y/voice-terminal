/** DisplayRun（正文片段）：由固定排版语法生成的纯文本，强调片段使用加粗。 */
export interface DisplayRun { text: string; bold: boolean }

/** DisplayBlock（图文块）：按模型提供的顺序放入展示框。 */
export type DisplayBlock =
  | { kind: 'title' | 'paragraph' | 'bullet'; runs: DisplayRun[] }
  | { kind: 'image'; src: string; description: string }

/** DisplayDocument（展示正文）：一屏图文及实际排版行数上限。 */
export interface DisplayDocument {
  blocks: DisplayBlock[]
  lineLimit: number
}

/** DisplayRequest（展示请求）：每次展示只需图文正文与初始时长。 */
export interface DisplayRequest {
  id: number // id（宿主分配的展示编号）。
  content: string // content（图文正文）。
  duration_seconds: number // duration_seconds（初始展示秒数）。
}

/** DisplayFrame（固定画布内的可见卡片）：位置为逻辑像素，比例以紧凑卡片为基准。 */
export interface DisplayFrame {
  x: number // x（左侧偏移）。
  y: number // y（顶部偏移）。
  scale: number // scale（图文缩放比例）。
}

/** DisplayEvent（展示窗口回执）：报告排版结果、到期和鼠标关注。 */
export type DisplayEvent =
  | { kind: 'hover'; id: number; focused: boolean }
  | { kind: 'display_status'; id: number; status: 'visible' | 'rejected' | 'expired'; reason?: string }

/** DISPLAY_BOUNDS（展示框尺寸）：先在紧凑态验收完整正文。 */
export const DISPLAY_BOUNDS = { width: 256, height: 160 }

/** resolveFloatingImage（解析展示图片）：只接受公开图片使用的加密地址。 */
export function resolveFloatingImage(reference: string) {
  let source: URL
  try { source = new URL(reference) } catch { throw new Error('请使用有效的加密图片直链。') }
  if (source.protocol !== 'https:' || source.username || source.password) {
    throw new Error('请使用有效的加密图片直链。')
  }
  return source.href
}

/** FloatingDisplayMode（正式悬浮模式）：代次用于丢弃退出或重进前的投递。 */
export interface FloatingDisplayMode {
  enabled: boolean // enabled（正式悬浮球已显示）。
  generation: number // generation（模式代次）。
}

/** FloatingDisplayDelivery（核心投递）：空正文和空时长代表取消。 */
export interface FloatingDisplayDelivery {
  generation: number // generation（模式代次）。
  delivery: number // delivery（本模式下的投递编号）。
  content: string | null // content（已提交的图文正文）。
  duration_seconds: number | null // duration_seconds（展示时长）。
}

/** FloatingDisplayReceipt（桌面回执）：只返回展示结果及失效原因。 */
export interface FloatingDisplayReceipt {
  generation: number // generation（模式代次）。
  delivery: number // delivery（投递编号）。
  status: 'visible' | 'rejected' | 'expired' | 'cleared' // status（展示结果）。
  reason: string | null // reason（供模型修正的固定原因）。
}

export const FLOATING_MODE_EVENT = 'voice-terminal:floating-mode' // FLOATING_MODE_EVENT（正式模式事件）。
export const FLOATING_LAYOUT_EVENT = 'voice-terminal:floating-layout' // FLOATING_LAYOUT_EVENT（子窗口布局事件）。
export const FLOATING_RESULT_EVENT = 'voice-terminal:floating-display-result' // FLOATING_RESULT_EVENT（图文结果事件）。

function textRuns(text: string): DisplayRun[] {
  return text.split(/(\*\*[^*\r\n]+\*\*)/g).filter(Boolean).map(part => part.startsWith('**') && part.endsWith('**') && part.length > 4
    ? { text: part.slice(2, -2), bold: true }
    : { text: part, bold: false })
}

/** parseFloatingDisplay（读取展示正文）：固定标题、段落、短列表与一张图。 */
export function parseFloatingDisplay(content: string, resolveImage: (reference: string) => string): DisplayDocument {
  if (content.length > 4096) throw new Error('展示正文太长，请简写后再提交。')
  const blocks: DisplayBlock[] = []
  let picture = false
  let characters = 0
  let textLines = 0
  for (const line of content.split(/\r?\n/)) {
    const source = line.trim()
    if (!source) continue
    const image = /^!\[([^\]]*)\]\((\S+)\)$/.exec(source)
    if (image) {
      if (picture || image[2]!.includes('![')) throw new Error('每条展示最多允许一张图片。')
      picture = true
      const description = image[1]!.trim()
      characters += Array.from(description).length
      blocks.push({ kind: 'image', src: resolveImage(image[2]!), description })
      continue
    }
    if (source.includes('![')) throw new Error('请把图片放在单独一行。')
    const title = source.startsWith('# ')
    const bullet = source.startsWith('- ')
    if (title && textLines > 0) throw new Error('标题最多一行，请放在正文前。')
    const runs = textRuns(title || bullet ? source.slice(2) : source)
    for (const run of runs) characters += Array.from(run.text).length
    textLines++
    blocks.push({ kind: title ? 'title' : bullet ? 'bullet' : 'paragraph', runs })
  }
  if (!blocks.length) throw new Error('请提供要展示的文字或图片。')
  if (characters > 500) throw new Error('正文和图注共需控制在 500 字符以内。')
  const lineLimit = picture ? 3 : 6
  if (textLines > lineLimit) throw new Error('内容超出紧凑框的行数，请简写。')
  return { blocks, lineLimit }
}

/** DisplayClock（展示计时）：使用单调时钟，关注期间可以停在零秒。 */
export interface DisplayClock {
  deadline: number // deadline（当前到期时间，毫秒）。
  interval: number // interval（本次计时所用总时长，毫秒）。
  focused: boolean // focused（鼠标正在关注卡片）。
}

export function startDisplayClock(now: number, seconds: number, focused: boolean): DisplayClock {
  const interval = seconds * 1000
  return { interval, deadline: now + interval, focused }
}

export function setDisplayFocus(clock: DisplayClock, focused: boolean, now: number): DisplayClock {
  if (focused === clock.focused) return clock
  if (focused) return { ...clock, focused }
  const interval = Math.max(0, clock.deadline - now) + 5000
  return { deadline: now + interval, interval, focused }
}

export function displayClockStatus(clock: DisplayClock, now: number) {
  const remaining = Math.max(0, clock.deadline - now)
  return {
    ratio: Math.min(1, remaining / clock.interval), // ratio（用于边框着色的剩余比例）。
    expired: remaining === 0 && !clock.focused,
  }
}

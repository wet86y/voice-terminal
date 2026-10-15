import { computed, ref, watch, type Ref } from 'vue'
import type { useVoiceTerminal } from '../composables/useVoiceTerminal'
import type { WorkbenchModel } from '../interface/workbench'
import { isRecord } from '../record'
import type { FloatingCaption, FloatingWindowProjection } from './floating-window'

/** FloatingWorkbench（动效来源）：仅开放主页面动效与控件状态，不开放聊天列表。 */
type FloatingWorkbench = Pick<WorkbenchModel, 'state' | 'sessionActive' | 'audioLevels' | 'disabled'>

/** splitSurrogate（编码单元边界）：当前下标是否落在一个表情字符的中间。 */
function splitSurrogate(text: string, index: number) {
  return index > 0 && index < text.length && /[\uD800-\uDBFF]/.test(text[index - 1]!) && /[\uDC00-\uDFFF]/.test(text[index]!)
}

/** userCaption（识别字幕）：定位本次改字或新增的片段，避免每次都滚回句首。 */
export function userCaption(key: string, text: string, active: boolean, previous: FloatingCaption | null): FloatingCaption | null {
  if (!text.trim()) return null
  let offset = Math.max(0, text.length - 32000)
  if (splitSurrogate(text, offset)) offset++
  const visible = text.slice(offset)
  let start = Math.max(0, visible.length - Array.from(visible).slice(-6).join('').length)
  let end = visible.length
  if (previous?.key === key && previous.role === 'user') {
    if (previous.text === visible) { start = previous.focusStart; end = previous.focusEnd }
    else {
      let prefix = 0
      while (prefix < Math.min(previous.text.length, visible.length) && previous.text[prefix] === visible[prefix]) prefix++
      let suffix = 0
      while (suffix < Math.min(previous.text.length, visible.length) - prefix
        && previous.text[previous.text.length - 1 - suffix] === visible[visible.length - 1 - suffix]) suffix++
      start = Math.min(prefix, Math.max(0, visible.length - 1))
      end = Math.max(start + 1, visible.length - suffix)
    }
  }
  if (splitSurrogate(visible, start)) start--
  if (splitSurrogate(visible, end)) end++
  return { key, role: 'user', text: visible, focusStart: start, focusEnd: end, active }
}

/** playbackCaption（播放字幕）：只消费后端按实际音频位置对齐的纯文本范围。 */
export function playbackCaption(key: string, value: unknown): FloatingCaption | null {
  if (!isRecord(value) || typeof value.text !== 'string' || !value.text.trim()
    || 'attachments' in value || 'images' in value
    || !['word', 'sentence'].includes(String(value.precision))) return null
  const points = Array.from(value.text)
  if (typeof value.start !== 'number' || !Number.isInteger(value.start) || value.start < 0
    || typeof value.end !== 'number' || !Number.isInteger(value.end) || value.end <= value.start || value.end > points.length) return null
  // Python（后端语言）按字符计数，浏览器按 UTF-16（双字节编码单元）计数；表情符号也需正确定位。
  const start = points.slice(0, value.start).join('').length
  const end = points.slice(0, value.end).join('').length
  let offset = Math.max(0, start - 16000)
  if (splitSurrogate(value.text, offset)) offset++
  let limit = Math.min(value.text.length, offset + 32000)
  if (splitSurrogate(value.text, limit)) limit--
  const text = value.text.slice(offset, limit)
  return { key, role: 'assistant', text, focusStart: start - offset, focusEnd: Math.min(end - offset, text.length), active: true }
}

/** useFloatingWindowProjection（悬浮窗投影）：只读取语音输入与播放队列，截图聊天列表不参与。 */
export function useFloatingWindowProjection(voice: ReturnType<typeof useVoiceTerminal>, workbench: Readonly<Ref<FloatingWorkbench>>) {
  const user = ref<FloatingCaption | null>(null)
  const output = ref<FloatingCaption | null>(null)
  const lastRole = ref<'user' | 'assistant'>('user')
  watch([
    () => voice.currentTurnId.value, () => voice.current.partial_text,
    () => voice.current.final_text, () => voice.listening.value,
  ], () => {
    const text = voice.current.partial_text || voice.current.final_text
    if (!voice.listening.value || !text.trim()) { user.value = null; return }
    user.value = userCaption(`input:${voice.currentTurnId.value ?? 'current'}`, text,
      Boolean(voice.current.partial_text), user.value)
    lastRole.value = 'user'
  }, { immediate: true, flush: 'sync' })
  watch(() => voice.playbackQueue.value.find(item => item.state === 'playing')?.speech_id, value => {
    output.value = null
    if (value) lastRole.value = 'assistant'
  }, { flush: 'sync' })
  watch(() => voice.speechProgress.value, value => {
    const playing = voice.playbackQueue.value.find(item => item.state === 'playing')
    if (!playing || !value || value.speech_id !== playing.speech_id || value.conversation_id !== playing.conversation_id
      || (value.output_generation !== undefined && playing.output_generation !== undefined
        && value.output_generation !== playing.output_generation)) return
    const caption = playbackCaption(`speech:${playing.conversation_id}:${playing.speech_id}`, value.playback_caption)
    if (caption) output.value = caption
  }, { flush: 'sync' })
  return computed<FloatingWindowProjection>(() => {
    const playing = voice.playbackQueue.value.find(item => item.state === 'playing')
    const key = playing ? `speech:${playing.conversation_id}:${playing.speech_id}` : ''
    const spokenText = playing?.text ?? ''
    const fallback: FloatingCaption | null = spokenText.trim()
      ? { key, role: 'assistant', text: spokenText.slice(0, 32000), focusStart: 0,
        focusEnd: Array.from(spokenText)[0]?.length ?? 0, active: true } : null
    const caption = user.value?.active ? user.value : playing
      ? output.value?.key === key ? output.value : fallback
      : lastRole.value === 'user' ? user.value : null
    return {
      // 动效严格镜像工作台；字幕仍独立跟随实际输出，不推导另一套运行状态。
      state: workbench.value.state,
      sessionActive: workbench.value.sessionActive ?? ['listening', 'thinking', 'speaking'].includes(workbench.value.state),
      audioLevels: workbench.value.audioLevels ?? voice.audioLevels.value,
      silenced: !voice.listening.value,
      canControl: voice.connection.value === 'connected' && !workbench.value.disabled
        && (voice.listening.value || voice.canStart.value),
      caption,
    }
  })
}

import type { VoiceOrbModel } from '../interface/voice-orb'

/** FloatingCaption（单行语音字幕）：只允许识别和实际播报文本，不接受聊天附件或工具消息。 */
export interface FloatingCaption {
  key: string // key（语音输入轮次或播报事务标识）。
  role: 'user' | 'assistant' // role（说话方）：用户或语音助手。
  text: string // text（纯文本字幕）：禁止图片与截图消息进入此接口。
  focusStart: number // focusStart（当前片段起点）：浏览器字符串下标。
  focusEnd: number // focusEnd（当前片段终点）：浏览器字符串下标。
  active: boolean // active（识别或播放正在进行）：结束后延时收起。
}

/** FloatingWindowProjection（悬浮窗展示快照）：主窗口持有真实语音会话。 */
export interface FloatingWindowProjection extends VoiceOrbModel {
  silenced: boolean // silenced（输入处于静默）：再次单击直接启动语音识别。
  canControl: boolean // canControl（语音控件可用）：启动中或连接断开时禁止切换。
  caption: FloatingCaption | null // caption（当前纯语音字幕）：不从聊天列表投影截图。
}

/** FloatingWindowModel（悬浮窗数据模型）：带递增版本的展示快照。 */
export interface FloatingWindowModel extends FloatingWindowProjection {
  revision: number // revision（递增版本）：忽略晚到的旧状态。
  doubleClickMs: number // doubleClickMs（系统双击间隔毫秒数）：避免慢速双击误触单击。
}

/** FLOATING_WINDOW_MODEL_EVENT（悬浮窗状态事件）：携带只读动效快照。 */
export const FLOATING_WINDOW_MODEL_EVENT = 'voice-terminal:floating-model'
/** FLOATING_WINDOW_ERROR_EVENT（悬浮窗错误事件）：通知主工作台入口切换失败。 */
export const FLOATING_WINDOW_ERROR_EVENT = 'voice-terminal:floating-error'
/** FLOATING_WINDOW_CONTROL_EVENT（悬浮窗语音操作）：由原生窗口转交主工作台。 */
export const FLOATING_WINDOW_CONTROL_EVENT = 'voice-terminal:floating-control'
/** FLOATING_WINDOW_WORKBENCH_EVENT（返回工作台事件）：恢复主页而非停留在设置页。 */
export const FLOATING_WINDOW_WORKBENCH_EVENT = 'voice-terminal:floating-workbench'
/** FloatingSessionAction（语音输入操作）：使用明确目标，避免迟到点击反向切换。 */
export type FloatingSessionAction = 'silence' | 'start'

/** readFloatingCaption（字幕边界校验）：丢弃截图、工具消息、空白及异常负载。 */
export function readFloatingCaption(value: unknown): FloatingCaption | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  if ((item.role !== 'user' && item.role !== 'assistant') || typeof item.key !== 'string'
    || typeof item.text !== 'string' || !item.text.trim() || item.text.length > 32000
    || 'attachments' in item || 'images' in item) return null
  if (typeof item.focusStart !== 'number' || !Number.isInteger(item.focusStart)
    || typeof item.focusEnd !== 'number' || !Number.isInteger(item.focusEnd)
    || item.focusStart < 0 || item.focusEnd < item.focusStart || item.focusEnd > item.text.length) return null
  return {
    key: item.key, role: item.role, text: item.text,
    focusStart: item.focusStart, focusEnd: item.focusEnd, active: item.active === true,
  }
}

/** EMPTY_FLOATING_WINDOW_MODEL（初始悬浮窗模型）：尚无主工作台更新时显示待机。 */
export const EMPTY_FLOATING_WINDOW_MODEL: FloatingWindowModel = {
  revision: 0,
  doubleClickMs: 500,
  state: 'standby',
  sessionActive: false,
  audioLevels: [0, 0, 0, 0, 0],
  silenced: true,
  canControl: false,
  caption: null,
}

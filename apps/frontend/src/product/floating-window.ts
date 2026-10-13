import type { VoiceOrbModel } from '../interface/voice-orb'

/** FloatingWindowModel（悬浮窗数据模型）：可扩展的窗口视图数据，首版只包含语音动效。 */
export interface FloatingWindowModel extends VoiceOrbModel {
  revision: number // revision（递增版本）：忽略晚到的旧状态。
}

/** FLOATING_WINDOW_MODEL_EVENT（悬浮窗状态事件）：携带只读动效快照。 */
export const FLOATING_WINDOW_MODEL_EVENT = 'voice-terminal:floating-model'
/** FLOATING_WINDOW_ERROR_EVENT（悬浮窗错误事件）：通知主工作台入口切换失败。 */
export const FLOATING_WINDOW_ERROR_EVENT = 'voice-terminal:floating-error'

/** EMPTY_FLOATING_WINDOW_MODEL（初始悬浮窗模型）：尚无主工作台更新时显示待机。 */
export const EMPTY_FLOATING_WINDOW_MODEL: FloatingWindowModel = {
  revision: 0,
  state: 'standby',
  sessionActive: false,
  audioLevels: [0, 0, 0, 0, 0],
}

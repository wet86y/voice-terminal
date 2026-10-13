/** VoiceOrbModel（语音动效数据）：主工作台与悬浮窗共用的最小动效输入。 */
export interface VoiceOrbModel {
  state: string // state（运行状态）。
  sessionActive: boolean // sessionActive（会话是否处于活动布局）。
  audioLevels: readonly number[] // audioLevels（五频带归一化音量）。
}

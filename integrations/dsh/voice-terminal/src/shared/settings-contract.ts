/** Shared settings vocabulary for the Voice Terminal DSH plugin. */

/** DSH settings namespace owned by the Voice Terminal Bridge. */
export const VOICE_TERMINAL_SETTINGS_NAMESPACE = 'voice-terminal-shadow-bridge'

/** DSH global model settings（宿主唯一的全局模型设置），与正式会话选择器共用。 */
export const DSH_GLOBAL_MODEL_NAMESPACE = 'agent-default-model'

/** Durable options consumed only when the Bridge opens a fresh voice session. */
export interface VoiceTerminalSettings {
  /** Selected workspace id（默认工作区标识）；the DSH registry id remains stable across path spelling changes. */
  readonly workspaceId?: string | undefined
  /** Project workspace id（项目默认工作区标识）；maintained by the Bridge as a fallback target. */
  readonly defaultWorkspaceId?: string | undefined
  /** Fallback notice（回退提示）；maintained by the Bridge for the settings page. */
  readonly fallbackNotice?: 'workspace-unavailable' | undefined
}

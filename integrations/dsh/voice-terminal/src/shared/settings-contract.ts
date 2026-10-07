/** Shared settings vocabulary for the Voice Terminal DSH plugin. */

/** DSH settings namespace owned by the Voice Terminal Bridge. */
export const VOICE_TERMINAL_SETTINGS_NAMESPACE = 'voice-terminal-shadow-bridge'

/** Durable options consumed only when the Bridge opens a fresh voice session. */
export interface VoiceTerminalSettings {
  /** Selected workspace id（默认工作区标识）；the DSH registry id remains stable across path spelling changes. */
  readonly workspaceId?: string | undefined
  /** Project workspace id（项目默认工作区标识）；maintained by the Bridge as a fallback target. */
  readonly defaultWorkspaceId?: string | undefined
  /** Provider route（模型供应方）；unset means inherit the current DSH global model. */
  readonly provider?: string | undefined
  /** Provider model id（模型标识）；unset means inherit the current DSH global model. */
  readonly model?: string | undefined
  /** Reasoning effort（推理强度）；unset means use the selected model's default. */
  readonly reasoningEffort?: string | undefined
  /** Fallback notice（回退提示）；maintained by the Bridge for the settings page. */
  readonly fallbackNotice?: 'workspace-unavailable' | 'model-unavailable' | 'reasoning-unavailable' | undefined
}

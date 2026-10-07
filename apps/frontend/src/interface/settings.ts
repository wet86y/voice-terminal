/** settingsSections（设置分区）：label（页面名称）、hint（导航说明）与 description（页面副标题）由正式窗口与独立展示共用。 */
export const settingsSections = [
  { id: "wake", label: "唤醒与静默", hint: "待机、唤醒词与时间", description: "唤醒短语与静默时段", icon: "◌" },
  { id: "persona", label: "人格与指令", hint: "偏好与协作方式", description: "人格风格与协作指令", icon: "✦" },
  { id: "speaker", label: "声纹识别", hint: "身份与打断策略", description: "身份识别与打断策略", icon: "♬" },
  { id: "wakes", label: "唤醒任务", hint: "定时与循环提醒", description: "定时唤醒与循环提醒", icon: "◷" },
  { id: "models", label: "模型管理", hint: "接口密钥", description: "DeepSeek（语言模型）与 MiniMax（语音合成）接口密钥", icon: "◇" },
  { id: "compute", label: "本地组件", hint: "运行状态与资源", description: "运行状态与资源占用", icon: "▦" },
] as const;
export type SettingsSection = (typeof settingsSections)[number]["id"];

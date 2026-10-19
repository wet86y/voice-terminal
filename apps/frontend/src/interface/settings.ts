/** settingsSections（设置分区）：label（页面名称）、hint（导航说明）与 description（页面副标题）由正式窗口与独立展示共用。 */
export const settingsSections = [
  { id: "tts", label: "语音与音色", hint: "播放目标与本地音色", description: "选择语音目标，设计与克隆本地音色", icon: "♫" },
  { id: "wake", label: "唤醒与静默", hint: "待机、唤醒词与时间", description: "唤醒短语与静默时段", icon: "◌" },
  { id: "persona", label: "人格与指令", hint: "偏好与协作方式", description: "人格风格与协作指令", icon: "✦" },
  { id: "speaker", label: "识别与修订", hint: "身份识别与名称纠错", description: "声纹身份识别、打断策略与热词修订", icon: "♬" },
  { id: "wakes", label: "唤醒任务", hint: "定时与循环提醒", description: "定时唤醒与循环提醒", icon: "◷" },
  { id: "compute", label: "组件设置", hint: "分类配置与运行状态", description: "按用途管理语音、推理与扩展组件", icon: "▦" },
  { id: "workspace", label: "关于", hint: "应用更新与工作区", description: "应用更新与工作区迁移", icon: "⌂" },
] as const;
export type SettingsSection = (typeof settingsSections)[number]["id"];

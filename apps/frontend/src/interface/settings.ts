/** settingsSections（设置分区）：label（页面名称）、hint（导航说明）与 description（页面副标题）由正式窗口与独立展示共用。 */
export const settingsSections = [
  { id: "tts", label: "语音与音色", hint: "播放目标与本地音色", description: "选择语音目标，设计与克隆本地音色", icon: "♫" },
  { id: "wake", label: "唤醒与静默", hint: "待机、唤醒词与时间", description: "唤醒短语与静默时段", icon: "◌" },
  { id: "hotwords", label: "热词替换", hint: "专有名称与读音", description: "识别后的名称纠正与词表管理", icon: "字" },
  { id: "persona", label: "人格与指令", hint: "偏好与协作方式", description: "人格风格与协作指令", icon: "✦" },
  { id: "speaker", label: "声纹识别", hint: "身份与打断策略", description: "身份识别与打断策略", icon: "♬" },
  { id: "wakes", label: "唤醒任务", hint: "定时与循环提醒", description: "定时唤醒与循环提醒", icon: "◷" },
  { id: "compute", label: "组件设置", hint: "分类配置与运行状态", description: "按用途管理语音、推理与扩展组件", icon: "▦" },
  { id: "workspace", label: "关于", hint: "应用更新与工作区", description: "应用更新与工作区迁移", icon: "⌂" },
] as const;
export type SettingsSection = (typeof settingsSections)[number]["id"];

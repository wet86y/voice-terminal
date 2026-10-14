/** settingsSections（设置分区）：label（页面名称）、hint（导航说明）与 description（页面副标题）由正式窗口与独立展示共用。 */
export const settingsSections = [
  { id: "screen", label: "屏幕图像", hint: "截屏与局部中心", description: "按需观察屏幕与图像尺寸", icon: "▧" },
  { id: "wake", label: "唤醒与静默", hint: "待机、唤醒词与时间", description: "唤醒短语与静默时段", icon: "◌" },
  { id: "hotwords", label: "热词替换", hint: "专有名称与读音", description: "识别后的名称纠正与词表管理", icon: "字" },
  { id: "persona", label: "人格与指令", hint: "偏好与协作方式", description: "人格风格与协作指令", icon: "✦" },
  { id: "speaker", label: "声纹识别", hint: "身份与打断策略", description: "身份识别与打断策略", icon: "♬" },
  { id: "wakes", label: "唤醒任务", hint: "定时与循环提醒", description: "定时唤醒与循环提醒", icon: "◷" },
  { id: "models", label: "模型管理", hint: "接口密钥", description: "DeepSeek（语言模型）与 MiniMax（语音合成）接口密钥", icon: "◇" },
  { id: "compute", label: "本地组件", hint: "运行状态与资源", description: "运行状态与资源占用", icon: "▦" },
  { id: "workspace", label: "关于与工作区", hint: "版本与数据位置", description: "应用版本、工作区位置与迁移", icon: "⌂" },
] as const;
export type SettingsSection = (typeof settingsSections)[number]["id"];

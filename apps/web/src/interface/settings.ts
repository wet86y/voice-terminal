/** settingsSections（设置导航）：正式窗口与独立展示共用。 */
export const settingsSections = [
  { id: "wake", label: "唤醒与静默", hint: "待机、唤醒词与时间", icon: "◌" },
  { id: "persona", label: "人格与指令", hint: "偏好与协作方式", icon: "✦" },
  { id: "speaker", label: "声纹识别", hint: "身份与打断策略", icon: "♬" },
  { id: "wakes", label: "唤醒任务", hint: "定时与循环提醒", icon: "◷" },
  { id: "models", label: "模型管理", hint: "连接与角色分配", icon: "◇" },
  { id: "compute", label: "本地计算", hint: "推理设备与资源", icon: "▦" },
  { id: "system", label: "系统运行", hint: "组件、日志与诊断", icon: "◎" },
] as const;
export type SettingsSection = (typeof settingsSections)[number]["id"];

/** APPLICATION_MOTION_MS（启动与退出界面过渡时长）：与桌面窗口的最短退出展示时间一致。 */
export const APPLICATION_MOTION_MS = 1000
/** APPLICATION_EXIT_EVENT（桌面退出通知）：原生按钮、托盘及实际退出共用。 */
export const APPLICATION_EXIT_EVENT = 'voice-terminal:exiting'

/** LogoRect（图标视口位置）：使用网页逻辑像素，适配窗口尺寸与显示缩放。 */
export interface LogoRect {
  x: number // x（左侧坐标）。
  y: number // y（顶部坐标）。
  width: number // width（图标宽度）。
  height: number // height（图标高度）。
}

let startupRect: LogoRect | null = null // startupRect（挂载界面之前保留的首屏图标位置）。

/** readLogoRect（读取图标位置）：隐藏或未完成布局的节点不参与移动。 */
export function readLogoRect(element: Element | null): LogoRect | null {
  const bounds = element?.getBoundingClientRect()
  return bounds && bounds.width > 0 && bounds.height > 0
    ? { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height } : null
}

/** captureStartupLogo（保留首屏图标）：跳过加载时也能从预检画面的真实位置入场。 */
export function captureStartupLogo() {
  startupRect = readLogoRect(document.querySelector('#app .startup-icon'))
}

/** takeStartupLogo（消费首屏位置）：每次挂载只使用一次，避免后续页面重用旧坐标。 */
export function takeStartupLogo(): LogoRect | null {
  const bounds = startupRect
  startupRect = null
  return bounds
}

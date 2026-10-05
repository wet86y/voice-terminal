import { locateNativeInvoker } from '../runtime/desktop-preflight'

/** DesktopPort（桌面交互接口）：正式产品入口通过桌面预检后才调用原生命令。 */
export interface DesktopPort {
  /** available（是否存在原生命令桥）。 */
  available: boolean
  /** focus（聚焦）：显示桌面主窗口。 */
  focus(): Promise<void>;
  /** openLogs（打开日志）：显示本机应用日志目录。 */
  openLogs(): Promise<void>;
  /** openConsole（打开控制台）：由 Host（应用宿主）校验并在默认浏览器打开 DSH（代理执行宿主）；url（短期令牌地址）。 */
  openConsole(url: string): Promise<void>
  /** exit（退出）：由桌面宿主关闭应用。 */
  exit(): Promise<void>;
}

export function desktopPort(): DesktopPort {
  const native = locateNativeInvoker()
  const call = (command: string, args?: Record<string, unknown>) =>
    native
      ? native.invoke<void>(command, args)
      : Promise.reject(new Error('此操作需要桌面窗口。'))
  return {
    available: Boolean(native),
    focus: () => call('focus_main'),
    openLogs: () => call('open_logs'),
    openConsole: url => call('open_console', { url }),
    exit: () => call('exit_application'),
  }
}

/** DesktopPort（桌面交互接口）：浏览器调试不依赖原生运行时。 */
export interface DesktopPort {
  available: boolean; // available（是否运行于受管桌面窗口）。
  focus(): Promise<void>;
  openLogs(): Promise<void>;
  openConsole(url: string): Promise<void>; // openConsole（在系统默认浏览器打开代理控制台）；url（带访问令牌的本机地址）。
  exit(): Promise<void>;
}

type NativeWindow = Window & {
  __TAURI_INTERNALS__?: {
    invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>;
  };
};
export function desktopPort(): DesktopPort {
  const native = (window as NativeWindow).__TAURI_INTERNALS__;
  const call = (command: string, args?: Record<string, unknown>) =>
    native
      ? native.invoke<void>(command, args)
      : Promise.reject(new Error("此操作需要桌面窗口。"));
  return {
    available: Boolean(native),
    focus: () => call("focus_main"),
    openLogs: () => call("open_logs"),
    openConsole: (url) => call("open_console", { url }),
    exit: () => call("exit_application"),
  };
}

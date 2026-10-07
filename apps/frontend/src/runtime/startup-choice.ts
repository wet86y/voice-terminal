const SKIP_KEY = 'voice-terminal-startup-skipped' // SKIP_KEY（当前产品窗口跳过加载页的会话标记）。

/** rememberStartupSkip（保存跳过选择）：不改变服务的启动或可用状态。 */
export function rememberStartupSkip() {
  try { window.sessionStorage.setItem(SKIP_KEY, '1') } catch { /* 存储不可用不影响当前画面。 */ }
}

/** startupWasSkipped（读取跳过选择）：仅用于同一产品窗口的加载画面接力。 */
export function startupWasSkipped(): boolean {
  try { return window.sessionStorage.getItem(SKIP_KEY) === '1' } catch { return false }
}

/** clearStartupSkip（清除跳过选择）：新应用实例重新展示加载页。 */
export function clearStartupSkip() {
  try { window.sessionStorage.removeItem(SKIP_KEY) } catch { /* 存储受限不影响启动。 */ }
}

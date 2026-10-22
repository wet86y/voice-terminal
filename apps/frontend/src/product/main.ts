import { locateNativeInvoker, runDesktopPreflight } from '../runtime/desktop-preflight'
import { loadProductApplicationConfig } from './application-config'
import { rememberStartupSkip, clearStartupSkip } from '../runtime/startup-choice'

// 只有原生状态与入口模式均通过后，才请求 Host 配置并加载 Vue（界面框架）应用。
void runDesktopPreflight({
  expectedMode: 'product',
  onSkip: rememberStartupSkip,
  startApplication: async status => {
    if (status.startup_skipped) rememberStartupSkip()
    else clearStartupSkip()
    await loadProductApplicationConfig()
    const application = await import('./application')
    const app = application.mountProductApplication()
    try { await locateNativeInvoker()?.invoke('desktop_page_ready') } // desktop_page_ready（产品页真实挂载确认）。
    catch (error) { app.unmount(); throw error }
  },
})

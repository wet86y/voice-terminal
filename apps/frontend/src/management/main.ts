import { locateNativeInvoker, runDesktopPreflight } from '../runtime/desktop-preflight'

// 本地管理界面不请求业务接口；核验、模块加载及挂载失败均由统一启动页承接。
void runDesktopPreflight({
  expectedMode: 'product', management: true,
  startApplication: async () => {
    const { mountManagementApplication } = await import('./application')
    const app = await mountManagementApplication()
    try { await locateNativeInvoker()?.invoke('desktop_page_ready') }
    catch (error) { app.unmount(); throw error }
  },
})

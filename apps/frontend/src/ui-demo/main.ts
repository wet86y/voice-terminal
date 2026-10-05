import { runDesktopPreflight } from '../runtime/desktop-preflight'

// UI preview（界面样例）仅校验原生桌面状态，不读取正式产品配置。
void runDesktopPreflight({
  expectedMode: 'ui-preview',
  startApplication: async () => {
    const application = await import('./application')
    application.mountUiDemoApplication()
  },
})

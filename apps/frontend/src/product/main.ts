import { runDesktopPreflight } from '../runtime/desktop-preflight'
import { loadProductApplicationConfig } from './application-config'

// 只有原生状态与入口模式均通过后，才请求 Host 配置并加载 Vue（界面框架）应用。
void runDesktopPreflight({
  expectedMode: 'product',
  startApplication: async () => {
    await loadProductApplicationConfig()
    const application = await import('./application')
    application.mountProductApplication()
  },
})

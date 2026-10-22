import { createApp, type Component } from 'vue'
import { assertStartupActive, finishStartupFallback } from './desktop-preflight'

/** mountApplication（挂载受控界面）：捕获首轮渲染异常，禁止空界面报告已就绪。 */
export function mountApplication(component: Component) {
  assertStartupActive()
  const root = document.getElementById('app')
  if (!root) throw new Error('应用挂载容器缺失，请重新加载。')
  const app = createApp(component)
  let failure: unknown
  let mounting = true
  app.config.errorHandler = error => {
    if (mounting) failure = error
    else console.error('界面组件运行失败', error)
  }
  try {
    app.mount(root)
    if (failure) throw failure
    if (!root.firstElementChild) throw new Error('应用界面未生成可显示内容，请重试。')
    mounting = false
    // 原生交接可能较慢；真实界面已挂载后立即停止模块加载计时。
    finishStartupFallback()
    return app
  } catch (error) {
    app.unmount()
    root.removeAttribute('data-v-app')
    throw error
  }
}

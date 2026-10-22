import { nextTick } from 'vue'
import { mountApplication } from '../runtime/mount-application'
import ManagementApp from './ManagementApp.vue'
import '../interface/design.css'
import '../product/application.css'

/** mountManagementApplication（挂载管理页）：延迟加载失败仍留在统一启动页。 */
export async function mountManagementApplication() {
  const app = mountApplication(ManagementApp)
  await nextTick()
  // 两次绘制回调后才交接旧窗口，确保新界面已经有可显示内容。
  await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
  return app
}

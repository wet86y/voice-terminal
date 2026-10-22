import { mountApplication } from '../runtime/mount-application'
import UiDemoApp from './UiDemoApp.vue'
import '../interface/design.css'
import './preview-application.css'

/** mountUiDemoApplication（挂载样例界面）：不连接产品业务或设备。 */
export function mountUiDemoApplication() {
  return mountApplication(UiDemoApp)
}

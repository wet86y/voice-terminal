import { createApp } from 'vue'
import UiDemoApp from './UiDemoApp.vue'
import '../interface/design.css'
import './preview-application.css'

/** mountUiDemoApplication（挂载样例界面）：不连接产品业务或设备。 */
export function mountUiDemoApplication() {
  createApp(UiDemoApp).mount('#app')
}

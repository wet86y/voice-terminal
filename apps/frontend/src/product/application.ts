import { createApp } from 'vue'
import ProductApp from './ProductApp.vue'
import '../interface/design.css'

/** mountProductApplication（挂载产品界面）：由受控桌面预检入口延迟调用。 */
export function mountProductApplication() {
  createApp(ProductApp).mount('#app')
}

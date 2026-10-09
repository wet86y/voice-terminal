import { createApp, nextTick } from 'vue'
import { locateNativeInvoker } from '../runtime/desktop-preflight'
import ManagementApp from './ManagementApp.vue'
import '../interface/design.css'
import '../product/application.css'

// 本地管理界面不请求业务接口；仍需原生窗口和产品模式授权。
const native = locateNativeInvoker()
if (native) {
  void native.invoke<{ role: string }>('desktop_status').then(async state => {
    if (state.role === 'product') {
      createApp(ManagementApp).mount('#app')
      await nextTick()
      await native.invoke('desktop_page_ready') // desktop_page_ready（管理页真实挂载确认）。
    }
  })
} else document.getElementById('app')!.textContent = '请从语音终端桌面程序打开组件管理。'

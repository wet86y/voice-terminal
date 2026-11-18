import { locateNativeInvoker, runDesktopPreflight } from '../runtime/desktop-preflight'
import { isBundledRemoteProductPage, loadProductApplicationConfig } from './application-config'
import { rememberStartupSkip, clearStartupSkip } from '../runtime/startup-choice'
import { nativeDeviceApi } from './native-device-api'
import { startRemoteClientRuntimeSupervisor } from './remote-client-runtime'

// 只有原生状态与入口模式均通过后，才请求 Host 配置并加载 Vue（界面框架）应用。
void runDesktopPreflight({
  expectedMode: 'product',
  onSkip: rememberStartupSkip,
  startApplication: async status => {
    if (status.startup_skipped) rememberStartupSkip()
    else clearStartupSkip()
    const staticProductPage = typeof window !== 'undefined' && isBundledRemoteProductPage(window.location)
    if (staticProductPage) {
      const device = await nativeDeviceApi.deviceConfigStatus()
      if (device.runtime.mode !== 'remote') {
        throw new Error('程序内置的静态产品页只用于远程配对模式；本地运行需通过本地语音宿主打开。')
      }
      window.__VOICE_APPLICATION__ = {
        managed: true,
        origin: window.location.origin,
        voice_api_url: '',
        voice_ws_url: '',
        remote_mode: true,
        host_id: device.device_config?.paired_host?.host_id ?? null,
        management_connected: false,
        remote_business_ready: false,
        business_ready: false,
      }
    } else {
      await loadProductApplicationConfig()
    }
    const application = await import('./application')
    const app = application.mountProductApplication()
    const stopRemoteSupervisor = staticProductPage ? startRemoteClientRuntimeSupervisor() : undefined
    try { await locateNativeInvoker()?.invoke('desktop_page_ready') } // desktop_page_ready（产品页真实挂载确认）。
    catch (error) { stopRemoteSupervisor?.(); app.unmount(); throw error }
  },
})

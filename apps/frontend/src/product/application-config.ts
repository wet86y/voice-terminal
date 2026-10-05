/** ProductApplicationConfig（产品运行配置）：由受控 Host（应用宿主）提供。 */
export interface ProductApplicationConfig {
  /** managed（受宿主管理）：必须为 true，才能进入正式产品界面。 */
  managed: true
  /** origin（宿主来源）：Host（应用宿主）的受信来源。 */
  origin: string
  /** voice_api_url（语音服务接口地址）：启动早期允许为空。 */
  voice_api_url: string
  /** voice_ws_url（语音实时通信地址）：启动早期允许为空。 */
  voice_ws_url: string
}

/** readProductApplicationConfig（读取并验证产品配置）：不会提供本地默认地址。 */
export function readProductApplicationConfig(value: unknown): ProductApplicationConfig {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('应用宿主没有提供正式运行配置。')
  }
  const config = value as Partial<ProductApplicationConfig>
  if (config.managed !== true || typeof config.origin !== 'string' || !config.origin.trim()
    || typeof config.voice_api_url !== 'string' || typeof config.voice_ws_url !== 'string') {
    throw new Error('应用宿主运行配置缺失或格式无效。')
  }
  return config as ProductApplicationConfig
}

/** loadProductApplicationConfig（加载产品配置）：仅在 desktop_status（桌面状态）通过后调用。 */
export function loadProductApplicationConfig(): Promise<ProductApplicationConfig> {
  if (typeof document === 'undefined' || typeof window === 'undefined') {
    return Promise.reject(new Error('正式运行配置只能在桌面窗口中加载。'))
  }
  const existing = document.querySelector<HTMLScriptElement>('script[data-voice-application-config]')
  existing?.remove()

  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = '/application-config.js'
    script.async = true
    script.dataset.voiceApplicationConfig = 'true'
    script.onload = () => {
      try {
        resolve(readProductApplicationConfig(window.__VOICE_APPLICATION__))
      } catch (error) {
        script.remove()
        reject(error)
      }
    }
    script.onerror = () => {
      script.remove()
      reject(new Error('无法加载应用宿主运行配置，请检查桌面应用会话后重试。'))
    }
    document.head.append(script)
  })
}

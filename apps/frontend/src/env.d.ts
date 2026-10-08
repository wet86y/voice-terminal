/// <reference types="vite/client" />

interface Window {
  /** __VOICE_APPLICATION__（独立启动程序提供的运行连接配置）。 */
  __VOICE_APPLICATION__?: {
    /** managed（受应用管理）。 */
    managed: boolean
    /** origin（应用控制地址）。 */
    origin: string
    /** voice_api_url（语音服务接口地址）。 */
    voice_api_url: string
    /** voice_ws_url（实时语音通信地址）。 */
    voice_ws_url: string
    /** endpoint_revision（语音端点版本）：允许连接器判断旧地址上运行的会话需要重建。 */
    endpoint_revision?: number
    /** business_ready（业务接口门禁）：宿主尚未就绪或会话失效时禁止产品请求。 */
    business_ready?: boolean
  }
  /** __TAURI_INTERNALS__（Tauri 原生命令桥）：仅桌面入口使用。 */
  __TAURI_INTERNALS__?: {
    /** invoke（原生命令调用）：desktop_status（桌面状态）与受控桌面操作。 */
    invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>
  }
}

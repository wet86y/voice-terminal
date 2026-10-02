/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_VOICE_API_URL?: string
  readonly VITE_VOICE_WS_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

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
  }
}

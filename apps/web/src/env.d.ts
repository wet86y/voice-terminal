/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_VOICE_API_URL?: string
  readonly VITE_VOICE_WS_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

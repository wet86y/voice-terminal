import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// UI preview（视觉展示服务）：独立配置，不注入应用宿主脚本，也不代理业务接口。
export default defineConfig({
  cacheDir: resolve(__dirname, '../../.local/cache/vite-demo'), // cacheDir（演示界面开发缓存）。
  plugins: [vue(), {
    name: 'desktop-owned-preview',
    configureServer(server) {
      if (!server.config.server.middlewareMode) throw new Error('请执行 npm run dev:ui（桌面样例开发），不支持直接启动网页服务。')
    },
    configurePreviewServer() { throw new Error('样例只能在调试版桌面程序中打开。') },
  }],
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    open: false,
    watch: { ignored: ['**/*.tmpdir/**', '**/*.tmp'] },
  },
})

import { resolve } from 'node:path'
import { defineConfig, searchForWorkspaceRoot } from 'vite'
import vue from '@vitejs/plugin-vue'
import { startupFallback } from './startup-plugin'

// UI preview（视觉展示服务）：独立配置，不注入应用宿主脚本，也不代理业务接口。
export default defineConfig({
  cacheDir: resolve(__dirname, '../../.local/cache/vite-demo'), // cacheDir（演示界面开发缓存）。
  plugins: [startupFallback(), vue(), {
    name: 'desktop-owned-preview',
    configureServer(server) {
      if (!server.config.server.middlewareMode) throw new Error('请执行 npm run dev:ui（桌面样例开发），不支持直接启动网页服务。')
    },
    configurePreviewServer() { throw new Error('样例只能在调试版桌面程序中打开。') },
  }],
  server: {
    fs: { allow: [searchForWorkspaceRoot(__dirname), resolve(__dirname, '../../docs/releases')] }, // fs（源码读取范围）：演示与产品共用版本文档。
    host: '127.0.0.1',
    port: 0, // port（默认监听请求端口）：由受管外层服务注入实际端口。
    strictPort: true,
    open: false,
    watch: { ignored: ['**/*.tmpdir/**', '**/*.tmp'] },
  },
})

import { resolve } from 'node:path'
import { copyFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  cacheDir: resolve(__dirname, '../../.local/cache/vite-product'), // cacheDir（产品界面开发缓存）。
  plugins: [vue(), {
    name: 'desktop-owned-development',
    closeBundle() {
      const output = resolve(__dirname, '../../.local/build/frontend')
      for (const name of ['startup.css', 'app-icon.svg', 'index.html']) {
        copyFileSync(resolve(__dirname, '../desktop/boot', name), resolve(output, name === 'index.html' ? 'startup.html' : name))
      }
    },
    configureServer(server) {
      // middlewareMode（中间件模式）：外层服务验证请求，禁止直接开网页。
      if (!server.config.server.middlewareMode) throw new Error('请执行 npm run dev（桌面开发），不支持直接启动网页服务。')
    },
    configurePreviewServer() {
      throw new Error('请执行 npm run desktop:start（桌面静态启动），不支持网页预览。')
    },
  }],
  build: {
    outDir: resolve(__dirname, '../../.local/build/frontend'), // outDir（固定界面构建目录）。
    emptyOutDir: true,
    manifest: 'desktop-manifest.json', // manifest（界面产物清单）：便携组装按产品依赖闭包验证。
    rollupOptions: {
      input: {
        index: resolve(__dirname, 'index.html'),
        management: resolve(__dirname, 'management.html'), // management（无后端的本地组件管理入口）。
        floating: resolve(__dirname, 'floating.html'), // floating（桌面悬浮窗的展示与语音控件入口）。
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    open: false,
    watch: {
      // Editors that save atomically drop a transient file next to the target
      // (for example ``.ui-demo.css.<pid>.<uuid>.tmpdir/ui-demo.css.tmp``).
      // Watching it can fail with EBUSY on Windows and that error is emitted on
      // the watcher itself, which takes the whole dev server down.  See
      // https://github.com/vitejs/vite/issues/19117
      ignored: ['**/*.tmpdir/**', '**/*.tmp'],
    },
  },
})

import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue(), {
    name: 'application-host-bootstrap',
    transformIndexHtml() {
      return process.env.VOICE_APP_ORIGIN ? [{ tag: 'script', attrs: { src: '/application-config.js' }, injectTo: 'head-prepend' as const }] : []
    },
  }],
  build: {
    rollupOptions: {
      input: {
        index: resolve(__dirname, 'index.html'),
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: process.env.VOICE_APP_ORIGIN ? Object.fromEntries(
      ['/api/v1/application', '/application-config.js'].map(path => [path, {
        target: process.env.VOICE_APP_ORIGIN, changeOrigin: true,
        headers: { Origin: process.env.VOICE_APP_ORIGIN },
      }]),
    ) : undefined,
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

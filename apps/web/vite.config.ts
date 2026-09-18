import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
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

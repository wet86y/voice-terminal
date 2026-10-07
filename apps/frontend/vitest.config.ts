import { resolve } from 'node:path'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  cacheDir: resolve(__dirname, '../../.local/cache/vitest'), // cacheDir（界面测试缓存）。
  plugins: [vue()],
  test: {
    environment: 'node',
    clearMocks: true,
  },
})

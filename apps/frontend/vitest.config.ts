import { resolve } from 'node:path'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { searchForWorkspaceRoot } from 'vite'

export default defineConfig({
  cacheDir: resolve(__dirname, '../../.local/cache/vitest'), // cacheDir（界面测试缓存）。
  plugins: [vue()],
  server: { fs: { allow: [searchForWorkspaceRoot(__dirname), resolve(__dirname, '../../docs/releases')] } }, // fs（测试源码读取范围）：包含正式版本文档。
  test: {
    environment: 'node',
    clearMocks: true,
  },
})

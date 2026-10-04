import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// UI preview（视觉展示服务）：独立配置，不注入应用宿主脚本，也不代理业务接口。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    open: '/ui-demo.html',
    watch: { ignored: ['**/*.tmpdir/**', '**/*.tmp'] },
  },
})

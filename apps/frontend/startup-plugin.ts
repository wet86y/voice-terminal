import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import type { Plugin } from 'vite'

/** startupFallback（首屏兜底）：内嵌样式与错误处理先于界面模块执行，不依赖资源下载。 */
export function startupFallback(): Plugin {
  const boot = resolve(__dirname, '../desktop/boot')
  return {
    name: 'startup-fallback',
    transformIndexHtml: {
      order: 'pre',
      handler(html) {
        if (!html.includes('class="startup-screen"')) return html
        return { html, tags: [
          { tag: 'style', children: `html,body{margin:0;background:#0e1118}${readFileSync(resolve(boot, 'startup.css'), 'utf8')}`, injectTo: 'head' },
          { tag: 'script', children: readFileSync(resolve(boot, 'startup-guard.js'), 'utf8'), injectTo: 'body-prepend' },
        ] }
      },
    },
  }
}

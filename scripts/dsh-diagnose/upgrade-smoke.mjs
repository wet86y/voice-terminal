// Upgrade smoke（代理升级冒烟检查）：真实加载器、任务共存及释放，成功后回收夹具。
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
const runner = fileURLToPath(new URL('../../integrations/dsh/voice-terminal/test-runner.mjs', import.meta.url))
const result = spawnSync(process.execPath, [runner], { stdio: 'inherit', windowsHide: true })
process.exit(result.status ?? 1)

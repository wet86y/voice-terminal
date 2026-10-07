// Test runner（插件测试执行器）：复用受控源码依赖，成功后回收临时功能包。
import { spawnSync } from 'node:child_process'
import { rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
const here = fileURLToPath(new URL('.', import.meta.url))
const bundle = fileURLToPath(new URL('../../../.local/build/dsh/bundles/voice-terminal-test', import.meta.url))
rmSync(bundle, { recursive: true, force: true })
const build = spawnSync(process.execPath, ['build.mjs', bundle, '--test'], { cwd: here, stdio: 'inherit', windowsHide: true })
if (build.status !== 0) process.exit(build.status ?? 1)
const result = spawnSync(process.execPath, ['--test', 'test/bridge/*.test.mjs', 'test/wake-tasks/*.test.mjs', 'test/*.test.mjs'], { cwd: here, stdio: 'inherit', windowsHide: true })
if (result.status === 0) rmSync(bundle, { recursive: true, force: true })
process.exit(result.status ?? 1)

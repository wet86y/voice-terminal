import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

test('真实 Loader（加载器）组合：单包、预设、工具、界面及组件释放', { timeout: 90_000 }, async () => {
  const script = fileURLToPath(new URL('./fixtures/loader-run.mjs', import.meta.url))
  const child = spawn(process.execPath, [script], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  let output = ''
  for (const pipe of [child.stdout, child.stderr]) pipe.on('data', chunk => { output += chunk })
  const timer = setTimeout(() => child.kill(), 80_000)
  const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve) })
  clearTimeout(timer)
  assert.equal(code, 0, output)
  assert.match(output, /LOADER_COMPOSITION_PASSED/u)
})

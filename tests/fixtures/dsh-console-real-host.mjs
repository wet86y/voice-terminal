/** 隔离的锁定 DSH（代理执行宿主）与正式插件；元数据只经私有标准输出交接。 */
import assert from 'node:assert/strict'
import { cp, mkdir, readFile, symlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { dirname, join, resolve, sep } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { randomUUID } from 'node:crypto'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const runtime = join(repo, '.local/runtimes/dsh')
const cli = join(runtime, 'node_modules/@deepseek-ai/dsh')
const lock = JSON.parse(await readFile(join(repo, 'config/dsh/runtime.lock.json'), 'utf8'))
assert.equal(JSON.parse(await readFile(join(cli, 'package.json'), 'utf8')).version, lock.version,
  '真实网页验收使用当前锁定的正式运行包，避免源码缓存中的旧网页构建')
const temporary = resolve(process.argv[2] ?? '')
assert.ok(temporary.startsWith(join(repo, '.local/tmp/tasks') + sep), '真实网页验收必须使用受管独占测试目录')
const profile = join(temporary, 'home/profiles/console-test')
const token = randomUUID()
const callback = createServer((req, res) => {
  assert.equal(req.headers.authorization, `Bearer ${token}`)
  req.resume()
  res.setHeader('content-type', 'application/json')
  res.end('{"result":"accepted"}')
})
let booted
try {
  await new Promise(resolve => callback.listen(0, '127.0.0.1', resolve))
  Object.assign(process.env, { DSH_HOME:join(temporary, 'home'), VOICE_DATA_DIR:temporary,
    VOICE_DSH_BRIDGE_TOKEN:token, VOICE_DSH_CALLBACK_ORIGIN:`http://127.0.0.1:${callback.address().port}`,
    VOICE_DSH_TASKS_ACTIVE:'1', VOICE_DSH_TASKS_PATH:join(temporary, 'tasks.json'), DSH_TELEMETRY_DISABLED:'1' })
  const built = join(repo, '.local/build/dsh/bundles/voice-terminal')
  const pkg = JSON.parse(await readFile(join(built, 'package.json'), 'utf8'))
  assert.equal(pkg.name, '@voice-terminal/dsh-plugin')
  await mkdir(join(profile, 'node_modules/@voice-terminal'), { recursive:true })
  await symlink(join(runtime, 'node_modules/@deepseek-ai'), join(profile, 'node_modules/@deepseek-ai'), process.platform === 'win32' ? 'junction' : 'dir')
  await cp(built, join(profile, 'node_modules/@voice-terminal/dsh-plugin'), { recursive:true,
    filter:path => path !== join(built, 'node_modules') })
  await writeFile(join(profile, 'package.json'), JSON.stringify({ name:'console-test-profile', private:true,
    dependencies:{ '@voice-terminal/dsh-plugin':pkg.version }, dsh:{ profile:{
      bundles:['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', '@voice-terminal/dsh-plugin'], patchReload:'live',
    } } }))
  await writeFile(join(profile, 'cordis.patch.yml'), [
    '- id: session-log-deepseek', '  disabled: true', '- id: session-title-llm', '  disabled: true',
    '- id: ui-plugin-manager', '  config:', '    registryProbeEnabled: false',
    '- id: web-runtime', '  config:', '    openBrowser: false', '    printUrl: false', '    surfaceContext: true', '',
  ].join('\n'))
  const originalFetch = globalThis.fetch
  globalThis.fetch = (input, init) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url)
    assert.equal(url.hostname, '127.0.0.1', '隔离宿主禁止访问外部网络')
    return originalFetch(input, init)
  }
  process.chdir(temporary)
  const { runProfile } = await import(pathToFileURL(join(cli, 'lib/profile-boot.js')).href)
  const require = createRequire(join(cli, 'package.json'))
  const { loadLayeredEnv } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-app-boot')).href)
  booted = await runProfile({ profile:'console-test', environment:loadLayeredEnv('dsh'), args:['--no-open','--port','0'], patchFiles:[] })
  assert.equal(booted.ctx.clientModules.graph().entries.filter(row => row.id === '@voice-terminal/dsh-plugin').length, 1)
  const origin = `http://127.0.0.1:${booted.ctx.webServer.port}`
  // 此行由隔离 Python（后端）父进程消费，不写普通日志、不交给客户端。
  process.stdout.write('VOICE_DSH_CONSOLE_HOST_READY ' + JSON.stringify({ origin, launch_url:booted.ctx.connection.authenticatedUrl(origin + '/') }) + '\n')
  await new Promise(resolve => {
    process.stdin.once('data', resolve)
    process.stdin.once('end', resolve)
    process.stdin.resume()
  })
  process.stdin.destroy()
} finally {
  await booted?.shutdown.shutdown(0)
  await new Promise(resolve => callback.close(resolve))
}

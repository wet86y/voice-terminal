/** npm adapter（包安装适配器）：原生入口可直接调用，不依赖 Python（后端环境）。 */
import { readFile, writeFile, mkdir, rm, lstat } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { resolve, dirname, join, isAbsolute } from 'node:path'
import { pathToFileURL } from 'node:url'
import { spawn } from 'node:child_process'
import { registryCandidates, registryEnvironment, canRetryRegistry, selectedRegistry as requestedRegistry } from './download-sources.mjs'

export function safePath(root, relative) {
  if (typeof relative !== 'string' || !relative || isAbsolute(relative) || relative.includes('\\') || relative.split('/').some(p => !p || p === '.' || p === '..')) throw new Error('DSH（代理宿主）入口路径无效')
  const path = resolve(root, relative)
  if (!path.startsWith(resolve(root) + '/'.replace('/', process.platform === 'win32' ? '\\' : '/'))) throw new Error('DSH（代理宿主）入口路径越界')
  return path
}
// registriesForAttempt（本轮来源）：由原生任务统一计数，命令行维护工具保持原有顺序回退。
export function registriesForAttempt(registries, attempt) {
  const round = Number(attempt)
  return Number.isInteger(round) && round >= 1 && round <= 3 && registries.length
    ? [registries[(round - 1) % registries.length]] : registries
}
export function targetSpec(lock) {
  const spec = lock.distribution
  if (!spec || spec.source_kind !== 'npm_package' || spec.adapter !== 'npm_cli_v1') throw new Error('DSH（代理宿主）分发适配器不受支持')
  if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/u.test(lock.version) || !/^@[a-z0-9._-]+\/[a-z0-9._-]+$/u.test(spec.package_name)) throw new Error('DSH（代理宿主）必须声明包名与精确版本')
  if (!/^pnpm@\d+\.\d+\.\d+$/u.test(lock.package_manager)) throw new Error('缺少 pnpm（插件包管理器）精确版本')
  if (new URL(spec.registry_url).protocol !== 'https:') throw new Error('npm（包管理器）来源必须使用 HTTPS（加密网络协议）')
  for (const relative of [spec.entry, spec.modules, spec.cli_entry, spec.schedule, spec.pnpm_entry, ...spec.required_files]) safePath(process.cwd(), relative)
  return { ...spec, version: lock.version, package_manager: lock.package_manager }
}
export function npmArguments(target, spec, npmVersion = '0.0.0') {
  const [major, minor] = npmVersion.trim().split('.').map(Number)
  // npm（包管理器）新版本默认拦截未列入策略的生命周期脚本；按上游声明安装，不另维护依赖版本策略。
  const scripts = major > 11 || major === 11 && minor >= 17 ? ['--dangerously-allow-all-scripts'] : []
  // Exact target releases may be published after an older packument entered npm's cache.
  // Refresh registry metadata while npm still reuses immutable package archives from cache.
  return ['install', '--prefix', target, '--package-lock=false', '--omit=dev', '--no-audit', '--no-fund', '--no-update-notifier', '--prefer-online', '--loglevel=info', '--ignore-scripts=false', ...scripts, '--registry', spec.registry_url]
}
export async function readJson(path) { return JSON.parse((await readFile(path, 'utf8')).replace(/^\uFEFF/u, '')) }
export function isCurrent(metadata, spec) {
  return ['source_kind', 'adapter', 'package_name', 'version', 'bridge_protocol', 'entry', 'modules', 'cli_entry', 'schedule', 'pnpm_entry', 'package_manager', 'required_files'].every(key => JSON.stringify(metadata[key]) === JSON.stringify(spec[key]))
}
export async function checkPlugin(program, spec) {
  const record = await readJson(safePath(program, spec.plugin_record))
  if (record.dsh_version !== spec.version || record.bridge_protocol !== spec.bridge_protocol || record.adapter !== spec.adapter) throw new Error('项目插件尚未适配目标 DSH（代理宿主）版本，请更新程序')
}
function run(args, cwd, env, capture = false) {
  return new Promise((success, failure) => {
    const child = spawn(process.execPath, args, { cwd, env, windowsHide: true, stdio: ['ignore', capture ? 'pipe' : 'inherit', 'pipe'] })
    let output = ''
    let installLog = '' // installLog（有界错误日志）：用于区分来源故障和组件构建错误。
    child.stderr.on('data', chunk => { process.stderr.write(chunk); installLog = (installLog + chunk).slice(-65536) })
    if (capture) child.stdout.on('data', chunk => { output += chunk })
    child.on('error', failure)
    child.on('exit', (code, signal) => code === 0 ? success(output.trim()) : failure(Object.assign(new Error('npm（包管理器）安装失败，请检查网络及日志后重试'), { installLog, cancelled: signal !== null || code === 130 || code === 3221225786 })))
  })
}
export async function install(program, target, npmCli, workspace) {
  const spec = targetSpec(await readJson(join(program, 'config/dsh/runtime.lock.json')))
  await checkPlugin(program, spec)
  await mkdir(target, { recursive: true })
  // incomplete extraction（未完成解压）：不能信任 npm（包管理器）对残缺目录的增量判断。
  // 完整候选由原生端先行复核复用；其余候选重新解压，已核验归档仍从工作区缓存读取。
  process.stdout.write('VOICE_DSH_PHASE 准备候选组件，复用已下载的 npm（包管理器）缓存\n')
  const modules = safePath(target, spec.modules)
  if (await lstat(modules).then(info => info.isSymbolicLink()).catch(error => { if (error.code === 'ENOENT') return false; throw error })) throw new Error('DSH（代理宿主）候选依赖目录不能是链接')
  await rm(modules, { recursive: true, force: true })
  for (const name of ['manifest.json', 'runtime.json', spec.entry]) await rm(safePath(target, name), { force: true })
  await writeFile(join(target, 'package.json'), JSON.stringify({ name: 'voice-terminal-dsh-runtime', private: true, type: 'module', dependencies: { [spec.package_name]: spec.version, pnpm: spec.package_manager.slice('pnpm@'.length) } }, null, 2) + '\n')
  const config = join(workspace, 'data/config/dsh-install.npmrc')
  await mkdir(dirname(config), { recursive: true })
  if (!existsSync(config)) await writeFile(config, '')
  const globalConfig = join(workspace, 'data/config/dsh-install-global.npmrc')
  if (!existsSync(globalConfig)) await writeFile(globalConfig, '')
  const cache = join(workspace, 'cache/npm')
  const temp = join(workspace, 'tmp/dsh-npm')
  await mkdir(temp, { recursive: true })
  const env = { ...process.env, PATH: `${dirname(process.execPath)}${process.platform === 'win32' ? ';' : ':'}${process.env.PATH ?? ''}`, NPM_CONFIG_USERCONFIG: config, NPM_CONFIG_GLOBALCONFIG: globalConfig, NPM_CONFIG_CACHE: cache, NPM_CONFIG_PREFIX: target, TEMP: temp, TMP: temp, TMPDIR: temp }
  delete env.NODE_OPTIONS
  delete env.ELECTRON_RUN_AS_NODE
  const npmVersion = await run([npmCli, '--version'], target, env, true)
  process.stdout.write('VOICE_DSH_PHASE 在线刷新版本元数据并复用已下载的 npm（包管理器）归档缓存\n')
  const registries = registriesForAttempt(registryCandidates(requestedRegistry(spec.registry_url, env, target)), env.VOICE_RESOURCE_ATTEMPT)
  let selectedRegistry = '' // selectedRegistry（实际传输来源）：不覆盖规范来源与候选身份。
  for (const [index, registry] of registries.entries()) {
    process.stdout.write(`VOICE_DSH_PHASE 下载来源：${new URL(registry).host}\n`)
    try {
      await run([npmCli, ...npmArguments(target, { ...spec, registry_url: registry }, npmVersion)], target, registryEnvironment(env, registry))
      selectedRegistry = registry
      break
    } catch (error) {
      if (index === registries.length - 1 || !canRetryRegistry(error)) throw error
      process.stdout.write(`VOICE_DSH_PHASE 来源 ${new URL(registry).host} 失败，切换备用注册表\n`)
      await rm(modules, { recursive: true, force: true })
    }
  }
  process.stdout.write('VOICE_DSH_PHASE 核对 DSH（代理宿主）版本、资源及启动入口\n')
  const pkg = await readJson(safePath(target, `${spec.modules}/${spec.package_name}/package.json`))
  if (pkg.name !== spec.package_name || pkg.version !== spec.version) throw new Error('已下载 DSH（代理宿主）包名或版本不匹配')
  const tool = await readJson(safePath(target, `${spec.modules}/pnpm/package.json`))
  if (tool.name !== 'pnpm' || `pnpm@${tool.version}` !== spec.package_manager) throw new Error('私有 pnpm（插件包管理器）实际版本不匹配')
  for (const path of [spec.cli_entry, spec.schedule, ...spec.required_files]) if (!existsSync(safePath(target, path))) throw new Error(`DSH（代理宿主）必要资源缺失：${path}`)
  const metadata = { schema: 3, kind: 'dsh', ...spec, fields: { source_kind: 'npm（包管理器）安装来源', adapter: '启动适配器', package_name: '官方宿主包名', version: '实际安装版本', entry: '启动包装入口', cli_entry: '上游命令入口', modules: '运行依赖目录', schedule: '调度入口', bridge_protocol: '桥接协议版本', package_manager: '插件包管理工具', pnpm_entry: '工作区插件包管理入口', registry_url: 'npm（包管理器）注册表来源', node_version_range: 'Node.js（运行环境）支持范围', required_files: '必要资源', plugin_record: '插件适配记录', schema: '运行记录格式', kind: '组件类型' } }
  metadata.download_source = selectedRegistry
  metadata.fields.download_source = '本次实际使用的下载注册表，不参与版本兼容判断'
  await writeFile(join(target, 'runtime.json'), JSON.stringify(metadata, null, 2) + '\n')
  // wrapper（启动包装入口）：通过上游 runCli（命令接口）注入工作区工具，避免全局安装。
  await writeFile(safePath(target, spec.entry), `import { readFileSync } from 'node:fs'\nimport { dirname, resolve } from 'node:path'\nimport { fileURLToPath, pathToFileURL } from 'node:url'\nconst root = dirname(fileURLToPath(import.meta.url))\nconst record = JSON.parse(readFileSync(resolve(root, 'runtime.json'), 'utf8'))\nconst { runCli } = await import(pathToFileURL(resolve(root, record.cli_entry)).href)\nawait runCli({ packageManager: { command: process.execPath, args: ['--expose-internals', resolve(root, record.pnpm_entry)] } })\n`)
  const version = await run([safePath(target, spec.entry), '--version'], target, env, true)
  if (version !== spec.version) throw new Error('DSH（代理宿主）实际命令版本与目标不匹配')
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const [action, program, target, npmCli, workspace] = process.argv.slice(2)
    if (action !== 'install') throw new Error('不支持的 DSH（代理宿主）安装操作')
    await install(resolve(program), resolve(target), resolve(npmCli), resolve(workspace))
  } catch (error) { process.stderr.write(`${error.message}\n`); process.exitCode = 1 }
}

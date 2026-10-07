/** Build（构建）：一个安装包，独立宿主组件与官方浏览器闭包。 */
import { copyFile, mkdir, readFile, readdir, rm, writeFile, symlink } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
import { projectRoot } from '../../../scripts/project-root.mjs'
const repo = projectRoot(import.meta.url)
const sourceRoot = resolve(repo, '.local/build/dsh/source')
const outputRoot = resolve(process.argv[2] ?? resolve(repo, '.local/build/dsh/bundles/voice-terminal'))
const runtimeBundles = resolve(repo, '.local/build/dsh/bundles')
const rel = relative(runtimeBundles, outputRoot)
if (!rel || rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
  throw new Error(`构建产物必须位于项目 bundles（功能包）目录：${outputRoot}`)
}
const ts = await import(pathToFileURL(resolve(sourceRoot, 'node_modules/typescript/lib/typescript.js')).href)
const manifest = JSON.parse(await readFile(resolve(here, 'package.json'), 'utf8'))

function run(args) {
  const result = spawnSync(process.execPath, args, { cwd: repo, stdio: 'inherit', windowsHide: true })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`插件构建检查失败：${args.join(' ')}`)
}

// 所有调用入口（包括 setup）先进行类型检查，避免仅转译掩盖错误。
for (const config of ['tsconfig.host.json', 'tsconfig.client.json']) {
  run([resolve(sourceRoot, 'node_modules/typescript/bin/tsc'), '-p', resolve(here, config)])
}
run([resolve(here, 'check.mjs')])

// 仅清理已验证的项目产物 lib（编译目录）；不触及宿主或用户存储。
await rm(resolve(outputRoot, 'lib'), { recursive: true, force: true })
async function compile(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const source = resolve(directory, entry.name)
    if (entry.isDirectory()) { if (entry.name !== 'client') await compile(source); continue }
    if (!entry.name.endsWith('.ts') || entry.name.endsWith('.d.ts')) continue
    const sourceName = relative(resolve(here, 'src'), source)
    const target = resolve(outputRoot, 'lib', sourceName.replace(/\.ts$/u, '.js'))
    const result = ts.transpileModule(await readFile(source, 'utf8'), {
      fileName: source, reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, sourceMap: true },
    })
    const errors = (result.diagnostics ?? []).filter(item => item.category === ts.DiagnosticCategory.Error)
    if (errors.length) throw new Error(errors.map(item => ts.flattenDiagnosticMessageText(item.messageText, '\n')).join('\n'))
    await mkdir(dirname(target), { recursive: true })
    await writeFile(target, result.outputText, 'utf8')
    await writeFile(`${target}.map`, result.sourceMapText ?? '{}', 'utf8')
  }
}
await compile(resolve(here, 'src'))
const prompt = `${await readFile(resolve(repo, 'config/shadow_agent/instructions.md'), 'utf8')}\n\n${await readFile(resolve(repo, 'config/dsh/shadow-agent.rules.md'), 'utf8')}`
await writeFile(resolve(outputRoot, 'lib/shared/generated-prompt.js'), `export const GENERATED_SHADOW_PROMPT = ${JSON.stringify(prompt)}\n`)
await writeFile(resolve(outputRoot, 'lib/shared/version.js'), `export const BUNDLE_VERSION = ${JSON.stringify(manifest.version)}\n`)
const metadata = await readFile(resolve(here, 'preset/shadow-agent/preset.yml'), 'utf8')
const yamlField = field => {
  const value = metadata.match(new RegExp(`^${field}:\\s*(.+?)\\s*$`, 'mu'))?.[1]
  if (!value) throw new Error(`预设缺少 ${field}（元数据字段）`)
  return JSON.stringify(value)
}
const composition = (await readFile(resolve(here, 'preset/shadow-agent/agent.cordis.yml'), 'utf8')).trimEnd()
for (const id of ['tool-pwsh', 'tool-fs', 'tool-jobs', 'tool-skill', 'compaction-basic', 'tool-subagent', 'tool-web', 'workflow-ptc']) {
  if (!composition.includes(`id: ${id}`)) throw new Error(`代理预设缺少原有能力：${id}`)
}
if (/tools\.restrict|exact safe tool surface/u.test(composition)) throw new Error('影子代理预设不得缩减默认工具可见性')
const presetPatch = `- insert:\n    - id: preset-shadow-agent\n      name: '@deepseek-ai/dsh-agent-preset'\n      config:\n        id: shadow-agent\n        name: ${yamlField('name')}\n        description: ${yamlField('description')}\n        order: 5\n        plugins:\n${composition.split(/\r?\n/u).map(line => `          ${line}`).join('\n')}\n`
await writeFile(resolve(outputRoot, 'cordis.patch.yml'), `${(await readFile(resolve(here, 'host.patch.yml'), 'utf8')).trimEnd()}\n${presetPatch}`)
for (const file of ['package.json', 'README.md', 'components.json']) await copyFile(resolve(here, file), resolve(outputRoot, file))
run([resolve(repo, 'scripts/build-dsh-client.mjs'), here, outputRoot, sourceRoot])
// package exports（包导出）和组件声明均必须指向真实发布文件。
for (const entry of [...Object.values(manifest.exports), ...JSON.parse(await readFile(resolve(here, 'components.json'), 'utf8')).components.map(component => component.runtime_entry)]) {
  if (!existsSync(resolve(outputRoot, entry))) throw new Error(`运行入口缺失：${entry}`)
}

// 隔离测试用依赖链接：正式安装由官方包管理器解析 peerDependencies（同宿主依赖）。
if (process.argv.includes('--test')) {
  const require = createRequire(resolve(sourceRoot, 'python/sdk-runtime/package.json'))
  const paths = ts.readConfigFile(resolve(sourceRoot, 'tsconfig.base.json'), ts.sys.readFile).config.compilerOptions.paths
  for (const name of Object.keys({ ...manifest.dependencies, ...manifest.peerDependencies })) {
    const alias = paths[name]?.[0]
    const packageRoot = alias ? dirname(alias.endsWith('/src') ? alias : dirname(alias)) : undefined
    const resolver = packageRoot ? createRequire(resolve(sourceRoot, packageRoot, 'package.json')) : require
    const entry = resolver.resolve(`${name}/package.json`)
    const target = resolve(outputRoot, 'node_modules', name)
    if (!existsSync(target)) {
      await mkdir(dirname(target), { recursive: true })
      await symlink(dirname(entry), target, process.platform === 'win32' ? 'junction' : 'dir')
    }
  }
}
console.log(`DSH 统一功能包已构建：${outputRoot}`)

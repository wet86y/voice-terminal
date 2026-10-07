/** Component checks（组件规范检查）：禁止循环跨域依赖及未声明的外部依赖。 */
import { readFile, readdir, access } from 'node:fs/promises'
import { dirname, resolve, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
const here = dirname(fileURLToPath(import.meta.url))
const manifest = JSON.parse(await readFile(resolve(here, 'package.json'), 'utf8'))
const declaration = JSON.parse(await readFile(resolve(here, 'components.json'), 'utf8'))
const components = new Map(declaration.components.map(item => [item.id, item]))
if (declaration.schema_version !== 1 || components.size !== declaration.components.length) throw new Error('组件声明版本错误或标识重复')
const visited = new Set()
const visiting = new Set()
function visit(id) {
  if (visiting.has(id)) throw new Error(`组件依赖成环：${id}`)
  if (visited.has(id)) return
  const component = components.get(id)
  if (!component) throw new Error(`依赖未声明的组件：${id}`)
  visiting.add(id)
  for (const dependency of component.depends_on) visit(dependency)
  visiting.delete(id)
  visited.add(id)
}
for (const component of components.values()) {
  visit(component.id)
  await access(resolve(here, component.entry))
  if (!component.description || !component.runtime_entry) throw new Error(`组件缺少职责或运行入口：${component.id}`)
}
const declared = { ...manifest.dependencies, ...manifest.peerDependencies, ...manifest.devDependencies }
async function inspect(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name)
    if (entry.isDirectory()) { await inspect(path); continue }
    if (!/\.tsx?$/u.test(entry.name)) continue
    const source = await readFile(path, 'utf8')
    if (/:\s*any\b|as\s+any\b|as\s+unknown\s+as\b/u.test(source)) throw new Error(`禁止绕过公开类型契约：${path}`)
    const owner = relative(resolve(here, 'src'), path).replaceAll('\\', '/').split('/')[0]
    for (const match of source.matchAll(/(?:from\s*|import\s*\()['"]([^'"]+)['"]/gu)) {
      const specifier = match[1]
      if (specifier.startsWith('.')) {
        const target = relative(resolve(here, 'src'), resolve(dirname(path), specifier)).replaceAll('\\', '/').split('/')[0]
        if (target !== owner && !components.get(owner)?.depends_on.includes(target)) throw new Error(`未声明组件依赖：${owner} → ${target}（${path}）`)
      } else if (!specifier.startsWith('node:')) {
        const pkg = specifier.startsWith('@') ? specifier.split('/').slice(0, 2).join('/') : specifier.split('/')[0]
        if (!declared[pkg]) throw new Error(`未声明外部依赖：${pkg}（${path}）`)
      }
    }
    if (/VOICE_DSH_SCHEDULE_MODULE|useSyncExternalStore/u.test(source)) throw new Error(`内部路径或手动可观测订阅不符合官方规范：${path}`)
  }
}
await inspect(resolve(here, 'src'))
for (const [pkg, version] of Object.entries(manifest.peerDependencies)) {
  if (manifest.devDependencies[pkg] !== version) throw new Error(`同宿主依赖必须匹配开发版本：${pkg}`)
}
for (const pkg of manifest.dsh.client.inject) {
  if (!manifest.devDependencies[pkg]) throw new Error(`浏览器注入依赖未声明开发版本：${pkg}`)
}
console.log('组件职责、跨组件依赖和同宿主版本检查通过。')

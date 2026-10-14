/** Typecheck（类型检查）：复用已锁定 DSH 的校验库及编译器，不安装第二份依赖。 */
import { access, lstat, mkdir, symlink, unlink } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
const here = dirname(fileURLToPath(import.meta.url))
const source = resolve(here, '../../../.local/build/dsh/source')
const target = resolve(here, 'node_modules/zod')
try { await access(resolve(target, 'package.json')) } catch {
  const require = createRequire(resolve(source, 'packages/session/session-projection/package.json'))
  const dependency = dirname(require.resolve('zod/package.json'))
  const stale = await lstat(target).catch(() => undefined)
  if (stale) {
    if (!stale.isSymbolicLink()) throw new Error('校验库路径存在但不可读，保留现有目录')
    await unlink(target)
  }
  await mkdir(dirname(target), { recursive: true })
  await symlink(dependency, target, 'junction')
}
for (const file of ['tsconfig.host.json', 'tsconfig.client.json']) {
  const result = spawnSync(process.execPath, [resolve(source, 'node_modules/typescript/bin/tsc'), '-p', resolve(here, file)], { stdio: 'inherit', windowsHide: true })
  if (result.error) throw result.error
  if (result.status !== 0) process.exit(result.status ?? 1)
}

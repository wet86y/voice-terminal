// Manifest check（发行清单核对）：校验唯一程序和生产组件，退出旧构建状态副本。
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'
import { REPO_ROOT } from './lib/dsh-paths.mjs'
const root = join(REPO_ROOT, '.local')
const result = spawnSync(join(root, 'runtimes/python/python.exe'), ['-B', '-X', 'utf8', join(root, 'app/scripts/backend-entry.py'), 'voice_terminal.distribution', 'verify-install', '--root', root], { stdio: 'inherit', windowsHide: true })
process.exit(result.status ?? 1)

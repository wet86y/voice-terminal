// Project root（工程根目录）：按源码标识定位，不依赖调用目录或父目录层数。
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export function projectRoot(moduleUrl) {
  let current = dirname(fileURLToPath(moduleUrl))
  while (true) {
    if (existsSync(join(current, 'pyproject.toml')) && existsSync(join(current, 'voice_terminal'))) return current
    const parent = dirname(current)
    if (parent === current) throw new Error('无法定位语音终端工程根目录')
    current = parent
  }
}

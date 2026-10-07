// Junction（目录联接）：搬迁后只修复受控构建缓存内部链接，不复制依赖。
import fs from 'node:fs'
import path from 'node:path'
const [oldRoot, newRoot] = process.argv.slice(2).map(p => path.resolve(p))
const pending = [newRoot]
let count = 0
while (pending.length) {
  for (const entry of fs.readdirSync(pending.pop(), { withFileTypes: true })) {
    const file = path.join(entry.parentPath ?? entry.path, entry.name)
    if (entry.isSymbolicLink()) {
      const target = fs.readlinkSync(file).replace(/^\\\\\?\\/, '')
      const relative = path.relative(oldRoot, path.resolve(target))
      if (!relative.startsWith('..') && !path.isAbsolute(relative)) {
        fs.unlinkSync(file)
        fs.symlinkSync(path.join(newRoot, relative), file, 'junction')
        count++
      }
    } else if (entry.isDirectory() && entry.name !== '.git') pending.push(file)
  }
}
console.log(`构建缓存内部联接已修复：${count}`)

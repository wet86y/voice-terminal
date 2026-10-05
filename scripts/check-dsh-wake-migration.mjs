import { readFileSync } from 'node:fs'
const [legacyPath] = process.argv.slice(2) // legacyPath（旧语音链路任务路径），官方会话任务不属于迁移来源。

function read(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')) }
  catch (error) {
    if (error?.code === 'ENOENT') return undefined
    throw error
  }
}

const legacy = read(legacyPath)
const legacyRows = legacy?.tasks ?? {}
if (typeof legacyRows !== 'object' || Array.isArray(legacyRows)) throw new Error('旧 Voice 任务目录无效。')
for (const [id, row] of Object.entries(legacyRows)) {
  const due = Number(row?.scheduled_at)
  if (!Number.isFinite(due) || !String(row?.text ?? '').trim()) {
    throw new Error(`旧 Voice 任务 ${id} 无法迁移。`)
  }
  const seconds = Number(row?.repeat_seconds)
  if (seconds > 0 && (!Number.isSafeInteger(seconds) || seconds < 60)) {
    throw new Error(`旧 Voice 任务 ${id} 的重复间隔不兼容。`)
  }
}
process.stdout.write(`可迁移旧 Voice（语音链路）任务：${Object.keys(legacyRows).length}；官方 DSH 会话任务保持独立。\n`)

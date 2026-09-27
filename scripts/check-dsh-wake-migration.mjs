import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const [scheduleModulePath, nativePath, legacyPath] = process.argv.slice(2)
const { decodeScheduleRecord } = await import(pathToFileURL(scheduleModulePath).href)

function read(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')) }
  catch (error) {
    if (error?.code === 'ENOENT') return undefined
    throw error
  }
}

const native = read(nativePath)
const nativeRows = native?.tables?.tasks ?? {}
if (typeof nativeRows !== 'object' || Array.isArray(nativeRows)) throw new Error('DSH 原生任务目录无效。')
for (const [id, row] of Object.entries(nativeRows)) {
  try { decodeScheduleRecord(row.record) }
  catch (error) { throw new Error(`原生任务 ${id} 无法迁移：${error}`) }
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
process.stdout.write(`可迁移任务：DSH ${Object.keys(nativeRows).length}，Voice ${Object.keys(legacyRows).length}\n`)

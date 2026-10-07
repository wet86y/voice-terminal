/**
 * Remove the stored Sessions that crash the DSH conversation pane.
 *
 * A session is poisoned when any `turn/end` row carries `reason: null` or no
 * `reason` at all: the web client reads `turn/end.data.reason.kind` on its normal
 * render path, and one bad row blanks the 对话 pane for every session. Sessions
 * are discovered by scanning the transcripts, not from a fixed list.
 *
 * 清理前停机并验证唯一用户数据备份，不生成按时间累积的诊断备份。
 *
 * Usage:
 *   node scripts/dsh-diagnose/purge-poisoned-sessions.mjs          # dry run
 *   node scripts/dsh-diagnose/purge-poisoned-sessions.mjs apply    # delete
 */
import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { rm } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import {
  PROJ_CACHE_DIR, REPO_ROOT, SESSION_MAPPINGS, SESSIONS_ROOT, WORKSPACE_STORE,
  readSessionRecords, sessionFiles, sessionIdOf,
} from './lib/dsh-paths.mjs'

const apply = process.argv[2] === 'apply'
if (!apply) console.log('dry run — re-run with "apply" to delete\n')

// ---- 1. discover poisoned sessions ----
const poisoned = new Map()
for (const path of sessionFiles()) {
  let records
  try {
    records = await readSessionRecords(path)
  } catch {
    continue
  }
  const rows = records.filter(record => record.type === 'turn/end'
    && (record.data?.reason === null || record.data?.reason === undefined))
  if (rows.length === 0) continue
  const owner = sessionIdOf(path)
  poisoned.set(owner, { transcript: path, sessionDir: dirname(path), rows: rows.length })
}

if (poisoned.size === 0) {
  console.log('no poisoned sessions found — nothing to do')
  process.exit(0)
}
console.log(`poisoned sessions: ${poisoned.size}`)
for (const [id, info] of poisoned) console.log(`  ${id}  (${info.rows} poisoned rows)`)

// ---- 2. back up ----
const backupDir = join(REPO_ROOT, '.local/backups/user-data/dsh') // backupDir（唯一滚动持久数据备份内的代理状态）。
if (apply) {
  const saved = spawnSync(join(REPO_ROOT, '.local/runtimes/python/python.exe'), ['-B', '-X', 'utf8', join(REPO_ROOT, 'scripts/local-admin.py'), 'backup'], { stdio: 'inherit' })
  if (saved.status !== 0) throw new Error('请先正常退出业务；唯一数据备份未成功，取消清理')
}
console.log(`\nbackup -> ${backupDir}`)

// ---- 3. delete transcripts and projection-cache entries ----
let removedSessions = 0
let removedCache = 0
for (const [id, info] of poisoned) {
  if (existsSync(info.sessionDir)) {
    if (apply) await rm(info.sessionDir, { recursive: true, force: true })
    removedSessions += 1
  }
  const cache = join(PROJ_CACHE_DIR, `${id}.json`)
  if (existsSync(cache)) {
    if (apply) await rm(cache, { force: true })
    removedCache += 1
  }
}
console.log(`sessions removed: ${removedSessions}, projection-cache entries removed: ${removedCache}`)

// ---- 4. drop workspace membership ----
const ids = [...poisoned.keys()]
const workspace = JSON.parse(readFileSync(WORKSPACE_STORE, 'utf8').replace(/^\uFEFF/, ''))
let droppedMembership = 0
let droppedArchived = 0
for (const row of Object.values(workspace.tables?.workspaces ?? {})) {
  if (!Array.isArray(row?.sessionIds)) continue
  const before = row.sessionIds.length
  row.sessionIds = row.sessionIds.filter(id => !ids.includes(id))
  droppedMembership += before - row.sessionIds.length
}
if (Array.isArray(workspace.global?.archivedSessionIds)) {
  const before = workspace.global.archivedSessionIds.length
  workspace.global.archivedSessionIds = workspace.global.archivedSessionIds.filter(id => !ids.includes(id))
  droppedArchived = before - workspace.global.archivedSessionIds.length
}
if (apply) writeFileSync(WORKSPACE_STORE, `${JSON.stringify(workspace, null, 2)}\n`, 'utf8')
console.log(`workspace.json: dropped ${droppedMembership} membership ids, ${droppedArchived} archived ids`)

// ---- 5. retire the Voice-side bindings ----
if (existsSync(SESSION_MAPPINGS)) {
  const mappings = JSON.parse(readFileSync(SESSION_MAPPINGS, 'utf8').replace(/^\uFEFF/, ''))
  let retired = 0
  for (const row of Object.values(mappings.sessions ?? {})) {
    if (!ids.includes(row?.dsh_session_id)) continue
    row.retired_at = Date.now() / 1000
    row.last_activation_id = null
    retired += 1
  }
  if (apply) writeFileSync(SESSION_MAPPINGS, `${JSON.stringify(mappings, null, 2)}\n`, 'utf8')
  console.log(`session-mappings.json: marked ${retired} voice mappings retired`)
}

if (apply) {
  console.log('\ndone. Restart the Voice Terminal stack so the DSH host drops them from memory:')
  console.log('  scripts\\start.ps1')
} else {
  console.log('\ndry run only — nothing was written')
}

/**
 * Remove the stored Sessions that crash the DSH conversation pane.
 *
 * A session is poisoned when any `turn/end` row carries `reason: null` or no
 * `reason` at all: the web client reads `turn/end.data.reason.kind` on its normal
 * render path, and one bad row blanks the 对话 pane for every session. Sessions
 * are discovered by scanning the transcripts, not from a fixed list.
 *
 * Everything removed is copied to `logs/incident-<stamp>/` first (transcripts,
 * projection-cache entries, and snapshots of both JSON stores), so the operation
 * is reversible. Other sessions, unrelated cache entries, and settings are left
 * alone.
 *
 * Usage:
 *   node scripts/dsh-diagnose/purge-poisoned-sessions.mjs          # dry run
 *   node scripts/dsh-diagnose/purge-poisoned-sessions.mjs apply    # delete
 */
import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { cp, mkdir, rm } from 'node:fs/promises'
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
const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
const backupDir = join(REPO_ROOT, 'logs', `incident-${stamp}`)
if (apply) {
  await mkdir(join(backupDir, 'sessions'), { recursive: true })
  await mkdir(join(backupDir, 'projcache'), { recursive: true })
  await cp(WORKSPACE_STORE, join(backupDir, 'workspace.json'))
  if (existsSync(SESSION_MAPPINGS)) await cp(SESSION_MAPPINGS, join(backupDir, 'session-mappings.json'))
}
console.log(`\nbackup -> ${backupDir}`)

// ---- 3. delete transcripts and projection-cache entries ----
let removedSessions = 0
let removedCache = 0
for (const [id, info] of poisoned) {
  if (existsSync(info.sessionDir)) {
    if (apply) await cp(info.sessionDir, join(backupDir, 'sessions', id), { recursive: true })
    if (apply) await rm(info.sessionDir, { recursive: true, force: true })
    removedSessions += 1
  }
  const cache = join(PROJ_CACHE_DIR, `${id}.json`)
  if (existsSync(cache)) {
    if (apply) await cp(cache, join(backupDir, 'projcache', `${id}.json`))
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

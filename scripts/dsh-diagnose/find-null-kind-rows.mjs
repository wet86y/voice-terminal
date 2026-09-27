/**
 * List every stored `turn/end` whose `reason` is `null` or missing.
 *
 * The DSH web client reads `turn/end.data.reason.kind` on its normal render path
 * (see `docs/dsh-plugin-audit-20260927.md`). One such row is enough to make the
 * conversation pane blank for every session, so this is the first check to run
 * when the 对话 pane is empty.
 *
 * Usage: node scripts/dsh-diagnose/find-null-kind-rows.mjs
 */
import { sessionFiles, sessionIdOf, readSessionRecords } from './lib/dsh-paths.mjs'

let total = 0
const poisoned = []

for (const path of sessionFiles()) {
  let records
  try {
    records = await readSessionRecords(path)
  } catch (error) {
    console.log(`UNREADABLE ${sessionIdOf(path)}: ${error.message}`)
    continue
  }
  for (const record of records) {
    if (record.type !== 'turn/end') continue
    total += 1
    const reason = record.data?.reason
    if (reason === undefined || reason === null) {
      poisoned.push({
        session: sessionIdOf(path),
        format: path.match(/\.v(\d+)\./)?.[1] ?? '?',
        seq: record.seq,
        reason: reason === undefined ? 'MISSING' : 'NULL',
      })
    }
  }
}

console.log(`scanned turn/end rows: ${total}; null/missing: ${poisoned.length}`)
for (const row of poisoned) console.log(`  v${row.format} ${row.session} seq=${row.seq} reason=${row.reason}`)

const bySession = new Map()
for (const row of poisoned) bySession.set(row.session, (bySession.get(row.session) ?? 0) + 1)
if (bySession.size > 0) {
  console.log('\naffected sessions (each one blanks the whole conversation pane):')
  for (const [session, count] of bySession) console.log(`  ${session}  (${count} poisoned rows)`)
  console.log('\nclean them up with: node scripts/dsh-diagnose/purge-poisoned-sessions.mjs')
}
process.exit(poisoned.length === 0 ? 0 : 1)

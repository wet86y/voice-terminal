/**
 * Inspect one stored session transcript.
 *
 * Decodes every Zstandard frame through the pinned DSH decoder and prints the
 * event sequence, the surface metadata the conversation page filters on, and a
 * per-turn summary of `turn/end` reasons — enough to tell a genuinely broken log
 * from a rendering problem.
 *
 * Usage: node scripts/dsh-diagnose/inspect-session.mjs <session.v4.jsonl.zstd | session directory>
 */
import { existsSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { readSessionRecords } from './lib/dsh-paths.mjs'

const target = process.argv[2]
if (!target) {
  console.error('usage: node scripts/dsh-diagnose/inspect-session.mjs <transcript | session directory>')
  process.exit(2)
}

const path = existsSync(join(target, 'session.v4.jsonl.zstd'))
  ? join(target, 'session.v4.jsonl.zstd')
  : target
if (!existsSync(path)) {
  console.error(`not found: ${path}`)
  process.exit(2)
}

const records = await readSessionRecords(path)
const header = records[0] ?? {}
console.log(`file      : ${path} (${statSync(path).size} bytes)`)
console.log(`header    : id=${header.id ?? '?'} version=${header.version ?? '?'} preset=${header.agentPreset ?? '-'} cwd=${header.cwd ?? '-'}`)
console.log(`records   : ${records.length}`)

const eventRows = records.filter(record => record.type !== 'session')
console.log(`events    : ${eventRows.length}`)
if (eventRows.length < records.length - 1) {
  console.log('note      : the header frame is the only frame — this log carries no events')
}

console.log('\nseq | type                  | surfaceOp | sourceEventSeqs')
for (const record of eventRows) {
  const surfaceOp = record.surfaceOp === undefined ? '-' : JSON.stringify(record.surfaceOp)
  const sources = record.sourceEventSeqs === undefined ? '-' : JSON.stringify(record.sourceEventSeqs)
  console.log(`${String(record.seq ?? '?').padStart(3)} | ${String(record.type).padEnd(21)} | ${String(surfaceOp).padEnd(9)} | ${sources}`)
}

const MESSAGE_TYPES = new Set(['user/message', 'assistant/message'])
const pageRows = eventRows.filter(record => MESSAGE_TYPES.has(record.type) && record.surfaceOp === 'append')
console.log(`\nconversation-page rows (user/assistant, surfaceOp=append): ${pageRows.length}`)
for (const record of pageRows) console.log(`  seq ${record.seq} ${record.type}`)

console.log('\nturn endings:')
let turn = 0
let toolCalls = 0
for (const record of eventRows) {
  if (record.type === 'turn/start') { turn = record.data?.turn ?? turn; toolCalls = 0; continue }
  if (record.type === 'tool/call') { toolCalls += 1; continue }
  if (record.type !== 'turn/end') continue
  const reason = record.data?.reason
  const verdict = reason === null || reason === undefined
    ? 'NULL — crashes the conversation pane'
    : JSON.stringify(reason)
  console.log(`  turn ${record.data?.turn ?? turn} seq=${record.seq} toolCalls=${toolCalls} reason=${verdict}`)
}

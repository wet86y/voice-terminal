import { randomUUID } from 'node:crypto'
import { readFileSync, mkdirSync, writeFileSync, renameSync, unlinkSync } from 'node:fs'
import { dirname } from 'node:path'
import {
  ScheduleId, createAfterScheduleRecord, createAtScheduleRecord, createEveryScheduleRecord,
  createDailyScheduleRecord, createWeeklyScheduleRecord, createCronScheduleRecord,
  decodeScheduleRecord, isRecurringScheduleRecord, resolveRecurringOccurrence,
} from '@deepseek-ai/dsh-schedule'
import type { ScheduleRecord, AtInput, DailyInput, WeeklyInput, CronInput } from '@deepseek-ai/dsh-schedule'
import { voiceApi } from './voice-api.js'
import { record as jsonRecord } from '../shared/json-record.js'
export { voiceApi } from './voice-api.js'

/** TaskInput（任务输入）：工具或 HTTP（网络接口）数据，规则由官方调度构造器验证。 */
export type TaskInput = Record<string, unknown>
const STORE_PATH = process.env.VOICE_DSH_TASKS_PATH ?? ''
const LEGACY_PATH = process.env.VOICE_DSH_LEGACY_WAKE_PATH ?? ''
const RETRY_MS = 30_000
function taskTarget(value: unknown): Row['target'] {
  if (value === 'voice_llm' || value === 'shadow_agent' || value === 'both') return value
  throw new Error('target（任务目标）无效。')
}

type Row = {
  /** creation_source（创建来源）：历史任务未提供时保持 unknown（未知）。 */
  creation_source?: 'manual' | 'agent' | 'unknown';
  task_id: string; target: 'voice_llm' | 'shadow_agent' | 'both'; text: string; reference: string;
  record: ScheduleRecord; status: 'pending' | 'archived' | 'cancelled';
  created_at: string; updated_at: string; last_triggered_at: string | null;
  occurrence: number; retry_at?: number; history: Record<string, unknown>[];
}

function readJson(path: string): Record<string, unknown> | undefined {
  if (!path) return undefined
  try {
    const value: unknown = JSON.parse(readFileSync(path, 'utf8'))
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('任务存储必须为 JSON（结构化数据）对象。')
    return jsonRecord(value)
  }
  catch (error) {
    if (jsonRecord(error).code === 'ENOENT') return undefined
    throw error
  }
}

/** Stored row decoder（存储行解码）：保留附加历史字段，公开调度器验证规则。 */
function decodeRow(value: unknown): Row {
  const raw = jsonRecord(value)
  if (typeof raw.task_id !== 'string' || typeof raw.text !== 'string' || typeof raw.reference !== 'string'
      || typeof raw.created_at !== 'string' || typeof raw.updated_at !== 'string'
      || (raw.last_triggered_at !== null && typeof raw.last_triggered_at !== 'string')
      || typeof raw.occurrence !== 'number' || !Number.isSafeInteger(raw.occurrence) || raw.occurrence < 1
      || !Array.isArray(raw.history)
      || (raw.status !== 'pending' && raw.status !== 'archived' && raw.status !== 'cancelled')) {
    throw new Error('DSH 任务存储行格式无效。')
  }
  return { ...raw, task_id: raw.task_id, text: raw.text, reference: raw.reference,
    target: taskTarget(raw.target), created_at: raw.created_at, updated_at: raw.updated_at,
    last_triggered_at: raw.last_triggered_at, occurrence: raw.occurrence, history: raw.history.map(jsonRecord),
    status: raw.status, record: decodeScheduleRecord(raw.record),
    ...(raw.creation_source === 'manual' || raw.creation_source === 'agent' || raw.creation_source === 'unknown'
      ? { creation_source: raw.creation_source } : {}),
    ...(typeof raw.retry_at === 'number' ? { retry_at: raw.retry_at } : {}),
  }
}

function duration(value: string): number {
  const match = /^\+?(\d+)\s*([smhd])$/iu.exec(value.trim())
  if (!match) throw new Error('repeat/when（重复间隔／时间）应为 30m、2h 等时长。')
  const seconds = Number(match[1]) * ({ s: 1, m: 60, h: 3600, d: 86400 } as Record<string, number>)[match[2]!.toLowerCase()]!
  if (!Number.isSafeInteger(seconds) || seconds < 1) throw new Error('时长无效。')
  return seconds
}

function recordFor(id: string, title: string, text: string, args: TaskInput, now: number): ScheduleRecord {
  const selectors = ['after_seconds', 'at', 'every_seconds', 'daily', 'weekly', 'cron']
    .filter(key => args[key] !== undefined && args[key] !== null)
  if (selectors.length !== 1) throw new Error('必须且只能提供一个定时规则。')
  switch (selectors[0]) {
    case 'after_seconds': return createAfterScheduleRecord(ScheduleId(id), text, Number(args.after_seconds), now, title)
    case 'at': return createAtScheduleRecord(ScheduleId(id), text, args.at as AtInput, now, title)
    case 'every_seconds': return createEveryScheduleRecord(ScheduleId(id), text, Number(args.every_seconds), now, title)
    case 'daily': return createDailyScheduleRecord(ScheduleId(id), text, args.daily as DailyInput, now, title)
    case 'weekly': return createWeeklyScheduleRecord(ScheduleId(id), text, args.weekly as WeeklyInput, now, title)
    default: return createCronScheduleRecord(ScheduleId(id), text, args.cron as CronInput, now, title)
  }
}

function wakeRule(when: string, repeat: string | undefined): TaskInput {
  const now = Date.now()
  let due: number
  if (/^\+?\d+\s*[smhd]$/iu.test(when.trim())) due = now + duration(when) * 1000
  else due = Date.parse(when)
  if (!Number.isFinite(due) || due <= now) throw new Error('when（触发时间）必须晚于当前时间。')
  if (repeat) {
    const seconds = duration(repeat)
    if (seconds < 60) throw new Error('repeat（重复间隔）至少为 60 秒。')
    return { every_seconds: seconds, initial_at: new Date(due).toISOString() }
  }
  return { at: new Date(due).toISOString() }
}

export class TaskService {
  private rows = new Map<string, Row>()
  private deletedIds = new Set<string>() // deletedIds（已删除任务标识）：防止旧存储再次导入。
  private timer: ReturnType<typeof setTimeout> | undefined
  private chain: Promise<unknown> = Promise.resolve()
  private closed = false
  private readonly deliveryController = new AbortController()

  constructor() {
    if (!STORE_PATH) throw new Error('DSH 任务存储路径缺失。')
    const stored = readJson(STORE_PATH)
    if (stored !== undefined) {
      if (stored.schema_version !== 1 || !Array.isArray(stored.tasks)) throw new Error('DSH 任务存储文件格式无效。')
      if (stored.deleted_task_ids !== undefined && (!Array.isArray(stored.deleted_task_ids) || stored.deleted_task_ids.some((id: unknown) => typeof id !== 'string'))) throw new Error('已删除任务记录格式无效。')
      this.deletedIds = new Set(stored.deleted_task_ids as string[] | undefined)
      for (const value of stored.tasks) {
        const row = decodeRow(value)
        if (this.deletedIds.has(row.task_id)) continue
        this.rows.set(row.task_id, row)
      }
    }
    const migrated = this.importLegacy()
    if (migrated) this.save()
    this.arm()
  }

  private importLegacy(): boolean {
    let changed = false
    // Native schedule（官方会话任务）由官方存储和计时器负责，不能复制成语音链路任务。
    // 已有项目 store（存储）的任务及历史已在 constructor（构造函数）中加载并原样保留。
    const legacy = readJson(LEGACY_PATH)
    const oldRows = legacy?.tasks
    if (oldRows !== undefined && (typeof oldRows !== 'object' || Array.isArray(oldRows))) throw new Error('旧 Voice 任务文件格式无效。')
    for (const [id, value] of Object.entries(jsonRecord(oldRows))) {
      if (this.rows.has(id) || this.deletedIds.has(id)) continue
      const task = jsonRecord(value)
      const text = String(task.text ?? '')
      const due = Number(task.scheduled_at)
      if (!Number.isFinite(due) || !String(task.text ?? '').trim()) throw new Error(`旧任务 ${id} 无法转换。`)
      const seconds = Number(task.repeat_seconds)
      const raw = seconds > 0
        ? { id, kind: 'every', title: text.slice(0, 120), prompt: text, everySeconds: seconds, scheduledAt: new Date(due * 1000).toISOString() }
        : { id, kind: 'at', title: text.slice(0, 120), prompt: text, scheduledAt: new Date(due * 1000).toISOString() }
      const record = decodeScheduleRecord(raw)
      this.rows.set(id, {
        task_id: id, target: taskTarget(task.target ?? 'shadow_agent'), text,
        reference: String(task.reference ?? ''), record,
        status: task.status === 'pending' ? 'pending' : task.status === 'cancelled' ? 'cancelled' : 'archived',
        created_at: new Date(Number(task.created_at ?? due) * 1000).toISOString(),
        updated_at: new Date(Number(task.updated_at ?? due) * 1000).toISOString(),
        last_triggered_at: task.last_triggered_at ? new Date(Number(task.last_triggered_at) * 1000).toISOString() : null,
        occurrence: Number(task.occurrence ?? 1), history: [],
      })
      changed = true
    }
    return changed
  }

  private save(): void {
    mkdirSync(dirname(STORE_PATH), { recursive: true })
    const temporary = `${STORE_PATH}.${randomUUID()}.tmp`
    try {
      writeFileSync(temporary, JSON.stringify({ schema_version: 1, tasks: [...this.rows.values()], deleted_task_ids: [...this.deletedIds] }, null, 2), 'utf8')
      renameSync(temporary, STORE_PATH)
    } catch (error) {
      try { unlinkSync(temporary) } catch { /* temporary（临时存储）可能尚未创建。 */ }
      throw error
    }
  }

  private serialized<T>(work: () => Promise<T> | T): Promise<T> {
    const next = this.chain.then(work, work)
    this.chain = next.catch(() => undefined)
    return next
  }

  view(row: Row) {
    const record = row.record
    return {
      id: row.task_id, task_id: row.task_id, title: record.title, target: row.target,
      when: record.scheduledAt, repeat: record.kind === 'every' ? `${record.everySeconds}s` : null,
      rule_kind: record.kind, rule: record,
      text: row.text, status: row.status, created_at: row.created_at,
      updated_at: row.updated_at, last_triggered_at: row.last_triggered_at,
      occurrence: row.occurrence, has_reference: !!row.reference,
      creation_source: row.creation_source ?? 'unknown',
      delivery_history: row.history,
    }
  }

  list() { return [...this.rows.values()].map(row => this.view(row)) }
  reference(id: string) {
    const row = this.rows.get(id)
    if (!row) throw new Error('任务不存在。')
    return { task_id: id, reference: row.reference }
  }

  create(args: TaskInput, reference = '') {
    return this.serialized(() => {
      const id = `schedule-${randomUUID()}`
      const now = Date.now()
      const text = String(args.text ?? args.prompt ?? '').trim()
      const title = String(args.title ?? text.slice(0, 120)).trim()
      const target = taskTarget(args.target ?? 'shadow_agent')
      if (!['voice_llm', 'shadow_agent', 'both'].includes(target)) throw new Error('target（任务目标）无效。')
      if (!text || !title || reference.length > 120_000) throw new Error('任务说明或参考上下文无效。')
      let rule = args.when !== undefined ? wakeRule(String(args.when), args.repeat === undefined ? undefined : String(args.repeat)) : args
      const initialAt = rule.initial_at
      if (initialAt) rule = { every_seconds: rule.every_seconds }
      const record = recordFor(id, title, text, rule, now)
      const finalRecord = initialAt ? decodeScheduleRecord({ ...record, scheduledAt: initialAt }) : record
      const stamp = new Date(now).toISOString()
      const row: Row = { task_id: id, target, text,
        creation_source: args.creation_source === 'manual' ? 'manual' : 'agent',
        reference, record: finalRecord, status: 'pending', created_at: stamp, updated_at: stamp,
        last_triggered_at: null, occurrence: 1, history: [] }
      this.rows.set(id, row)
      this.save(); this.arm()
      return this.view(row)
    })
  }

  update(id: string, args: TaskInput) {
    return this.serialized(() => {
      const row = this.rows.get(id)
      if (!row || row.status !== 'pending') throw new Error('待执行任务不存在。')
      const now = Date.now()
      const text = String(args.text ?? args.prompt ?? row.text).trim()
      const title = String(args.title ?? row.record.title).trim()
      if (!text || !title) throw new Error('任务名称和说明不能为空。')
      let record: ScheduleRecord
      if (args.when !== undefined) {
        const rule = wakeRule(String(args.when), args.repeat ? String(args.repeat) : undefined)
        const initialAt = rule.initial_at
        if (initialAt) delete rule.initial_at
        record = recordFor(id, title, text, rule, now)
        if (initialAt) record = decodeScheduleRecord({ ...record, scheduledAt: initialAt })
      } else if (args.change) record = recordFor(id, title, text, args.change as TaskInput, now)
      else record = decodeScheduleRecord({ ...row.record, title, prompt: text })
      row.record = record
      row.text = text
      const target = taskTarget(args.target ?? row.target)
      if (!['voice_llm', 'shadow_agent', 'both'].includes(target)) throw new Error('target（任务目标）无效。')
      row.target = target
      row.updated_at = new Date(now).toISOString()
      delete row.retry_at
      this.save(); this.arm()
      return this.view(row)
    })
  }

  cancel(id: string) {
    return this.serialized(() => {
      const row = this.rows.get(id)
      if (!row) throw new Error('任务不存在。')
      row.status = 'cancelled'; row.updated_at = new Date().toISOString()
      this.save(); this.arm()
      return this.view(row)
    })
  }

  removeArchive(id: string): Promise<{ task_id: string; deleted: true }> {
    return this.serialized(() => {
      const row = this.rows.get(id)
      if (row?.status === 'pending') throw new Error('待执行任务请先取消，再删除归档记录。')
      if (!row && this.deletedIds.has(id)) return { task_id: id, deleted: true }
      if (!row) throw new Error('任务不存在。')
      this.rows.delete(id)
      this.deletedIds.add(id)
      try { this.save() }
      catch (error) {
        this.rows.set(id, row)
        this.deletedIds.delete(id)
        throw error
      }
      this.arm()
      return { task_id: id, deleted: true }
    })
  }

  private arm(): void {
    if (this.timer) clearTimeout(this.timer)
    if (this.closed) return
    const now = Date.now()
    const due = [...this.rows.values()].filter(row => row.status === 'pending')
      .map(row => Math.max(Date.parse(row.record.scheduledAt), row.retry_at ?? 0))
      .reduce((minimum, value) => Math.min(minimum, value), Infinity)
    if (Number.isFinite(due)) this.timer = setTimeout(() => void this.drive(), Math.min(Math.max(0, due - now), 30_000))
  }

  private async drive(): Promise<void> {
    await this.serialized(async () => {
      if (this.closed) return
      const now = Date.now()
      for (const row of this.rows.values()) {
        if (this.closed) break
        if (row.status !== 'pending' || Math.max(Date.parse(row.record.scheduledAt), row.retry_at ?? 0) > now) continue
        const occurrenceId = `${row.task_id}-${row.occurrence}`
        try {
          const receipt = await voiceApi('/api/internal/dsh-bridge/mainline.deliver', 'POST', {
            task_id: row.task_id, occurrence_id: occurrenceId, text: row.text, target: row.target,
          }, this.deliveryController.signal)
          const deliveredAt = new Date().toISOString()
          row.last_triggered_at = deliveredAt
          row.history.push({ scheduledAt: row.record.scheduledAt, deliveredAt,
            messageId: receipt.message_id ?? occurrenceId, dshSessionId: receipt.dsh_session_id, prompt: row.text })
          row.history = row.history.slice(-200)
          row.occurrence += 1
          delete row.retry_at
          if (isRecurringScheduleRecord(row.record)) {
            const next = resolveRecurringOccurrence(row.record, Date.now()).nextScheduledAt
            row.status = next ? 'pending' : 'archived'
            if (next) row.record = decodeScheduleRecord({ ...row.record, scheduledAt: next })
          } else row.status = 'archived'
          row.updated_at = deliveredAt
          this.save()
        } catch (error) {
          row.retry_at = Date.now() + RETRY_MS
          console.warn('[voice-terminal-wake-tasks] delivery retry', row.task_id, String(error))
        }
      }
      this.arm()
    })
  }

  async close(): Promise<void> {
    this.closed = true
    this.deliveryController.abort()
    if (this.timer) clearTimeout(this.timer)
    await this.chain
  }
}

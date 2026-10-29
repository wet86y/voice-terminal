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
const MIN_EVERY_SECONDS = 60
const MAX_EVERY_SECONDS = 24 * 60 * 60
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
  /** loop（是否持续循环）：仅 every（间隔）规则使用；旧任务缺省为 true。 */
  loop?: boolean
  /** trigger_count（总触发次数）：包含第一次成功触发。 */
  trigger_count?: number
  occurrence: number; retry_at?: number; history: Record<string, unknown>[];
  /** delivery_pending（投递结果待确认）：发送前持久化，重试与重启期间固定当前触发内容。 */
  delivery_pending?: boolean
}

type RecurrenceSettings = Pick<Row, 'loop' | 'trigger_count'>

function validateEverySeconds(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value)
      || value < MIN_EVERY_SECONDS || value > MAX_EVERY_SECONDS || value % 60 !== 0) {
    throw new Error('every_seconds（间隔秒数）必须是 60 至 86400 秒之间的整分钟数。')
  }
  return value
}

function validateTriggerCount(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) {
    throw new Error('trigger_count（总触发次数）必须是正整数。')
  }
  return value
}

function recurrenceSettings(args: TaskInput, isEvery: boolean, current?: Row): RecurrenceSettings {
  if (!isEvery) {
    if (args.loop !== undefined || args.trigger_count !== undefined) {
      throw new Error('loop（是否循环）和 trigger_count（总触发次数）只适用于间隔规则。')
    }
    return {}
  }
  if (args.loop !== undefined && typeof args.loop !== 'boolean') {
    throw new Error('loop（是否循环）必须是布尔值。')
  }
  const sameEvery = current?.record.kind === 'every'
  const loop = args.loop ?? (sameEvery ? current?.loop ?? true : true)
  if (loop) {
    if (args.trigger_count !== undefined) throw new Error('循环任务不能同时设置 trigger_count（总触发次数）。')
    return { loop: true }
  }
  const countValue = args.trigger_count !== undefined
    ? args.trigger_count
    : sameEvery ? current?.trigger_count : undefined
  if (countValue === undefined) throw new Error('关闭循环时必须提供 trigger_count（总触发次数）。')
  return { loop: false, trigger_count: validateTriggerCount(countValue) }
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
      || (raw.delivery_pending !== undefined && typeof raw.delivery_pending !== 'boolean')
      || !Array.isArray(raw.history)
      || (raw.status !== 'pending' && raw.status !== 'archived' && raw.status !== 'cancelled')) {
    throw new Error('DSH 任务存储行格式无效。')
  }
  const record = decodeScheduleRecord(raw.record)
  const storedCount = raw.trigger_count === null ? undefined : raw.trigger_count
  let loop: boolean | undefined
  let triggerCount: number | undefined
  if (record.kind === 'every') {
    loop = raw.loop === undefined ? storedCount === undefined : raw.loop === true
    if (raw.loop !== undefined && typeof raw.loop !== 'boolean') throw new Error('loop（是否循环）存储值无效。')
    if (storedCount !== undefined) triggerCount = validateTriggerCount(storedCount)
    if ((loop && triggerCount !== undefined) || (!loop && triggerCount === undefined)) {
      throw new Error('间隔任务的循环与总触发次数存储值不一致。')
    }
  } else if (raw.loop !== undefined || storedCount !== undefined) {
    throw new Error('非间隔任务不能保存循环或总触发次数。')
  }
  const decoded: Row = { ...raw, task_id: raw.task_id, text: raw.text, reference: raw.reference,
    target: taskTarget(raw.target), created_at: raw.created_at, updated_at: raw.updated_at,
    last_triggered_at: raw.last_triggered_at, occurrence: raw.occurrence, history: raw.history.map(jsonRecord),
    status: raw.status, record,
    ...(raw.creation_source === 'manual' || raw.creation_source === 'agent' || raw.creation_source === 'unknown'
      ? { creation_source: raw.creation_source } : {}),
    ...(typeof raw.retry_at === 'number' ? { retry_at: raw.retry_at } : {}),
  }
  if (record.kind === 'every') {
    decoded.loop = loop ?? true
    if (triggerCount === undefined) delete decoded.trigger_count
    else decoded.trigger_count = triggerCount
  } else {
    delete decoded.loop
    delete decoded.trigger_count
  }
  return decoded
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
    case 'every_seconds': return createEveryScheduleRecord(ScheduleId(id), text, validateEverySeconds(args.every_seconds), now, title)
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
    validateEverySeconds(seconds)
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
  private retryAt = new Map<string, number>() // retryAt（瞬时重试截止）：不改写已提交的任务快照。
  private settlements = new Map<string, Row>() // settlements（已获投递回执的待保存结算）：写盘前冻结正文和触发序号。

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
    if (migrated) this.commitState(this.rows)
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

  /** commitState（提交候选状态）：磁盘原子换入成功后才替换生效状态。 */
  private commitState(rows: Map<string, Row>, deletedIds = this.deletedIds): void {
    mkdirSync(dirname(STORE_PATH), { recursive: true })
    const temporary = `${STORE_PATH}.${randomUUID()}.tmp`
    try {
      writeFileSync(temporary, JSON.stringify({ schema_version: 1, tasks: [...rows.values()], deleted_task_ids: [...deletedIds] }, null, 2), 'utf8')
      renameSync(temporary, STORE_PATH)
    } catch (error) {
      try { unlinkSync(temporary) } catch { /* temporary（临时存储）可能尚未创建。 */ }
      throw error
    }
    for (const id of this.retryAt.keys()) {
      if (rows.get(id) !== this.rows.get(id)) this.retryAt.delete(id)
    }
    this.rows = rows
    this.deletedIds = deletedIds
  }

  /** flushSettlements（补交已确认回执）：失败时保留原任务和待保存结算。 */
  private flushSettlements(): void {
    if (!this.settlements.size) return
    const rows = new Map(this.rows)
    for (const [id, row] of this.settlements) rows.set(id, row)
    this.commitState(rows)
    this.settlements.clear()
  }

  private serialized<T>(work: () => Promise<T> | T): Promise<T> {
    const next = this.chain.then(work, work)
    this.chain = next.catch(() => undefined)
    return next
  }

  /** change（串行用户变更）：先保存已接受的投递，避免新正文复用旧触发的回执。 */
  private change<T>(work: () => T): Promise<T> {
    return this.serialized(() => {
      this.flushSettlements()
      try { return work() }
      finally { this.arm() }
    })
  }

  view(row: Row) {
    const record = row.record
    return {
      id: row.task_id, task_id: row.task_id, title: record.title, target: row.target,
      when: record.scheduledAt, repeat: record.kind === 'every' ? `${record.everySeconds}s` : null,
      ...(record.kind === 'every' ? { loop: row.loop ?? true, trigger_count: row.trigger_count ?? null } : {}),
      ...(record.kind === 'cron' ? { cron_input: { expression: record.expression, time_zone: record.timeZone } } : {}),
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
    return this.change(() => {
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
      const recurrence = recurrenceSettings(args, finalRecord.kind === 'every')
      const stamp = new Date(now).toISOString()
      const row: Row = { task_id: id, target, text,
        creation_source: args.creation_source === 'manual' ? 'manual' : 'agent',
        reference, record: finalRecord, status: 'pending', created_at: stamp, updated_at: stamp,
        last_triggered_at: null, occurrence: 1, history: [], ...recurrence }
      this.commitState(new Map(this.rows).set(id, row))
      return this.view(row)
    })
  }

  update(id: string, args: TaskInput) {
    return this.change(() => {
      const current = this.rows.get(id)
      if (!current || current.status !== 'pending') throw new Error('待执行任务不存在。')
      if (current.delivery_pending) throw new Error('本次投递结果尚未确认，暂不能修改任务；可等待确认，或取消后新建任务。')
      const row = { ...current }
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
      const target = taskTarget(args.target ?? row.target)
      if (!['voice_llm', 'shadow_agent', 'both'].includes(target)) throw new Error('target（任务目标）无效。')
      const recurrence = recurrenceSettings(args, record.kind === 'every', row)
      row.record = record
      row.text = text
      row.target = target
      if (record.kind === 'every') {
        row.loop = recurrence.loop!
        if (recurrence.trigger_count === undefined) delete row.trigger_count
        else row.trigger_count = recurrence.trigger_count
        if (row.loop === false && row.trigger_count! < row.occurrence) row.status = 'archived'
      } else {
        delete row.loop
        delete row.trigger_count
      }
      row.updated_at = new Date(now).toISOString()
      delete row.retry_at
      this.commitState(new Map(this.rows).set(id, row))
      return this.view(row)
    })
  }

  cancel(id: string) {
    return this.change(() => {
      const row = this.rows.get(id)
      if (!row) throw new Error('任务不存在。')
      const updated: Row = { ...row, status: 'cancelled', updated_at: new Date().toISOString() }
      delete updated.delivery_pending
      this.commitState(new Map(this.rows).set(id, updated))
      return this.view(updated)
    })
  }

  removeArchive(id: string): Promise<{ task_id: string; deleted: true }> {
    return this.change(() => {
      const row = this.rows.get(id)
      if (row?.status === 'pending') throw new Error('待执行任务请先取消，再删除归档记录。')
      if (!row && this.deletedIds.has(id)) return { task_id: id, deleted: true }
      if (!row) throw new Error('任务不存在。')
      const rows = new Map(this.rows)
      rows.delete(id)
      this.commitState(rows, new Set(this.deletedIds).add(id))
      return { task_id: id, deleted: true }
    })
  }

  /** deadline（允许触发时刻）：同时遵守历史重试登记和当前进程的退避。 */
  private deadline(row: Row): number {
    return Math.max(Date.parse(row.record.scheduledAt), row.retry_at ?? 0, this.retryAt.get(row.task_id) ?? 0)
  }

  private arm(): void {
    if (this.timer) clearTimeout(this.timer)
    if (this.closed) return
    const now = Date.now()
    const due = [...this.rows.values()].filter(row => row.status === 'pending')
      .map(row => this.deadline(row))
      .reduce((minimum, value) => Math.min(minimum, value), Infinity)
    if (Number.isFinite(due)) this.timer = setTimeout(() => void this.drive(), Math.min(Math.max(0, due - now), 30_000))
  }

  private async drive(): Promise<void> {
    await this.serialized(async () => {
      if (this.closed) return
      const now = Date.now()
      for (let row of [...this.rows.values()]) {
        if (this.closed) break
        if (this.rows.get(row.task_id) !== row || row.status !== 'pending' || this.deadline(row) > now) continue
        const occurrenceId = `${row.task_id}-${row.occurrence}`
        try {
          if (!row.delivery_pending) {
            row = { ...row, delivery_pending: true }
            this.commitState(new Map(this.rows).set(row.task_id, row))
          }
          if (!this.settlements.has(row.task_id)) {
            const receipt = await voiceApi('/api/internal/dsh-bridge/mainline.deliver', 'POST', {
              task_id: row.task_id, occurrence_id: occurrenceId, text: row.text, target: row.target,
            }, this.deliveryController.signal)
            const deliveredAt = new Date().toISOString()
            const updated: Row = { ...row, last_triggered_at: deliveredAt, updated_at: deliveredAt,
              history: [...row.history, { scheduledAt: row.record.scheduledAt, deliveredAt,
                messageId: receipt.message_id ?? occurrenceId, dshSessionId: receipt.dsh_session_id, prompt: row.text }].slice(-200),
              occurrence: row.occurrence + 1 }
            delete updated.retry_at
            delete updated.delivery_pending
            if (updated.record.kind === 'every' && updated.loop === false
                && updated.trigger_count !== undefined && updated.occurrence > updated.trigger_count) {
              updated.status = 'archived'
            } else if (isRecurringScheduleRecord(updated.record)) {
              const next = resolveRecurringOccurrence(updated.record, Date.now()).nextScheduledAt
              updated.status = next ? 'pending' : 'archived'
              if (next) updated.record = decodeScheduleRecord({ ...updated.record, scheduledAt: next })
            } else updated.status = 'archived'
            this.settlements.set(row.task_id, updated)
          }
          this.flushSettlements()
        } catch (error) {
          this.retryAt.set(row.task_id, Date.now() + RETRY_MS)
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

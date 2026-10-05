import { randomUUID } from 'node:crypto'
import { readFileSync, mkdirSync, writeFileSync, renameSync } from 'node:fs'
import { dirname } from 'node:path'
import { pathToFileURL } from 'node:url'

const scheduleModulePath = process.env.VOICE_DSH_SCHEDULE_MODULE ?? ''
const scheduleModule = scheduleModulePath ? await import(pathToFileURL(scheduleModulePath).href) : {} as any
const {
  createAfterScheduleRecord, createAtScheduleRecord, createEveryScheduleRecord,
  createDailyScheduleRecord, createWeeklyScheduleRecord, createCronScheduleRecord,
  decodeScheduleRecord, isRecurringScheduleRecord, resolveRecurringOccurrence,
} = scheduleModule

export const CALLBACK_ORIGIN = process.env.VOICE_DSH_CALLBACK_ORIGIN ?? ''
export const TOKEN = process.env.VOICE_DSH_BRIDGE_TOKEN ?? ''
const STORE_PATH = process.env.VOICE_DSH_TASKS_PATH ?? ''
const LEGACY_PATH = process.env.VOICE_DSH_LEGACY_WAKE_PATH ?? ''
const RETRY_MS = 30_000

type Row = {
  task_id: string; target: string; text: string; reference: string;
  record: any; status: 'pending' | 'archived' | 'cancelled';
  created_at: string; updated_at: string; last_triggered_at: string | null;
  occurrence: number; retry_at?: number; history: any[];
}

function readJson(path: string): any | undefined {
  if (!path) return undefined
  try { return JSON.parse(readFileSync(path, 'utf8')) }
  catch (error: any) {
    if (error?.code === 'ENOENT') return undefined
    throw error
  }
}

function duration(value: string): number {
  const match = /^\+?(\d+)\s*([smhd])$/iu.exec(value.trim())
  if (!match) throw new Error('repeat/when（重复间隔／时间）应为 30m、2h 等时长。')
  const seconds = Number(match[1]) * ({ s: 1, m: 60, h: 3600, d: 86400 } as any)[match[2].toLowerCase()]
  if (!Number.isSafeInteger(seconds) || seconds < 1) throw new Error('时长无效。')
  return seconds
}

function recordFor(id: string, title: string, text: string, args: any, now: number): any {
  const selectors = ['after_seconds', 'at', 'every_seconds', 'daily', 'weekly', 'cron']
    .filter(key => args[key] !== undefined && args[key] !== null)
  if (selectors.length !== 1) throw new Error('必须且只能提供一个定时规则。')
  switch (selectors[0]) {
    case 'after_seconds': return createAfterScheduleRecord(id as any, text, Number(args.after_seconds), now, title)
    case 'at': return createAtScheduleRecord(id as any, text, args.at, now, title)
    case 'every_seconds': return createEveryScheduleRecord(id as any, text, Number(args.every_seconds), now, title)
    case 'daily': return createDailyScheduleRecord(id as any, text, args.daily, now, title)
    case 'weekly': return createWeeklyScheduleRecord(id as any, text, args.weekly, now, title)
    default: return createCronScheduleRecord(id as any, text, args.cron, now, title)
  }
}

function wakeRule(when: string, repeat: string | undefined): any {
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

export async function voiceApi(path: string, method: string, body?: unknown): Promise<any> {
  if (!CALLBACK_ORIGIN.startsWith('http://127.0.0.1:') || !TOKEN) throw new Error('Voice Bridge（语音桥接）不可用。')
  const response = await fetch(`${CALLBACK_ORIGIN}${path}`, {
    method,
    signal: AbortSignal.timeout(15_000),
    headers: { Authorization: `Bearer ${TOKEN}`, Accept: 'application/json',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const data: any = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(String(data.detail ?? `HTTP ${response.status}`))
  return data
}

export class TaskService {
  private rows = new Map<string, Row>()
  private timer: ReturnType<typeof setTimeout> | undefined
  private chain: Promise<unknown> = Promise.resolve()
  private closed = false

  constructor() {
    if (!STORE_PATH || !scheduleModulePath) throw new Error('DSH 任务存储路径或调度规则模块缺失。')
    const stored = readJson(STORE_PATH)
    if (stored !== undefined) {
      if (stored.schema_version !== 1 || !Array.isArray(stored.tasks)) throw new Error('DSH 任务存储文件格式无效。')
      for (const row of stored.tasks) {
        row.record = decodeScheduleRecord(row.record)
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
    for (const [id, value] of Object.entries(oldRows ?? {})) {
      if (this.rows.has(id)) continue
      const task: any = value
      const due = Number(task.scheduled_at)
      if (!Number.isFinite(due) || !String(task.text ?? '').trim()) throw new Error(`旧任务 ${id} 无法转换。`)
      const seconds = Number(task.repeat_seconds)
      const raw = seconds > 0
        ? { id, kind: 'every', title: String(task.text).slice(0, 120), prompt: task.text, everySeconds: seconds, scheduledAt: new Date(due * 1000).toISOString() }
        : { id, kind: 'at', title: String(task.text).slice(0, 120), prompt: task.text, scheduledAt: new Date(due * 1000).toISOString() }
      const record = decodeScheduleRecord(raw)
      this.rows.set(id, {
        task_id: id, target: String(task.target ?? 'shadow_agent'), text: task.text,
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
    writeFileSync(temporary, JSON.stringify({ schema_version: 1, tasks: [...this.rows.values()] }, null, 2), 'utf8')
    renameSync(temporary, STORE_PATH)
  }

  private serialized<T>(work: () => Promise<T> | T): Promise<T> {
    const next = this.chain.then(work, work)
    this.chain = next.catch(() => undefined)
    return next
  }

  view(row: Row): any {
    const record = row.record
    return {
      id: row.task_id, task_id: row.task_id, title: record.title, target: row.target,
      when: record.scheduledAt, repeat: record.kind === 'every' ? `${record.everySeconds}s` : null,
      rule_kind: record.kind, rule: record,
      text: row.text, status: row.status, created_at: row.created_at,
      updated_at: row.updated_at, last_triggered_at: row.last_triggered_at,
      occurrence: row.occurrence, has_reference: !!row.reference,
      delivery_history: row.history,
    }
  }

  list(): any[] { return [...this.rows.values()].map(row => this.view(row)) }
  reference(id: string): any {
    const row = this.rows.get(id)
    if (!row) throw new Error('任务不存在。')
    return { task_id: id, reference: row.reference }
  }

  create(args: any, reference = ''): Promise<any> {
    return this.serialized(() => {
      const id = `schedule-${randomUUID()}`
      const now = Date.now()
      const text = String(args.text ?? args.prompt ?? '').trim()
      const title = String(args.title ?? text.slice(0, 120)).trim()
      const target = String(args.target ?? 'shadow_agent')
      if (!['voice_llm', 'shadow_agent', 'both'].includes(target)) throw new Error('target（任务目标）无效。')
      if (!text || !title || reference.length > 120_000) throw new Error('任务说明或参考上下文无效。')
      let rule = args.when !== undefined ? wakeRule(String(args.when), args.repeat) : args
      const initialAt = rule.initial_at
      if (initialAt) rule = { every_seconds: rule.every_seconds }
      const record = recordFor(id, title, text, rule, now)
      const finalRecord = initialAt ? decodeScheduleRecord({ ...record, scheduledAt: initialAt }) : record
      const stamp = new Date(now).toISOString()
      const row: Row = { task_id: id, target, text,
        reference, record: finalRecord, status: 'pending', created_at: stamp, updated_at: stamp,
        last_triggered_at: null, occurrence: 1, history: [] }
      this.rows.set(id, row)
      this.save(); this.arm()
      return this.view(row)
    })
  }

  update(id: string, args: any): Promise<any> {
    return this.serialized(() => {
      const row = this.rows.get(id)
      if (!row || row.status !== 'pending') throw new Error('待执行任务不存在。')
      const now = Date.now()
      const text = String(args.text ?? args.prompt ?? row.text).trim()
      const title = String(args.title ?? row.record.title).trim()
      if (!text || !title) throw new Error('任务名称和说明不能为空。')
      let record: any
      if (args.when !== undefined) {
        const rule = wakeRule(String(args.when), args.repeat || undefined)
        const initialAt = rule.initial_at
        if (initialAt) delete rule.initial_at
        record = recordFor(id, title, text, rule, now)
        if (initialAt) record = decodeScheduleRecord({ ...record, scheduledAt: initialAt })
      } else if (args.change) record = recordFor(id, title, text, args.change, now)
      else record = decodeScheduleRecord({ ...row.record, title, prompt: text })
      row.record = record
      row.text = text
      const target = String(args.target ?? row.target)
      if (!['voice_llm', 'shadow_agent', 'both'].includes(target)) throw new Error('target（任务目标）无效。')
      row.target = target
      row.updated_at = new Date(now).toISOString()
      row.retry_at = undefined
      this.save(); this.arm()
      return this.view(row)
    })
  }

  cancel(id: string): Promise<any> {
    return this.serialized(() => {
      const row = this.rows.get(id)
      if (!row) throw new Error('任务不存在。')
      row.status = 'cancelled'; row.updated_at = new Date().toISOString()
      this.save(); this.arm()
      return this.view(row)
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
      const now = Date.now()
      for (const row of this.rows.values()) {
        if (row.status !== 'pending' || Math.max(Date.parse(row.record.scheduledAt), row.retry_at ?? 0) > now) continue
        const occurrenceId = `${row.task_id}-${row.occurrence}`
        try {
          const receipt = await voiceApi('/api/internal/dsh-bridge/mainline.deliver', 'POST', {
            task_id: row.task_id, occurrence_id: occurrenceId, text: row.text, target: row.target,
          })
          const deliveredAt = new Date().toISOString()
          row.last_triggered_at = deliveredAt
          row.history.push({ scheduledAt: row.record.scheduledAt, deliveredAt,
            messageId: receipt.message_id ?? occurrenceId, dshSessionId: receipt.dsh_session_id, prompt: row.text })
          row.history = row.history.slice(-200)
          row.occurrence += 1
          row.retry_at = undefined
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
    if (this.timer) clearTimeout(this.timer)
    await this.chain
  }
}

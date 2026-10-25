import type { Agent } from '@deepseek-ai/dsh-agent'
import type { ToolRunContext } from '@deepseek-ai/dsh-tools'
import type { Context } from '../shared/services.js'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { stableVoiceEventId } from './event-id.js'
import { authorized } from '../shared/protocol.js'
import { record } from '../shared/json-record.js'
import { voiceApi } from './voice-api.js'
import { TaskService } from './task-service.js'

export const name = 'voice-terminal-wake-tasks'
export const inject = ['agents', 'webServer']

const MAX_REFERENCE_CHARS = 100_000
const cronInputSchema = {
  type: 'object' as const,
  description: 'cron（Cron 定时规则）：与 DSH 官方 schedule_create（创建会话任务）相同的严格五字段 Vixie Cron 格式。',
  properties: {
    expression: {
      type: 'string' as const,
      required: true as const,
      description: 'expression（五字段表达式）：格式为“分钟 小时 日期 月份 星期”；例如 0 9 * * 1-5 表示工作日 09:00。不接受 HH:mm、秒字段或扩展语法。',
    },
    time_zone: {
      type: 'string' as const,
      required: true as const,
      description: 'time_zone（时区）：UTC 或 IANA 区域/地点，例如 Asia/Hong_Kong。',
    },
  },
  additionalProperties: false as const,
}

const atInputSchema = {
  description: 'at（单次绝对时点）：可传带 Z（协调世界时）或数字偏移的 RFC 3339 日期时间，也可传本地日期时间对象。',
  oneOf: [
    {
      type: 'string' as const,
      description: 'RFC 3339（日期时间格式），必须包含 Z 或时区偏移，例如 2026-10-26T09:00:00+08:00。',
    },
    {
      type: 'object' as const,
      additionalProperties: false as const,
      description: '本地日期时间对象，date（日期）、time（时间）和 time_zone（时区）均必填。',
      properties: {
        date: { type: 'string' as const, required: true as const, description: 'date（日期）：YYYY-MM-DD（年-月-日）。' },
        time: { type: 'string' as const, required: true as const, description: 'time（时间）：HH:mm:ss（时:分:秒），可带 1 至 3 位小数秒。' },
        time_zone: { type: 'string' as const, required: true as const, description: 'time_zone（时区）：UTC 或 IANA 区域/地点，例如 Asia/Hong_Kong。' },
      },
    },
  ] as const,
} as const

const dailyInputSchema = {
  type: 'object' as const,
  additionalProperties: false as const,
  description: 'daily（每日规则）：每天在指定时区的本地时间触发。',
  properties: {
    time: { type: 'string' as const, required: true as const, description: 'time（时间）：HH:mm:ss（时:分:秒），可带 1 至 3 位小数秒。' },
    time_zone: { type: 'string' as const, required: true as const, description: 'time_zone（时区）：UTC 或 IANA 区域/地点，例如 Asia/Hong_Kong。' },
  },
} as const

const weeklyInputSchema = {
  type: 'object' as const,
  additionalProperties: false as const,
  description: 'weekly（每周规则）：在指定星期和时区的本地时间触发。',
  properties: {
    time: { type: 'string' as const, required: true as const, description: 'time（时间）：HH:mm:ss（时:分:秒），可带 1 至 3 位小数秒。' },
    time_zone: { type: 'string' as const, required: true as const, description: 'time_zone（时区）：UTC 或 IANA 区域/地点，例如 Asia/Hong_Kong。' },
    weekdays: {
      type: 'array' as const,
      required: true as const,
      description: 'weekdays（星期列表）：非空 ISO 星期数字；周一为 1，周日为 7，不得重复。',
      items: { type: 'integer' as const },
    },
  },
} as const

const everySecondsSchema = {
  type: 'integer' as const,
  description: 'every_seconds（间隔秒数）：60 至 86400 秒的整分钟数，范围为 1 分钟至 24 小时。',
} as const

function responseToolOutput() {
  const nullableText = (description: string) => ({
    oneOf: [
      { type: 'string' as const },
      { type: 'null' as const },
    ] as const,
    description,
  })
  const nullableBoolean = (description: string) => ({
    oneOf: [{ type: 'boolean' as const }, { type: 'null' as const }] as const,
    description,
  })
  const nullableInteger = (description: string) => ({
    oneOf: [{ type: 'integer' as const }, { type: 'null' as const }] as const,
    description,
  })
  return {
    schema: {
      type: 'object' as const,
      properties: {
        task_id: { type: 'string' as const, description: 'task_id（任务标识）：唤醒任务的唯一标识。' },
        target: {
          type: 'string' as const,
          enum: ['voice_llm', 'shadow_agent', 'both'] as const,
          description: 'target（唤醒目标）：voice_llm、shadow_agent 或 both。',
        },
        when: nullableText('when（触发时间）：按项目本地时区表达的时间。'),
        repeat: nullableText('repeat（间隔）：按秒数字符串返回，范围为 60 至 86400 秒的整分钟间隔；空值表示非间隔任务。'),
        loop: nullableBoolean('loop（是否持续循环）：true（是）持续循环，false（否）按 trigger_count（总触发次数）后归档；非间隔任务为空。'),
        trigger_count: nullableInteger('trigger_count（总触发次数）：包含第一次成功触发；持续循环或非间隔任务为空。'),
        text: { type: 'string' as const, description: 'text（任务说明）：触发时传给目标 Agent 的方向。' },
        status: {
          type: 'string' as const,
          enum: ['pending', 'archived', 'cancelled'] as const,
          description: 'status（任务状态）：pending、archived 或 cancelled。',
        },
        created_at: nullableText('created_at（创建时间）。'),
        updated_at: nullableText('updated_at（更新时间）。'),
        last_triggered_at: nullableText('last_triggered_at（最近触发时间）。'),
        occurrence: { type: 'integer' as const, description: 'occurrence（当前循环序号）。' },
        has_reference: { type: 'boolean' as const, description: 'has_reference（是否有冻结参考上下文）。' },
        reference: { type: 'string' as const, description: 'reference（创建任务时冻结的 DSH 上下文，只读）。' },
        cron_input: {
          ...cronInputSchema,
          description: 'cron_input（Cron 规则的规范输入，可直接传给 wake_task_update（修改任务）的 cron 字段）。',
        },
      },
      additionalProperties: true,
    },
    render: (_args: unknown, value: unknown) => [{ type: 'text' as const, text: JSON.stringify(value) }],
  }
}

function textFrom(value: unknown, budget: { chars: number }): string {
  if (budget.chars <= 0) return ''
  if (typeof value === 'string') {
    const result = value.slice(0, budget.chars)
    budget.chars -= result.length
    return result
  }
  if (Array.isArray(value)) {
    return value.map(item => textFrom(item, budget)).filter(Boolean).join('\n')
  }
  if (typeof value !== 'object' || value === null) return ''
  const record = value as Record<string, unknown>
  if (typeof record.text === 'string') return textFrom(record.text, budget)
  if (typeof record.content === 'string' || Array.isArray(record.content)) return textFrom(record.content, budget)
  return ''
}

function freezeCurrentContext(agent: Agent): string {
  const events = agent.session.snapshotEvents().slice(-240)
  const budget = { chars: MAX_REFERENCE_CHARS }
  const rows: Array<{ role: string; text: string }> = []
  for (const event of events) {
    if (!['user/message', 'assistant/message', 'tool/result'].includes(String(event?.type))) continue
    const data = event.data as Record<string, unknown>
    const source = (data.message ?? data) as Record<string, unknown>
    const role = event.type === 'user/message' ? 'user' : event.type === 'tool/result' ? 'tool' : 'assistant'
    const text = textFrom(source?.content ?? source, budget).trim()
    if (text) rows.push({ role, text })
    if (budget.chars <= 0) break
  }
  return JSON.stringify(rows)
}

function installAgentTools(agentCtx: Context, agent: Agent, service: TaskService): () => void {
  const dispose: Array<() => void> = []
  const ensureRoot = (exec: ToolRunContext): void => {
    if (exec.agent !== agent) {
      throw new Error('Wake task tools are available only to the DSH root Agent')
    }
  }
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'schedule_wake',
    description: '创建项目级定时唤醒。创建时冻结当前 DSH 文本上下文，供触发后的任务按需读取；参考不会自动注入。',
    parameters: {
      target: { type: 'string', enum: ['voice_llm', 'shadow_agent', 'both'], required: true, description: 'target（目标）：唤醒 voice_llm（语音大模型）、shadow_agent（影子代理）或 both（双方）。' },
      when: { type: 'string', required: true, description: 'when（时间）：延迟如 30m、2h，或项目本地时区的 ISO 日期时间。' },
      repeat: { type: 'string', description: 'repeat（重复间隔）：可选整分钟间隔，范围为 1 分钟至 24 小时；省略表示一次性。' },
      loop: { type: 'boolean', description: 'loop（是否持续循环）：仅 repeat（重复间隔）有效，默认 true（是）；false（否）时必须提供 trigger_count（总触发次数）。' },
      trigger_count: { type: 'integer', description: 'trigger_count（总触发次数）：正整数，包含第一次成功触发；关闭 loop（循环）时必填。' },
      text: { type: 'string', required: true, description: 'text（任务说明）：触发时传给目标 Agent 的简短方向。' },
    },
    output: responseToolOutput(),
    async execute(args, exec) {
      ensureRoot(exec)
      const reference = freezeCurrentContext(agent)
      return service.create(args, reference)
    },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'cancel_wake',
    description: '取消一个待执行的项目级唤醒任务；task_id（任务标识）来自创建结果。',
    parameters: {
      task_id: { type: 'string', required: true, description: 'task_id（任务标识）：schedule_wake 返回的任务标识。' },
    },
    output: responseToolOutput(),
    async execute(args, exec) {
      ensureRoot(exec)
      return service.cancel(args.task_id)
    },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'read_wake_reference',
    description: '按需读取任务创建时冻结的 DSH 上下文参考；该参考与当前会话隔离且只读。',
    parameters: {
      task_id: { type: 'string', required: true, description: 'task_id（任务标识）：触发消息或创建结果中的任务标识。' },
    },
    output: responseToolOutput(),
    async execute(args, exec) {
      ensureRoot(exec)
      return service.reference(args.task_id)
    },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'publish_voice_event',
    description: '把自由文本事件投递到 Voice（语音）持久信箱；无需 Voice 先唤醒 DSH。',
    parameters: {
      text: { type: 'string', required: true, description: 'text（事件内容）：给语音模型处理的普通事件文本。' },
      event_id: { type: 'string', description: 'event_id（事件标识）：重试同一事件时复用，可省略。' },
      payload: { type: 'object', description: 'payload（附加内容）：可选的结构化数据。', additionalProperties: true },
    },
    output: {
      schema: { type: 'object', properties: {
        result: { type: 'string', enum: ['accepted', 'duplicate'], description: 'result（投递结果）：accepted（已接收）或 duplicate（幂等重复）。' },
        event_id: { type: 'string', description: 'event_id（事件标识）。' },
      }, additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(args, exec) {
      ensureRoot(exec)
      const sourceSessionId = String(agent.session.id)
      const result = await voiceApi('/api/internal/dsh-bridge/voice.publish', 'POST', {
        event_id: args.event_id || stableVoiceEventId(sourceSessionId, exec.callId),
        source_session_id: sourceSessionId,
        text: args.text, ...(args.payload ? { payload: args.payload } : {}),
      }, exec.signal)
      if (result.result !== 'accepted' && result.result !== 'duplicate') throw new Error('Voice 信箱未接受事件。')
      exec.concludeTurn()
      return { ...result, result: result.result as 'accepted' | 'duplicate' }
    },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'wake_task_create',
    description: '创建 Voice（语音）链路定时任务；任务到期后进入当前语音主线，不绑定创建它的 DSH 会话。必须且只能提供一种时间规则字段。DSH 会话提醒请使用官方 schedule_create（创建会话任务）。',
    parameters: {
      title: { type: 'string', required: true, description: 'title（任务名称）。' },
      prompt: { type: 'string', required: true, description: 'prompt（任务内容）。' },
      after_seconds: { type: 'integer', description: 'after_seconds（延迟秒数）：相对当前时间的一次性延迟，须为正整数。' },
      at: atInputSchema,
      every_seconds: everySecondsSchema,
      loop: { type: 'boolean', description: 'loop（是否持续循环）：仅适用于 every_seconds（间隔秒数）；省略时默认为 true（持续循环）。' },
      trigger_count: { type: 'integer', description: 'trigger_count（总触发次数）：正整数，包含第一次成功触发；loop（是否循环）为 false（否）时必填。' },
      daily: dailyInputSchema,
      weekly: weeklyInputSchema,
      cron: cronInputSchema,
    },
    output: responseToolOutput(),
    async execute(args, exec) { ensureRoot(exec); return service.create(args, freezeCurrentContext(agent)) },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'wake_task_list',
    description: '列出项目语音链路任务；不包含官方 DSH 会话提醒。',
    parameters: {},
    output: { schema: { type: 'array', items: responseToolOutput().schema },
      render: (_args: unknown, value: unknown) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(_args, exec) { ensureRoot(exec); return service.list() },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'wake_task_update',
    description: '更新项目语音链路任务的名称、内容或时间规则；不修改官方 DSH 会话提醒。',
    parameters: {
      id: { type: 'string', required: true, description: 'id（任务标识）。' },
      title: { type: 'string', description: 'title（任务名称）。' },
      prompt: { type: 'string', description: 'prompt（任务内容）。' },
      at: atInputSchema,
      every_seconds: everySecondsSchema,
      loop: { type: 'boolean', description: 'loop（是否持续循环）：仅适用于 every_seconds（间隔秒数）；当前间隔任务省略时保留当前状态，其他规则改为间隔时默认为 true（持续循环）。' },
      trigger_count: { type: 'integer', description: 'trigger_count（总触发次数）：正整数，包含第一次成功触发；关闭循环且没有已有次数时必填，可用它修改现有有限次数。' },
      daily: dailyInputSchema,
      weekly: weeklyInputSchema,
      cron: cronInputSchema,
    },
    output: responseToolOutput(),
    async execute(args, exec) {
      ensureRoot(exec)
      const change: Record<string, unknown> = {}
      for (const key of ['at', 'every_seconds', 'daily', 'weekly', 'cron'] as const) if (args[key] !== undefined) change[key] = args[key]
      return service.update(args.id, { title: args.title, prompt: args.prompt,
        ...(Object.keys(change).length ? { change } : {}),
        ...(args.loop !== undefined ? { loop: args.loop } : {}),
        ...(args.trigger_count !== undefined ? { trigger_count: args.trigger_count } : {}) })
    },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'wake_task_delete',
    description: '取消项目语音链路任务；不删除官方 DSH 会话提醒。',
    parameters: { id: { type: 'string', required: true, description: 'id（任务标识）。' } },
    output: responseToolOutput(),
    async execute(args, exec) { ensureRoot(exec); return service.cancel(args.id) },
  })))
  return () => { for (const release of dispose.reverse()) release() }
}

export function apply(ctx: Context): void {
  if (process.env.VOICE_DSH_TASKS_ACTIVE !== '1') return
  const service = new TaskService()
  ctx.effect(() => async () => { await service.close() })
  ctx.effect(() => ctx.webServer.register({
    kind: 'prefix', path: '/api/voice-terminal-wake/v1',
    async handler(req: IncomingMessage, res: ServerResponse) {
      if (!authorized(req)) {
        res.statusCode = 401; res.end('{}'); return
      }
      const path = new URL(req.url ?? '/', 'http://127.0.0.1').pathname.replace(/^\/api\/voice-terminal-wake\/v1\/?/u, '')
      try {
        let body: Record<string, unknown> = {}
        if (req.method !== 'GET') {
          const chunks: Buffer[] = []
          let bytes = 0
          for await (const chunk of req) {
            const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
            bytes += data.length
            if (bytes > 2_097_152) throw new Error('请求体过大。')
            chunks.push(data)
          }
          const parsed: unknown = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {}
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('请求体必须为 JSON（结构化数据）对象。')
          body = record(parsed)
        }
        let value: unknown
        if (req.method === 'GET' && path === 'tasks') value = { tasks: service.list() }
        else if (req.method === 'GET' && /^tasks\/[^/]+\/reference$/u.test(path)) value = service.reference(decodeURIComponent(path.split('/')[1]!))
        else if (req.method === 'POST' && path === 'tasks') value = await service.create(body, String(body.reference ?? ''))
        else if (req.method === 'PATCH' && /^tasks\/[^/]+$/u.test(path)) value = await service.update(decodeURIComponent(path.split('/')[1]!), body)
        else if (req.method === 'DELETE' && /^tasks\/[^/]+\/archive$/u.test(path)) value = await service.removeArchive(decodeURIComponent(path.split('/')[1]!))
        else if (req.method === 'DELETE' && /^tasks\/[^/]+$/u.test(path)) value = await service.cancel(decodeURIComponent(path.split('/')[1]!))
        else { res.statusCode = 404; res.end('{}'); return }
        res.setHeader('content-type', 'application/json; charset=utf-8')
        res.end(JSON.stringify(value))
      } catch (error) {
        res.statusCode = 422
        res.setHeader('content-type', 'application/json; charset=utf-8')
        res.end(JSON.stringify({ detail: String(error instanceof Error ? error.message : error) }))
      }
    },
  }))
  const installed = new WeakSet<object>()
  // Weak keys: an Agent that disappears without a matching `agent/disposed`
  // event must not be kept alive by this registry. Disposal order is tracked by
  // the detach closures themselves, which hold no Agent reference.
  const attached = new WeakMap<object, () => Promise<void>>()
  const detachers: Array<() => Promise<void>> = []
  const install = (agent: Agent): void => {
    if (!agent || !ctx.agents.roots().includes(agent) || installed.has(agent)) return
    installed.add(agent)
    const detach = ctx.effect(
      () => agent.ctx.effect(() => installAgentTools(agent.ctx, agent, service)),
    )
    attached.set(agent, detach)
    detachers.push(detach)
  }
  ctx.on('agent/created', ({ agent }) => { install(agent) })
  ctx.on('agent/disposed', ({ agent }) => {
    const detach = attached.get(agent)
    if (detach === undefined) return
    attached.delete(agent)
    const index = detachers.indexOf(detach)
    if (index !== -1) detachers.splice(index, 1)
    void detach()
  })
  for (const agent of ctx.agents.list()) install(agent)
  ctx.effect(() => async () => {
    const pending = detachers.splice(0, detachers.length)
    await Promise.all(pending.map(release => release()))
  })
}

import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'voice-terminal-wake-tasks'
export const inject = ['agents']

const CALLBACK_ORIGIN = process.env.VOICE_DSH_CALLBACK_ORIGIN ?? ''
const TOKEN = process.env.VOICE_DSH_BRIDGE_TOKEN ?? ''
const MAX_REFERENCE_CHARS = 100_000

function responseToolOutput() {
  const nullableText = (description: string) => ({
    oneOf: [
      { type: 'string' as const },
      { type: 'null' as const },
    ] as const,
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
        repeat: nullableText('repeat（重复间隔）：固定间隔；空值表示一次性任务。'),
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
      },
      additionalProperties: true,
    },
    render: (_args: unknown, value: unknown) => [{ type: 'text' as const, text: JSON.stringify(value) }],
  }
}

async function voiceApi(path: string, method: string, body?: unknown): Promise<unknown> {
  if (!CALLBACK_ORIGIN.startsWith('http://127.0.0.1:') || !TOKEN) {
    throw new Error('Voice Server wake-task endpoint is unavailable')
  }
  const response = await fetch(`${CALLBACK_ORIGIN}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      Accept: 'application/json',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const payload: unknown = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = typeof payload === 'object' && payload !== null && 'detail' in payload
      ? String((payload as { detail: unknown }).detail) : `HTTP ${response.status}`
    throw new Error(detail)
  }
  return payload
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

function freezeCurrentContext(agent: any): string {
  const events = agent.session.snapshotEvents().slice(-240)
  const budget = { chars: MAX_REFERENCE_CHARS }
  const rows: Array<{ role: string; text: string }> = []
  for (const event of events) {
    if (!['user/message', 'assistant/message', 'tool/result'].includes(String(event?.type))) continue
    const data = event?.data
    const source = data?.message ?? data
    const role = event.type === 'user/message' ? 'user' : event.type === 'tool/result' ? 'tool' : 'assistant'
    const text = textFrom(source?.content ?? source, budget).trim()
    if (text) rows.push({ role, text })
    if (budget.chars <= 0) break
  }
  return JSON.stringify(rows)
}

function installAgentTools(agentCtx: any, agent: any): () => void {
  const dispose: Array<() => void> = []
  const ensureRoot = (exec: any): void => {
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
      repeat: { type: 'string', description: 'repeat（重复间隔）：可选固定间隔，如 1h、1d；省略表示一次性。' },
      text: { type: 'string', required: true, description: 'text（任务说明）：触发时传给目标 Agent 的简短方向。' },
    },
    output: responseToolOutput(),
    async execute(args: any, exec: any) {
      ensureRoot(exec)
      const reference = freezeCurrentContext(agent)
      return voiceApi('/api/internal/wake-tasks', 'POST', {
        target: args.target,
        when: args.when,
        ...(args.repeat ? { repeat: args.repeat } : {}),
        text: args.text,
        reference,
      })
    },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'cancel_wake',
    description: '取消一个待执行的项目级唤醒任务；task_id（任务标识）来自创建结果。',
    parameters: {
      task_id: { type: 'string', required: true, description: 'task_id（任务标识）：schedule_wake 返回的任务标识。' },
    },
    output: responseToolOutput(),
    async execute(args: any, exec: any) {
      ensureRoot(exec)
      return voiceApi(`/api/internal/wake-tasks/${encodeURIComponent(args.task_id)}/cancel`, 'POST')
    },
  })))
  dispose.push(agentCtx.tools.register(defineTool({
    name: 'read_wake_reference',
    description: '按需读取任务创建时冻结的 DSH 上下文参考；该参考与当前会话隔离且只读。',
    parameters: {
      task_id: { type: 'string', required: true, description: 'task_id（任务标识）：触发消息或创建结果中的任务标识。' },
    },
    output: responseToolOutput(),
    async execute(args: any, exec: any) {
      ensureRoot(exec)
      return voiceApi(`/api/internal/wake-tasks/${encodeURIComponent(args.task_id)}/reference`, 'GET')
    },
  })))
  return () => { for (const release of dispose.reverse()) release() }
}

export function apply(ctx: any): void {
  const installed = new WeakSet<object>()
  // Weak keys: an Agent that disappears without a matching `agent/disposed`
  // event must not be kept alive by this registry. Disposal order is tracked by
  // the detach closures themselves, which hold no Agent reference.
  const attached = new WeakMap<object, () => Promise<void>>()
  const detachers: Array<() => Promise<void>> = []
  const install = (agent: any): void => {
    if (!agent || !ctx.agents.roots().includes(agent) || installed.has(agent)) return
    installed.add(agent)
    const detach = ctx.effect(
      () => agent.ctx.effect(() => installAgentTools(agent.ctx, agent)),
    )
    attached.set(agent, detach)
    detachers.push(detach)
  }
  ctx.on('agent/created', ({ agent }: any) => install(agent))
  ctx.on('agent/disposed', ({ agent }: any) => {
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

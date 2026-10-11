/** Runtime details（运行详情）：当前 Voice 根代理及所属子代理的输出和权限。 */
import { randomUUID } from 'node:crypto'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { SessionId } from '@deepseek-ai/dsh-session'
import type { JobId, JobEvent, JobView } from '@deepseek-ai/dsh-jobs'
import type { Context } from '../shared/services.js'
import type { Binding } from './contracts.js'
import { record } from '../shared/json-record.js'
import { redactSensitive } from '../shared/protocol.js'
import { sendRuntimeActivity } from './events.js'

/** ownedBinding（运行归属）：逐层验证实时所有权，排除其他独立会话和会话分叉。 */
function ownedBinding(ctx: Context, bindings: Map<string, Binding>, agent: Agent): Binding | undefined {
  if (!ctx.agents.list().includes(agent)) return undefined
  const seen = new Set<string>()
  let current: Agent | undefined = agent
  while (current && !seen.has(String(current.id))) {
    seen.add(String(current.id))
    const binding = bindings.get(String(current.id))
    if (binding?.state === 'active' && binding.handle?.agent === current) return binding
    const parentId: SessionId | undefined = current.session.header.parentSession
    const parent: Agent | undefined = ctx.agents.list().find(value => value.id === parentId)
    if (!parent || !ctx.agents.isOwnedBy(current.id, parent)) return undefined
    current = parent
  }
  return undefined
}

function display(value: unknown): string {
  return redactSensitive(typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)).slice(-12000)
}

export function registerExtendedActivity(ctx: Context, bindings: Map<string, Binding>): void {
  ctx.on('session/event', (session, event) => {
    const agent = ctx.agents.list().find(value => value.session === session)
    const binding = agent && ownedBinding(ctx, bindings, agent)
    if (!binding || !agent || binding.handle?.agent === agent) return
    const data = record(event.data)
    const message = record(data.message)
    const first = record(Array.isArray(message.content) ? message.content[0] : undefined)
    const callId = String(data.callId ?? first.toolCallId ?? record(message.source).callId ?? '')
    const key = `${agent.id}:${callId}`
    if (event.type === 'tool/call') {
      if (!binding.activationId) return
      const name = display(data.name ?? '子代理工具').slice(0, 160)
      binding.pendingTools.set(key, name)
      binding.toolArguments ??= new Map()
      binding.toolArguments.set(key, display(data.arguments))
      if (binding.toolArguments.size > 96) binding.toolArguments.delete(binding.toolArguments.keys().next().value!)
      binding.pendingToolActivations.set(key, binding.activationId)
      sendRuntimeActivity(binding, binding.activationId, { stream: 'tool', phase: 'started', item_id: `child-${key}`.slice(0, 160), tool_name: `子代理 · ${name}`.slice(0, 160), tool_status: 'running', text: display(data.arguments) })
    } else if (event.type === 'tool/result') {
      const activationId = binding.pendingToolActivations.get(key)
      if (!activationId) return
      const failed = Boolean(data.error) || message.isError === true || first.isError === true
      sendRuntimeActivity(binding, activationId, { stream: 'tool', phase: failed ? 'failed' : 'completed', item_id: `child-${key}`.slice(0, 160), tool_name: `子代理 · ${binding.pendingTools.get(key) ?? '工具'}`.slice(0, 160), tool_status: failed ? 'failed' : 'completed', text: display(`参数：\n${binding.toolArguments?.get(key) ?? ''}\n\n结果：\n${display(message.content ?? data)}`) })
      binding.pendingTools.delete(key)
      binding.pendingToolActivations.delete(key)
      binding.toolArguments?.delete(key)
    }
  })

  // StreamState（流状态）：各代理独立校验修订、尝试和激活，拒收迟到帧。
  const streams = new WeakMap<Agent, { revision: number; attemptId: string; activationId: string; items: Map<string, 'thinking' | 'output'> }>()
  const finishStream = (agent: Agent, binding: Binding) => {
    const state = streams.get(agent)
    if (!state) return
    for (const [itemId, stream] of state.items) {
      if (stream === 'thinking') sendRuntimeActivity(binding, state.activationId, { stream, phase: 'completed', item_id: itemId })
      else sendRuntimeActivity(binding, state.activationId, { stream, phase: 'completed', item_id: itemId, tool_name: binding.handle?.agent === agent ? 'DSH 回复' : '子代理回复', tool_status: 'completed' })
    }
    state.items.clear()
    state.attemptId = ''
  }
  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    const binding = ownedBinding(ctx, bindings, agent)
    if (!binding) return
    const previous = streams.get(agent)
    if (!Number.isSafeInteger(frame.revision) || frame.revision <= (previous?.revision ?? 0)) return
    if (previous) previous.revision = frame.revision
    if (frame.type === 'start') {
      finishStream(agent, binding)
      if (binding.activationId) streams.set(agent, { revision: frame.revision, attemptId: String(frame.attemptId), activationId: binding.activationId, items: new Map() })
      return
    }
    if (!previous || previous.attemptId !== String(frame.attemptId)
      || (binding.activationId ?? binding.terminalActivationId) !== previous.activationId) return
    if (frame.type === 'end') { finishStream(agent, binding); return }
    if (frame.type !== 'chunk') return
    const chunk = frame.chunk
    if (chunk.type !== 'text-delta' && chunk.type !== 'reasoning-delta') return
    const child = binding.handle?.agent !== agent
    if (!child && chunk.type === 'reasoning-delta') return
    const text = display(chunk.text)
    const itemId = `${chunk.type === 'reasoning-delta' ? 'child-thinking' : 'message'}-${agent.id}-${frame.attemptId}-${chunk.index}`.slice(0, 160)
    previous.items.set(itemId, chunk.type === 'reasoning-delta' ? 'thinking' : 'output')
    for (let offset = 0; offset < text.length; offset += 1800) {
      if (chunk.type === 'reasoning-delta') sendRuntimeActivity(binding, previous.activationId, { stream: 'thinking', phase: 'delta', item_id: itemId, delta: text.slice(offset, offset + 1800) })
      else sendRuntimeActivity(binding, previous.activationId, { stream: 'output', phase: 'delta', item_id: itemId, tool_name: child ? '子代理回复' : 'DSH 回复', tool_status: 'running', delta: text.slice(offset, offset + 1800) })
    }
  })
  ctx.on('agent/status', ({ agent, status }) => {
    const binding = ownedBinding(ctx, bindings, agent)
    if (binding && status === 'idle') finishStream(agent, binding)
  })

  ctx.on('approval/request', (request, next) => {
    const binding = ownedBinding(ctx, bindings, request.agent)
    const activationId = binding?.activationId
    if (!binding || !activationId) return next()
    if (request.signal?.aborted) return Promise.resolve('cancelled' as const)
    const approvalId = randomUUID() // approvalId（本次待决权限标识）：按钮只能决定该请求。
    const callId = String(request.callId ?? '')
    const key = binding.handle?.agent === request.agent ? callId : `${request.agent.id}:${callId}`
    const text = display([request.displayReason?.['zh-CN'] ?? request.reason ?? '此操作需要授权', binding.toolArguments?.get(key) ?? '', `工具：${request.toolName}`].filter(Boolean).join('\n\n'))
    binding.pendingApprovals ??= new Map()
    return new Promise<'allowed-once' | 'rejected' | 'cancelled'>(resolve => {
      const settle = (outcome: 'allowed-once' | 'rejected' | 'cancelled') => {
        if (!binding.pendingApprovals?.delete(approvalId)) return
        request.signal?.removeEventListener('abort', abort)
        const result = outcome === 'allowed-once' ? '已同意本次操作' : outcome === 'rejected' ? '已拒绝本次操作' : '权限请求已取消'
        sendRuntimeActivity(binding, activationId, { stream: 'approval', phase: outcome === 'allowed-once' ? 'completed' : 'failed', item_id: `approval-${approvalId}`, approval_id: approvalId, tool_name: display(request.toolName).slice(0, 160), text: `${text}\n\n${result}`.slice(-12000) })
        resolve(outcome)
      }
      const abort = () => settle('cancelled')
      binding.pendingApprovals!.set(approvalId, { activationId, settle })
      request.signal?.addEventListener('abort', abort, { once: true })
      sendRuntimeActivity(binding, activationId, { stream: 'approval', phase: 'started', item_id: `approval-${approvalId}`, approval_id: approvalId, tool_name: display(request.toolName).slice(0, 160), text })
      if (request.signal?.aborted) abort()
    })
  }, { prepend: true })

  const offsets = new Map<JobId, number>() // offsets（作业输出游标）：独立于模型消费游标。
  const owners = new Map<JobId, { binding: Binding; activationId: string; job: JobView }>()
  const receiveJob = (event: JobEvent) => {
    const job = event.type === 'output' ? undefined : event.job
    const id = event.type === 'output' ? event.id : event.job.id
    // 首次注册固定归属；进度和结束事件不得把旧作业重新绑定到新的运行。
    if (event.type === 'registered' && job?.owner && !owners.has(id)) {
      const agent = ctx.agents.list().find(value => value.id === job.owner)
      const binding = agent && ownedBinding(ctx, bindings, agent)
      if (binding?.activationId) owners.set(id, { binding, activationId: binding.activationId, job })
    }
    const owned = owners.get(id)
    if (!owned || owned.binding.state !== 'active' || (owned.binding.activationId ?? owned.binding.terminalActivationId) !== owned.activationId) { owners.delete(id); offsets.delete(id); return }
    if (job) owned.job = job
    const current = owned.job
    const itemId = `job-${id}`.slice(0, 160)
    const toolName = display(current.label).slice(0, 160)
    if (event.type === 'registered') sendRuntimeActivity(owned.binding, owned.activationId, { stream: 'output', phase: 'started', item_id: itemId, tool_name: toolName, tool_status: 'running', text: `${display(current.label)}\n` })
    try {
      const output = ctx.jobs.readAt(id, offsets.get(id) ?? 0, current.owner)
      offsets.set(id, output.next)
      const text = `${output.lossy ? '\n[较早输出已截断]\n' : ''}${output.chunks.map(chunk => `${chunk.channel === 'stderr' ? '[错误输出] ' : ''}${chunk.gapBefore ? '[输出中断]\n' : ''}${chunk.text}`).join('')}`
      const safe = display(text)
      for (let offset = 0; offset < safe.length; offset += 1800) sendRuntimeActivity(owned.binding, owned.activationId, { stream: 'output', phase: 'delta', item_id: itemId, tool_name: toolName, tool_status: 'running', delta: safe.slice(offset, offset + 1800) })
    } catch { /* 作业已回收或所有权失效；不再读取输出。 */ }
    if (event.type === 'progress' || event.type === 'stopping' || current.detail) {
      const detail = display(current.detail ?? current.progress ?? '正在停止')
      sendRuntimeActivity(owned.binding, owned.activationId, { stream: 'output', phase: 'delta', item_id: itemId, tool_name: toolName, tool_status: 'running', delta: `\n${detail}\n`.slice(-2000) })
    }
    if (event.type === 'settled' || event.type === 'removed') {
      const failed = current.status === 'failed' || current.status === 'killed'
      sendRuntimeActivity(owned.binding, owned.activationId, { stream: 'output', phase: failed ? 'failed' : 'completed', item_id: itemId, tool_name: toolName, tool_status: failed ? 'failed' : 'completed' })
      owners.delete(id); offsets.delete(id)
    }
  }
  ctx.effect(() => {
    const dispose = ctx.jobs.events.subscribe({ owners: 'all' }, receiveJob)
    return () => { dispose(); owners.clear(); offsets.clear(); for (const binding of bindings.values()) for (const pending of binding.pendingApprovals?.values() ?? []) pending.settle('cancelled') }
  })
}

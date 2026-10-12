import { record } from '../shared/json-record.js'
import type { Context } from '../shared/services.js'
import type { AssistantStreamFrame } from '@deepseek-ai/dsh-agent'
/** Event component（事件组件）：观察已提交事件并投影根 Agent 的实时思考与工具动作。 */
import { randomUUID } from 'node:crypto'
import { digest, isDelivered, redactSensitive } from '../shared/protocol.js'
import type { Json } from '../shared/protocol.js'
import { MAX_OBSERVER_RECORDS } from '../shared/identity.js'
import { bindingCallback } from './callbacks.js'
import { clearActivationIfCurrent, createBridgeUserMessage } from './messages.js'
import { armRuntimeReminder, reassertContextAfterCompaction, stopRuntimeReminder } from './context.js'
import { observerData, readSessionTelemetry } from './observer.js'
import type { Binding, RuntimeActivity } from './contracts.js'

const THINKING_FLUSH_MS = 60 // thinking flush interval（思考增量合并间隔）。
const THINKING_DELTA_LIMIT = 1800 // delta limit（单次增量上限）：低于服务端协议限制并保留编码余量。

function queueCallback(
  binding: Binding,
  path: string,
  payload: Record<string, Json>,
  activationId?: string,
): Promise<Record<string, Json>> {
  const request = binding.actionQueue.catch(() => undefined)
    .then(async () => {
      if (binding.state !== 'active' || binding.callbacks?.signal.aborted) return {} as Record<string, Json>
      if (activationId && binding.activationId !== activationId
        && binding.terminalActivationId !== activationId) return {} as Record<string, Json>
      const attempts = payload.run_origin === 'schedule' ? 3 : 1
      for (let attempt = 0; attempt < attempts; attempt += 1) {
        try {
          const response = await bindingCallback(binding, path, payload, AbortSignal.timeout(10000))
          if (payload.run_origin === 'schedule' && !isDelivered(response)) throw new Error('scheduled activation rejected（定时激活未获确认）')
          return response
        } catch (error) {
          if (attempt + 1 === attempts || binding.callbacks?.signal.aborted
            || binding.state !== 'active' || binding.activationId !== activationId) throw error
          await new Promise(resolve => setTimeout(resolve, 250))
        }
      }
      throw new Error('scheduled activation unavailable（定时激活不可用）')
    })
  binding.actionQueue = request.then(() => undefined, () => undefined)
  return request
}

function queueActivityCallback(binding: Binding, activationId: string, payload: Record<string, Json>): Promise<Record<string, Json>> {
  const actionSeq = ++binding.actionSeq
  return queueCallback(binding, '/api/internal/dsh-bridge/shadow.activity', {
    request_id: randomUUID(), voice_session_id: binding.voiceSessionId,
    dsh_session_id: binding.dshSessionId, epoch: binding.epoch,
    context_revision: binding.revision, activation_id: activationId,
    action_seq: actionSeq, ...payload,
  }, activationId || undefined)
}

export function sendRuntimeActivity(binding: Binding, activationId: string, activity: RuntimeActivity): void {
  if (binding.state !== 'active' || !activationId
    || (binding.activationId !== activationId && binding.terminalActivationId !== activationId)) return
  void queueActivityCallback(binding, activationId, { activity }).catch(() => undefined)
}

function flushThinking(binding: Binding): void {
  if (binding.thinkingTimer) clearTimeout(binding.thinkingTimer)
  binding.thinkingTimer = undefined
  const delta = binding.pendingThinkingDelta
  binding.pendingThinkingDelta = ''
  if (!delta || !binding.currentThinkingItemId || !binding.currentThinkingActivationId) return
  const characters = Array.from(delta)
  for (let offset = 0; offset < characters.length; offset += THINKING_DELTA_LIMIT) {
    sendRuntimeActivity(binding, binding.currentThinkingActivationId, {
      stream: 'thinking', phase: 'delta', item_id: binding.currentThinkingItemId,
      delta: characters.slice(offset, offset + THINKING_DELTA_LIMIT).join(''),
    })
  }
}

function clearCurrentThinking(binding: Binding): void {
  if (binding.currentThinkingAttemptId) finishThinking(binding, binding.currentThinkingAttemptId)
  else flushThinking(binding)
  binding.currentThinkingItemId = undefined
  binding.currentThinkingAttemptId = undefined
  binding.currentThinkingActivationId = undefined
}

function finishThinking(binding: Binding, attemptId: string): void {
  if (binding.currentThinkingAttemptId === attemptId) {
    flushThinking(binding)
    if (binding.currentThinkingItemId && binding.currentThinkingActivationId) {
      sendRuntimeActivity(binding, binding.currentThinkingActivationId, {
        stream: 'thinking', phase: 'completed', item_id: binding.currentThinkingItemId,
      })
    }
    binding.currentThinkingItemId = undefined
    binding.currentThinkingAttemptId = undefined
    binding.currentThinkingActivationId = undefined
  }
}

/**
 * Stop buffered UI activity（停止缓冲界面活动），供激活替换、取消及组件卸载使用。
 * @param binding - Bound root Agent（已绑定根代理）。
 */
export function stopRuntimeActivity(binding: Binding): void {
  for (const pending of binding.pendingApprovals?.values() ?? []) pending.settle('cancelled')
  binding.pendingApprovals?.clear()
  binding.toolArguments?.clear()
  if (binding.thinkingTimer) clearTimeout(binding.thinkingTimer)
  binding.thinkingTimer = undefined
  binding.pendingThinkingDelta = ''
  binding.currentThinkingItemId = undefined
  binding.currentThinkingAttemptId = undefined
  binding.currentThinkingActivationId = undefined
}

function onAssistantStream(binding: Binding, frame: AssistantStreamFrame): void {
  const activationId = binding.activationId
  if (binding.state !== 'active' || !activationId) return
  if (!Number.isSafeInteger(frame.revision) || frame.revision <= binding.assistantStreamRevision) return
  binding.assistantStreamRevision = frame.revision
  if (frame.type === 'start') {
    clearCurrentThinking(binding)
    binding.currentThinkingAttemptId = String(frame.attemptId)
    binding.currentThinkingActivationId = activationId
    return
  }
  if (binding.currentThinkingAttemptId !== String(frame.attemptId)
    || binding.currentThinkingActivationId !== activationId) return
  if (frame.type === 'chunk' && frame.chunk?.type === 'reasoning-delta') {
    const text = typeof frame.chunk.text === 'string' ? redactSensitive(frame.chunk.text) : ''
    if (!text) return
    const attemptId = String(frame.attemptId)
    const itemId = `thinking-${attemptId}-${frame.chunk.index}`.slice(0, 160)
    if (binding.currentThinkingItemId !== itemId
      || binding.currentThinkingActivationId !== activationId) {
      clearCurrentThinking(binding)
      binding.currentThinkingItemId = itemId
      binding.currentThinkingAttemptId = attemptId
      binding.currentThinkingActivationId = activationId
    }
    binding.pendingThinkingDelta += text
    if (binding.pendingThinkingDelta.length >= THINKING_DELTA_LIMIT) flushThinking(binding)
    else if (!binding.thinkingTimer) {
      binding.thinkingTimer = setTimeout(() => flushThinking(binding), THINKING_FLUSH_MS)
    }
    return
  }
  if (frame.type === 'end') finishThinking(binding, String(frame.attemptId))
}

export function registerSessionEvents(ctx: Context, bindings: Map<string, Binding>): void {
  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    const binding = bindings.get(String(agent?.id ?? ''))
    if (!binding || binding.handle?.agent !== agent) return
    onAssistantStream(binding, frame)
  })

  ctx.on('session/event', (session, event) => {
    const binding = bindings.get(String(session.id))
    if (!binding || binding.state !== 'active' || binding.handle?.agent.session !== session) return
    const type = event.type
    const data = record(event.data)
    const message = record(data.message)
    const first = record(Array.isArray(message.content) ? message.content[0] : undefined)
    if (!['assistant/message', 'tool/call', 'tool/result', 'step/end', 'turn/start', 'turn/end', 'compaction/start', 'compaction/end'].includes(type)) return
    binding.traceRevision += 1
    binding.records.push({ trace_revision: binding.traceRevision, type, data: observerData(event, binding) })
    if (binding.records.length > MAX_OBSERVER_RECORDS) binding.records.splice(0, binding.records.length - MAX_OBSERVER_RECORDS)
    if (type === 'compaction/end' && !data.error) reassertContextAfterCompaction(binding)
    if (type === 'tool/call') {
      clearCurrentThinking(binding)
      const callId = String(data.callId ?? binding.traceRevision).slice(0, 120)
      const name = redactSensitive(String(data.name ?? '工具调用')).slice(0, 160)
      const activationId = binding.activationId
      const argumentsText = redactSensitive(JSON.stringify(data.arguments ?? {}, null, 2)).slice(0, 12000)
      binding.toolArguments ??= new Map()
      binding.toolArguments.set(callId, argumentsText)
      if (binding.toolArguments.size > 96) binding.toolArguments.delete(binding.toolArguments.keys().next().value!)
      binding.pendingTools.set(callId, name)
      if (activationId) {
        binding.pendingToolActivations.set(callId, activationId)
        sendRuntimeActivity(binding, activationId, {
          stream: 'tool', phase: 'started', item_id: `tool-${callId}`.slice(0, 160),
          tool_name: name, tool_status: 'running', text: argumentsText,
        })
      }
    }
    if (type === 'tool/result') {
      const callId = String(first.toolCallId ?? record(message.source).callId ?? '').slice(0, 120)
      const name = binding.pendingTools.get(callId)
      const activationId = binding.pendingToolActivations.get(callId)
      const failed = Boolean(data.error) || message.isError === true || first.isError === true
      if (name && activationId) {
        const settlement = failed
          ? { phase: 'failed', tool_status: 'failed' } as const
          : { phase: 'completed', tool_status: 'completed' } as const
        sendRuntimeActivity(binding, activationId, {
          stream: 'tool', ...settlement, item_id: `tool-${callId}`.slice(0, 160),
          tool_name: name,
          text: redactSensitive(`参数：\n${binding.toolArguments?.get(callId) ?? ''}\n\n结果：\n${JSON.stringify(message.content ?? data, null, 2)}`).slice(-12000),
        })
      }
      binding.pendingTools.delete(callId)
      binding.pendingToolActivations.delete(callId)
      binding.toolArguments?.delete(callId)
    }
    if (type === 'step/end') {
      binding.committedSteps += 1
      void queueActivityCallback(binding, binding.activationId ?? binding.terminalActivationId ?? '', {
        trace_revision: binding.traceRevision, committed_steps: binding.committedSteps,
        telemetry: readSessionTelemetry(binding, ctx.get('tokenMeter')),
        state: binding.agentState,
      }).catch(() => undefined)
    }
    if (type === 'turn/end' && binding.activationId) {
      if (!binding.protocolRecoveryAttempted) {
        binding.protocolRecoveryAttempted = true
        // turn/end is published while Session.append() still rejects reentrant
        // durable inbox writes.  Arm the recovery here, then enqueue it from the
        // following running→idle status edge (the public Agent safe boundary).
        binding.protocolRecoveryPending = true
      } else {
        const activationId = binding.activationId
        void (async () => {
          const response = await bindingCallback(binding, '/api/internal/dsh-bridge/shadow.event', {
            request_id: digest({ scope: 'protocol-recovery-request', dsh_session_id: binding.dshSessionId, activation_id: activationId }),
            event_id: digest({ scope: 'protocol-recovery-event', dsh_session_id: binding.dshSessionId, activation_id: activationId }),
            voice_session_id: binding.voiceSessionId, dsh_session_id: binding.dshSessionId,
            epoch: binding.epoch, context_revision: binding.revision,
            activation_id: activationId,
            event: {
              type: 'failed',
              message: 'DSH Shadow protocol failed to settle after one recovery turn.',
              code: 'missing_terminal_tool',
              infrastructure: true,
            },
          })
          if (isDelivered(response) && clearActivationIfCurrent(binding, activationId)) {
            stopRuntimeReminder(binding, true)
          }
        })().catch(() => undefined)
      }
    }
  })

  ctx.on('agent/inbox/claimed', ({ agent, message }) => {
    const binding = bindings.get(String(agent?.id ?? ''))
    if (!binding || binding.state !== 'active' || binding.handle?.agent !== agent) return
    // 官方定时任务和项目提醒在空闲根代理领取后建立独立运行，沿用当前会话身份。
    if (!binding.activationId && message.source?.kind === 'schedule') {
      stopRuntimeActivity(binding)
      binding.activationId = randomUUID()
      binding.activationOrigin = 'schedule'
      binding.terminalActivationId = undefined
      binding.actionSeq = 0
      binding.pendingTools.clear()
      binding.pendingToolActivations.clear()
      binding.protocolRecoveryAttempted = false
      binding.protocolRecoveryPending = false
      armRuntimeReminder(binding)
      binding.activationReady = queueActivityCallback(binding, binding.activationId, {
        run_origin: 'schedule', state: 'running',
      })
      void binding.activationReady.catch(() => undefined) // 拒绝结果保留给控制工具读取。
    }
    const revision = binding?.queuedMessages?.get(String(message?.id ?? ''))
    if (revision === undefined) return
    binding.queuedMessages?.delete(String(message.id))
    binding.claimedRevision = Math.max(binding.claimedRevision ?? 0, revision)
    if (binding.gateObserved) console.info('[voice-shadow-bridge] context gate', {
      action: 'claimed', activation_id: binding.activationId ?? '',
      context_revision: binding.claimedRevision, message_id: message.id,
    })
    if (binding.gateObserved && binding.activationId) {
      void queueActivityCallback(binding, binding.activationId, {
        claimed_revision: binding.claimedRevision, state: binding.agentState,
      }).catch(() => undefined)
    }
  })

  ctx.on('agent/status', ({ agent, status }) => {
    const binding = bindings.get(String(agent?.id ?? ''))
    if (!binding || binding.state !== 'active' || binding.handle?.agent !== agent) return
    binding.traceRevision += 1
    binding.agentState = String(status ?? '')
    binding.records.push({ trace_revision: binding.traceRevision, type: 'agent/status', data: { state: String(status ?? '') } })
    if (status === 'idle') {
      if (binding.currentThinkingAttemptId) finishThinking(binding, binding.currentThinkingAttemptId)
      else flushThinking(binding)
    }
    const protocolRecovery = status === 'idle' && binding.protocolRecoveryPending && Boolean(binding.activationId)
    if (protocolRecovery) {
      binding.protocolRecoveryPending = false
      binding.handle!.agent.followup(createBridgeUserMessage([
        { type: 'text', text: 'PROTOCOL RECOVERY: This activation ended without a Voice Bridge terminal tool. Re-evaluate the latest authoritative context and end with exactly one of request_user_input, return_shadow_result, or finish_shadow_no_action.' },
      ]))
    }
    const callbackActivationId = binding.activationId ?? binding.terminalActivationId ?? ''
    void queueActivityCallback(binding, callbackActivationId, {
      trace_revision: binding.traceRevision, claimed_revision: binding.claimedRevision ?? 0,
      telemetry: readSessionTelemetry(binding, ctx.get('tokenMeter')),
      state: protocolRecovery ? 'running' : String(status ?? ''),
    }).catch(() => undefined)
    void bindingCallback(binding, '/api/internal/dsh-bridge/mainline.activity', {
      dsh_session_id: binding.dshSessionId, epoch: binding.epoch,
      state: String(status ?? ''),
    }).catch(() => undefined)
  })
}

import { MessageId } from '@deepseek-ai/dsh-llm'
import type { UserMessage } from '@deepseek-ai/dsh-llm'
import { record } from '../shared/json-record.js'
/** Context component（上下文组件）：安全点合并、门禁暂缓、压缩恢复与运行提醒。 */
import { randomUUID } from 'node:crypto'
import { BRIDGE_PROTOCOL_VERSION } from '../shared/protocol.js'
import type { Json, Envelope } from '../shared/protocol.js'
import { BRIDGE_NAME as name, MAX_OBSERVER_RECORDS } from '../shared/identity.js'
import { bindingCallback } from './callbacks.js'
import { createBridgeUserMessage } from './messages.js'
import type { Binding, PendingContext } from './contracts.js'
function publicContextMessage(body: Envelope, update: 'snapshot' | 'delta'): string {
  const frames = Array.isArray(body.frames) ? body.frames : []
  return [
    'VOICE AUTHORITATIVE CONTEXT',
    `Voice Session: ${body.voice_session_id.slice(0, 12)}`,
    `Epoch: ${body.epoch}`,
    `Revision: ${body.context_revision ?? 0}`,
    `Update: ${update}`,
    'Source: Voice Terminal',
    '',
    JSON.stringify(frames),
  ].join('\n')
}

function contextMessage(binding: Binding, pending: PendingContext): UserMessage {
  const prefix = pending.activationId ? [
    'VOICE SHADOW ACTIVATION',
    `Activation: ${pending.activationId}`,
    `Revision: ${pending.revision}`,
    'Judge and execute the current task. End only with a Voice Bridge terminal tool.',
    '',
    `VOICE GATE DEFERRED CONTEXT: revisions through ${pending.deferredThrough ?? pending.revision} were delayed to this next turn.`,
    '',
  ].join('\n') : ''
  return createBridgeUserMessage([{
    type: 'text',
    text: prefix + publicContextMessage({
      protocol_version: BRIDGE_PROTOCOL_VERSION,
      request_id: 'merged-context',
      voice_session_id: binding.voiceSessionId,
      dsh_session_id: binding.dshSessionId,
      epoch: binding.epoch,
      context_revision: pending.revision,
      from_revision: pending.fromRevision,
      frames: pending.frames,
    }, pending.update),
  }], pending.messageId)
}

export function enqueueAuthoritativeContext(
  binding: Binding,
  body: Envelope,
  update: 'snapshot' | 'delta',
  preferredMessageId?: string,
): 'inserted' | 'replaced' | 'already_claimed' {
  const revision = Number(body.context_revision)
  const fromRevision = update === 'snapshot' ? 0 : Number(body.from_revision)
  const frames = (Array.isArray(body.frames) ? body.frames : []) as Json[]
  const prior = binding.pendingContext
  if (prior) {
    const merged: PendingContext = update === 'snapshot'
      ? { messageId: prior.messageId, update, fromRevision: 0, revision, frames,
          activationId: prior.activationId, deferredThrough: prior.deferredThrough }
      : {
          messageId: prior.messageId,
          update: prior.update,
          fromRevision: prior.fromRevision,
          revision,
          frames: [...prior.frames, ...frames],
          activationId: prior.activationId,
          deferredThrough: prior.deferredThrough,
        }
    if (binding.handle!.agent.inbox.replace(MessageId(prior.messageId), contextMessage(binding, merged))) {
      binding.pendingContext = merged
      binding.queuedRevision = revision
      binding.queuedMessages?.set(merged.messageId, revision)
      console.info('[voice-shadow-bridge] context merge', {
        action: 'replaced', from_revision: merged.fromRevision,
        context_revision: revision, frame_count: merged.frames.length,
        target: merged.activationId ? 'next_turn' : 'next_step',
        message_id: merged.messageId,
      })
      return 'replaced'
    }
  }
  const pending: PendingContext = {
    messageId: preferredMessageId ?? randomUUID(), update, fromRevision, revision, frames,
  }
  binding.pendingContext = pending
  binding.queuedRevision = revision
  binding.queuedMessages?.set(pending.messageId, revision)
  binding.handle!.agent.inject(contextMessage(binding, pending))
  const action = prior ? 'already_claimed' : 'inserted'
  console.info('[voice-shadow-bridge] context merge', {
    action, from_revision: fromRevision, context_revision: revision,
    frame_count: frames.length, target: 'next_step', message_id: pending.messageId,
  })
  return action
}

export function holdAuthoritativeContext(binding: Binding, body: Envelope, update: 'snapshot' | 'delta'): void {
  binding.gateObserved = true
  const revision = Number(body.context_revision)
  const frames = (Array.isArray(body.frames) ? body.frames : []) as Json[]
  const prior = binding.heldContext
  binding.heldContext = update === 'snapshot' || !prior
    ? { messageId: prior?.messageId ?? randomUUID(), update, fromRevision: update === 'snapshot' ? 0 : Number(body.from_revision), revision, frames }
    : { ...prior, revision, frames: [...prior.frames, ...frames] }
  console.info('[voice-shadow-bridge] context gate', {
    action: 'held', activation_id: binding.activationId ?? '',
    from_revision: binding.heldContext.fromRevision, context_revision: revision,
    frame_count: binding.heldContext.frames.length,
  })
}

export function releaseHeldContext(binding: Binding, revision: number): 'inserted' | 'replaced' | 'already_claimed' | 'empty' {
  const held = binding.heldContext
  if (!held || revision > held.revision || revision <= held.fromRevision) return 'empty'
  const released = held.frames.filter(frame => Number(record(frame).revision ?? 0) <= revision)
  const remaining = held.frames.filter(frame => Number(record(frame).revision ?? 0) > revision)
  const remainder: PendingContext | undefined = held.revision > revision
    ? { messageId: held.messageId, update: 'delta', fromRevision: revision,
        revision: held.revision, frames: remaining }
    : undefined
  const action = enqueueAuthoritativeContext(binding, {
    protocol_version: BRIDGE_PROTOCOL_VERSION, request_id: held.messageId,
    voice_session_id: binding.voiceSessionId, dsh_session_id: binding.dshSessionId,
    epoch: binding.epoch, context_revision: revision,
    from_revision: held.fromRevision, frames: released,
  }, held.update, held.messageId)
  binding.heldContext = remainder
  if ((binding.deferredRevision ?? 0) <= revision) binding.deferredRevision = undefined
  return action
}

export function enqueueDeferredActivation(binding: Binding, activationId: string): boolean {
  const held = binding.heldContext
  if (!held) return false
  const delivered = binding.pendingContext?.messageId === held.messageId
    && binding.pendingContext.activationId === activationId
    && ((binding.handle!.agent.inbox.nextTurn ?? []).some(item => item.id === held.messageId)
      || (binding.claimedRevision ?? 0) >= held.revision)
  if (delivered) {
    binding.heldContext = undefined
    binding.deferredRevision = undefined
    return true
  }
  const pending: PendingContext = {
    ...held, messageId: held.messageId, activationId,
    deferredThrough: held.revision,
  }
  const previousPending = binding.pendingContext
  const previousQueued = binding.queuedRevision
  binding.pendingContext = pending
  binding.queuedRevision = pending.revision
  binding.queuedMessages?.set(pending.messageId, pending.revision)
  try {
    binding.handle!.agent.followup(contextMessage(binding, pending))
  } catch (error) {
    if (!(binding.handle!.agent.inbox.nextTurn ?? []).some(item => item.id === pending.messageId)) {
      binding.pendingContext = previousPending
      binding.queuedRevision = previousQueued
      binding.queuedMessages?.delete(pending.messageId)
      throw error
    }
  }
  binding.heldContext = undefined
  binding.deferredRevision = undefined
  console.info('[voice-shadow-bridge] context gate', {
    action: 'next_turn', activation_id: activationId,
    from_revision: pending.fromRevision, context_revision: pending.revision,
    message_id: pending.messageId,
  })
  return true
}

function runtimeReminderMessage(binding: Binding, messageId: string, elapsedMs: number): UserMessage {
  return createBridgeUserMessage([{
    type: 'text',
    text: [
      'VOICE SHADOW RUNTIME ADVISORY',
      `Activation: ${binding.activationId ?? ''}`,
      `Elapsed: ${(elapsedMs / 1000).toFixed(1)} seconds`,
      '',
      '【运行时限提醒】本次 Shadow activation 已运行较长时间。请立即收敛已有信息，停止低收益思考、重复搜索和非必要工具调用，并尽快输出可用结果。',
    ].join('\n'),
  }], messageId)
}

export function deliverRuntimeReminder(
  binding: Binding,
  nowMs = Date.now(),
): false | 'inserted' | 'replaced' | 'already_claimed' {
  if (
    binding.state !== 'active' || !binding.activationId
    || binding.activationStartedAt === undefined
  ) return false
  const elapsedMs = Math.max(0, Math.round(nowMs - binding.activationStartedAt))
  if (elapsedMs < binding.nextReminderMs) return false
  const crossedIntervals = Math.max(1, Math.floor(elapsedMs / binding.reminderIntervalMs))
  binding.nextReminderMs = (crossedIntervals + 1) * binding.reminderIntervalMs
  const prior = binding.pendingReminder
  if (prior) {
    const replacement = runtimeReminderMessage(binding, prior.messageId, elapsedMs)
    if (binding.handle!.agent.inbox.replace(MessageId(prior.messageId), replacement)) {
      recordRuntimeReminder(binding, 'replaced', elapsedMs, crossedIntervals)
      return 'replaced'
    }
  }
  const pending = { messageId: randomUUID() }
  binding.handle!.agent.inject(runtimeReminderMessage(binding, pending.messageId, elapsedMs))
  binding.pendingReminder = pending
  const action = prior ? 'already_claimed' : 'inserted'
  recordRuntimeReminder(binding, action, elapsedMs, crossedIntervals)
  return action
}

function recordRuntimeReminder(
  binding: Binding,
  action: 'inserted' | 'replaced' | 'already_claimed',
  elapsedMs: number,
  crossedIntervals: number,
): void {
  binding.traceRevision += 1
  binding.records.push({
    trace_revision: binding.traceRevision,
    type: 'voice/runtime-reminder',
    data: { elapsed_ms: elapsedMs, crossed_intervals: crossedIntervals, action },
  })
  if (binding.records.length > MAX_OBSERVER_RECORDS) binding.records.splice(0, binding.records.length - MAX_OBSERVER_RECORDS)
  console.info('[voice-shadow-bridge] runtime reminder', {
    activation_id: binding.activationId, elapsed_ms: elapsedMs,
    crossed_intervals: crossedIntervals, action,
  })
}

export function stopRuntimeReminder(binding: Binding, discardPending = false): void {
  if (binding.reminderTimer !== undefined) clearTimeout(binding.reminderTimer)
  binding.reminderTimer = undefined
  binding.activationStartedAt = undefined
  binding.nextReminderMs = binding.reminderIntervalMs
  if (!discardPending || !binding.pendingReminder) return
  const messageId = binding.pendingReminder.messageId
  binding.pendingReminder = undefined
  queueMicrotask(() => {
    try { binding.handle?.agent?.inbox?.remove(MessageId(messageId)) } catch { /* session may be closing */ }
  })
}

function scheduleRuntimeReminder(binding: Binding, activationId: string): void {
  if (
    binding.state !== 'active' || binding.activationId !== activationId
    || binding.activationStartedAt === undefined
  ) return
  const dueAt = binding.activationStartedAt + binding.nextReminderMs
  const delayMs = Math.max(0, dueAt - Date.now())
  binding.reminderTimer = setTimeout(() => {
    binding.reminderTimer = undefined
    if (
      binding.state !== 'active' || binding.activationId !== activationId
    ) return
    deliverRuntimeReminder(binding)
    scheduleRuntimeReminder(binding, activationId)
  }, delayMs)
  ;(binding.reminderTimer as { unref?: () => void }).unref?.()
}

export function armRuntimeReminder(binding: Binding): void {
  stopRuntimeReminder(binding, true)
  binding.activationStartedAt = Date.now()
  binding.nextReminderMs = binding.reminderIntervalMs
  scheduleRuntimeReminder(binding, String(binding.activationId ?? ''))
}

export function discardResumedAuthoritativeContext(binding: Binding): void {
  const pending = [...(binding.handle!.agent.inbox.nextStep ?? [])]
  let removed = 0
  for (const message of pending) {
    const source = message?.source
    const content = message?.content?.[0]
    if (
      source?.kind === `plugin:${name}`
      && content?.type === 'text'
      && content.text.startsWith('VOICE AUTHORITATIVE CONTEXT')
      && binding.handle!.agent.inbox.remove(message.id)
    ) removed += 1
  }
  if (removed) console.info('[voice-shadow-bridge] context merge', { action: 'resume_discarded', count: removed })
}

export function reassertContextAfterCompaction(binding: Binding): void {
  void bindingCallback(binding, '/api/internal/dsh-bridge/shadow.context.read', {
    request_id: randomUUID(), voice_session_id: binding.voiceSessionId,
    dsh_session_id: binding.dshSessionId, epoch: binding.epoch,
    since_revision: 0,
  }).then(value => {
    if (binding.state !== 'active' || binding.callbacks?.signal.aborted) return
    if (value?.result !== 'accepted' || !Array.isArray(value.frames)) return
    if (!binding.gateObserved) {
      enqueueAuthoritativeContext(binding, {
        protocol_version: BRIDGE_PROTOCOL_VERSION,
        request_id: randomUUID(), voice_session_id: binding.voiceSessionId,
        dsh_session_id: binding.dshSessionId, epoch: binding.epoch,
        context_revision: binding.revision, frames: value.frames,
      }, 'snapshot')
      return
    }
    const guided = binding.claimedRevision ?? 0
    if (guided <= 0) return
    const pending: PendingContext = {
      messageId: randomUUID(), update: 'snapshot',
      fromRevision: 0, revision: guided,
      frames: value.frames.filter(frame => Number(record(frame).revision ?? 0) <= guided),
    }
    binding.handle!.agent.inject(contextMessage(binding, pending))
    console.info('[voice-shadow-bridge] context merge', {
      action: 'compaction_reassert', activation_id: binding.activationId ?? '',
      context_revision: guided, message_id: pending.messageId,
    })
  }).catch(() => undefined)
}

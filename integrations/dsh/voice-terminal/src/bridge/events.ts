import { record } from '../shared/json-record.js'
import type { Context } from '../shared/services.js'
/** Event component（事件组件）：只观察已提交事件，并在空闲安全点恢复协议。 */
import { randomUUID } from 'node:crypto'
import { digest, isDelivered } from '../shared/protocol.js'
import { MAX_OBSERVER_RECORDS } from '../shared/identity.js'
import { bindingCallback } from './callbacks.js'
import { clearActivationIfCurrent, createBridgeUserMessage } from './messages.js'
import { reassertContextAfterCompaction, stopRuntimeReminder } from './context.js'
import { observerData, readSessionTelemetry } from './observer.js'
import type { Binding } from './contracts.js'

export function registerSessionEvents(ctx: Context, bindings: Map<string, Binding>): void {
  ctx.on('session/event', (session, event) => {
    const binding = bindings.get(String(session.id))
    if (!binding || binding.state !== 'active') return
    const type = event.type
    const data = record(event.data)
    const message = record(data.message)
    const first = record(Array.isArray(message.content) ? message.content[0] : undefined)
    if (!['assistant/message', 'tool/call', 'tool/result', 'step/end', 'turn/start', 'turn/end', 'compaction/start', 'compaction/end'].includes(type)) return
    binding.traceRevision += 1
    binding.records.push({ trace_revision: binding.traceRevision, type, data: observerData(event, binding) })
    if (binding.records.length > MAX_OBSERVER_RECORDS) binding.records.splice(0, binding.records.length - MAX_OBSERVER_RECORDS)
    if (type === 'compaction/end' && !data.error) reassertContextAfterCompaction(binding)
    if (type === 'tool/call') binding.pendingTools.set(String(data.callId ?? binding.traceRevision), String(data.name ?? ''))
    if (type === 'tool/result') {
      const callId = String(first.toolCallId ?? record(message.source).callId ?? '')
      binding.pendingTools.delete(callId)
    }
    if (type === 'step/end') {
      binding.committedSteps += 1
      void bindingCallback(binding, '/api/internal/dsh-bridge/shadow.activity', {
        request_id: randomUUID(), voice_session_id: binding.voiceSessionId,
        dsh_session_id: binding.dshSessionId, epoch: binding.epoch,
        context_revision: binding.revision, activation_id: binding.activationId ?? '',
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
    const revision = binding?.queuedMessages?.get(String(message?.id ?? ''))
    if (!binding || revision === undefined) return
    binding.queuedMessages?.delete(String(message.id))
    binding.claimedRevision = Math.max(binding.claimedRevision ?? 0, revision)
    if (binding.gateObserved) console.info('[voice-shadow-bridge] context gate', {
      action: 'claimed', activation_id: binding.activationId ?? '',
      context_revision: binding.claimedRevision, message_id: message.id,
    })
    if (binding.gateObserved && binding.activationId) {
      void bindingCallback(binding, '/api/internal/dsh-bridge/shadow.activity', {
        request_id: randomUUID(), voice_session_id: binding.voiceSessionId,
        dsh_session_id: binding.dshSessionId, epoch: binding.epoch,
        context_revision: binding.revision, activation_id: binding.activationId,
        claimed_revision: binding.claimedRevision, state: binding.agentState,
      }).catch(() => undefined)
    }
  })

  ctx.on('agent/status', ({ agent, status }) => {
    const binding = bindings.get(String(agent?.id ?? ''))
    if (!binding || binding.state !== 'active') return
    binding.traceRevision += 1
    binding.agentState = String(status ?? '')
    binding.records.push({ trace_revision: binding.traceRevision, type: 'agent/status', data: { state: String(status ?? '') } })
    if (status === 'idle' && binding.protocolRecoveryPending && binding.activationId) {
      binding.protocolRecoveryPending = false
      binding.handle!.agent.followup(createBridgeUserMessage([
        { type: 'text', text: 'PROTOCOL RECOVERY: This activation ended without a Voice Bridge terminal tool. Re-evaluate the latest authoritative context and end with exactly one of request_user_input, return_shadow_result, or finish_shadow_no_action.' },
      ]))
    }
    void bindingCallback(binding, '/api/internal/dsh-bridge/shadow.activity', {
      request_id: randomUUID(), voice_session_id: binding.voiceSessionId,
      dsh_session_id: binding.dshSessionId, epoch: binding.epoch,
      context_revision: binding.revision, activation_id: binding.activationId ?? '',
      trace_revision: binding.traceRevision, claimed_revision: binding.claimedRevision ?? 0,
      telemetry: readSessionTelemetry(binding, ctx.get('tokenMeter')),
      state: String(status ?? ''),
    }).catch(() => undefined)
    void bindingCallback(binding, '/api/internal/dsh-bridge/mainline.activity', {
      dsh_session_id: binding.dshSessionId, epoch: binding.epoch,
      state: String(status ?? ''),
    }).catch(() => undefined)
  })

}

import { record } from '../shared/json-record.js'
import { SessionId } from '@deepseek-ai/dsh-session'
import type { AgentHandle } from '@deepseek-ai/dsh-agent'
import type { Context } from '../shared/services.js'
/** HTTP component（本机接口组件）：现有 Bridge 2.0 路由与会话操作。 */
import type { IncomingMessage, ServerResponse } from 'node:http'
import { BRIDGE_PROTOCOL_VERSION, json, result, authorized, readBody, digest, assertEnvelope } from '../shared/protocol.js'
import type { Json } from '../shared/protocol.js'
import { GENERATED_SHADOW_PROMPT } from '../shared/generated-prompt.js'
import { BUNDLE_VERSION } from '../shared/version.js'
import { CallbackScope } from '../shared/callback.js'
import { BRIDGE_NAME as name, MAX_OBSERVER_RECORDS } from '../shared/identity.js'
import { createAttachedVoiceSession } from './settings.js'
import { installRootTools } from './tools.js'
import { createUserMessage, createBridgeUserMessage } from './messages.js'
import { enqueueAuthoritativeContext, holdAuthoritativeContext, releaseHeldContext, enqueueDeferredActivation, armRuntimeReminder, stopRuntimeReminder, discardResumedAuthoritativeContext } from './context.js'
import { exportSessionPage, readSessionTelemetry } from './observer.js'
import type { Binding, Config } from './contracts.js'
import { stopRuntimeActivity } from './events.js'

export function createBridgeRoute(ctx: Context, config: Config, bindings: Map<string, Binding>, settingsReady: ReturnType<typeof import('./settings.js').initializeVoiceTerminalSettings>, startupError: () => unknown) {
  return {
    kind: 'prefix' as const,
    path: config.routePrefix,
    async handler(req: IncomingMessage, res: ServerResponse) {
      if (!authorized(req)) { json(res, 401, result('unauthorized')); return }
      const path = new URL(req.url ?? '/', 'http://127.0.0.1').pathname.slice(config.routePrefix.length).replace(/^\//, '')
      if (req.method === 'GET' && path === 'health') {
        json(res, 200, { protocol_version: BRIDGE_PROTOCOL_VERSION, status: 'ok', bundle_version: BUNDLE_VERSION }); return
      }
      if (req.method !== 'POST') { json(res, 404, result('not_found')); return }
      try {
        const body = await readBody(req, config.maxBodyBytes)
        assertEnvelope(body)
        const existing = bindings.get(body.dsh_session_id)
        const prior = existing?.requests.get(body.request_id)
        const bodyDigest = digest(body)
        if (prior) { json(res, 200, prior.digest === bodyDigest ? result('duplicate', { original: prior.response }) : result('conflict')); return }
        if (path === 'session.open' || path === 'session.resume') {
          if (existing) { json(res, 409, result('conflict')); return }
          const requestedReminderMs = Number(body.runtime_reminder_interval_ms)
          const reminderIntervalMs = Number.isSafeInteger(requestedReminderMs) && requestedReminderMs > 0
            ? requestedReminderMs : 20000
          const binding: Binding = { voiceSessionId: body.voice_session_id, dshSessionId: body.dsh_session_id, epoch: body.epoch, revision: 0, state: 'active', protocolRecoveryAttempted: false, protocolRecoveryPending: false, handle: undefined, requests: new Map(), records: [], traceRevision: 0, agentState: 'idle', committedSteps: 0, pendingTools: new Map(), pendingToolActivations: new Map(), actionSeq: 0, actionQueue: Promise.resolve(), assistantStreamRevision: 0, pendingThinkingDelta: '', queuedMessages: new Map(), reminderIntervalMs, nextReminderMs: reminderIntervalMs, queuedRevision: 0, claimedRevision: 0 }
          const preset = await ctx.agentPresets.resolve('shadow-agent')
          const modelSelection = ctx.agentDefaultModel.currentSelection()
          const setup = async (agentCtx: Context) => { await ctx.agentPresets.mount(agentCtx, preset.id); installRootTools(agentCtx, binding) }
          let handle: AgentHandle
          if (path === 'session.resume') {
            // A resumed voice session keeps its persisted Session settings; plugin defaults
            // are resolved only for a newly opened session.
            handle = await ctx.agents.resume({ resumeSessionId: SessionId(body.dsh_session_id), agentOptions: modelSelection, setup })
          } else {
            try {
              const initialized = await settingsReady
              const opened = await createAttachedVoiceSession({
                ctx,
                settings: initialized.settings,
                sessionId: body.dsh_session_id,
                presetId: preset.id,
                globalSelection: modelSelection,
                setup,
              })
              handle = opened.handle
            } catch (error) {
              const message = String(error instanceof Error ? error.message : error).slice(0, 240)
              console.error('[voice-shadow-bridge] voice session startup failed', {
                session_id: body.dsh_session_id,
                startup_error: startupError() === undefined ? undefined : String(startupError()),
                error: message,
              })
              json(res, 503, result('conflict', { message: `voice session startup failed: ${message}` }))
              return
            }
          }
          binding.handle = handle
          binding.callbacks = new CallbackScope()
          // archive_only（仅恢复归档）：不注入提示或启动推理，保留原始持久记录。
          if (path === 'session.resume' && body.archive_only !== true) discardResumedAuthoritativeContext(binding)
          bindings.set(binding.dshSessionId, binding)
          const voiceLabel = binding.voiceSessionId.replace(/^conversation[-_]/, '').slice(0, 8)
          ctx.sessionTitle.rename(handle.agent.session, `Voice Shadow · ${voiceLabel} · epoch ${binding.epoch}`)
          if (body.archive_only !== true) handle.agent.inject(createBridgeUserMessage([
            { type: 'text', text: `MANAGED VOICE SHADOW INSTRUCTIONS\nSource: config/shadow_agent/instructions.md\n\n${GENERATED_SHADOW_PROMPT}` },
          ]))
          const response = result('accepted', { dsh_session_id: binding.dshSessionId })
          binding.requests.set(body.request_id, { digest: bodyDigest, response })
          json(res, 200, response); return
        }
        if (!existing || existing.voiceSessionId !== body.voice_session_id) { json(res, 404, result('not_found')); return }
        if (path === 'session.retire' && existing.epoch === body.epoch && existing.state === 'retired') {
          json(res, 200, result('duplicate')); return
        }
        if (existing.epoch !== body.epoch || existing.state === 'retired') { json(res, 409, result('stale')); return }
        let response: Json
        if (path === 'approval.decide') {
          const pending = existing.pendingApprovals?.get(String(body.approval_id ?? ''))
          if (!pending || pending.activationId !== existing.activationId || body.activation_id !== pending.activationId) response = result('stale')
          else if (body.decision !== 'allowed-once' && body.decision !== 'rejected') response = result('conflict')
          else { pending.settle(body.decision); response = result('accepted') }
        } else if (path === 'context.snapshot' || path === 'context.update') {
          const revision = Number(body.context_revision)
          const isSnapshot = path.endsWith('snapshot')
          const contextDigest = digest({ revision, from_revision: body.from_revision, frames: body.frames })
          if (!Number.isSafeInteger(revision) || revision < 0) response = result('conflict')
          else if (!isSnapshot && Number(body.from_revision) !== existing.revision) response = result('resync_required')
          else if (!isSnapshot && revision <= Number(body.from_revision)) response = result('conflict')
          else if (revision < existing.revision) response = result('stale')
          else if (revision === existing.revision && existing.contextDigest === contextDigest) {
              console.info('[voice-shadow-bridge] context merge', {
                action: 'duplicate', context_revision: revision,
                frame_count: Array.isArray(body.frames) ? body.frames.length : 0,
              })
              response = result('duplicate')
          }
          else if (!isSnapshot && revision === existing.revision) response = result('conflict')
          else {
            if (body.guidance_delivery === 'hold') holdAuthoritativeContext(existing, body, isSnapshot ? 'snapshot' : 'delta')
            else {
              if (!existing.heldContext) existing.gateObserved = false
              enqueueAuthoritativeContext(existing, body, isSnapshot ? 'snapshot' : 'delta')
            }
            existing.revision = revision
            existing.contextDigest = contextDigest
            existing.traceRevision += 1
            existing.records.push({
              trace_revision: existing.traceRevision,
              type: 'voice/context',
              data: { source: 'Voice Terminal', epoch: existing.epoch, revision, update: isSnapshot ? 'snapshot' : 'delta' },
            })
            response = result('accepted', { context_revision: revision })
          }
        } else if (path === 'context.guidance') {
          const revision = Number(body.context_revision)
          const timing = String(body.timing ?? '')
          if (!existing.activationId || body.activation_id !== existing.activationId
              || !Number.isSafeInteger(revision) || revision > existing.revision
              || !['inject_now', 'defer_once'].includes(timing)
              || !existing.heldContext || existing.heldContext.fromRevision >= revision
              || existing.heldContext.revision < revision) response = result('conflict')
          else if (timing === 'defer_once') {
            existing.deferredRevision = revision
            console.info('[voice-shadow-bridge] context gate', {
              action: 'deferred', activation_id: existing.activationId,
              from_revision: existing.heldContext.fromRevision, context_revision: revision,
            })
            response = result('accepted', { queued_revision: existing.queuedRevision ?? 0, deferred_revision: revision })
          } else {
            const action = releaseHeldContext(existing, revision)
            response = result('accepted', { queued_revision: existing.queuedRevision ?? 0, merge_action: action })
          }
        } else if (path === 'wake') {
          const revision = Number(body.context_revision)
          if (revision !== existing.revision) response = revision < existing.revision ? result('stale') : result('resync_required')
          else {
            existing.activationId = String(body.activation_id ?? '')
            existing.activationOrigin = 'voice'
            existing.activationReady = undefined
            existing.actionSeq = 0
            existing.terminalActivationId = undefined
            existing.pendingTools.clear()
            existing.pendingToolActivations.clear()
            stopRuntimeActivity(existing)
            existing.protocolRecoveryAttempted = false
            existing.protocolRecoveryPending = false
            existing.traceRevision += 1
            existing.records.push({ trace_revision: existing.traceRevision, type: 'voice/activation', data: { activation_id: existing.activationId } })
            if (existing.records.length > MAX_OBSERVER_RECORDS) existing.records.splice(0, existing.records.length - MAX_OBSERVER_RECORDS)
            if (body.guidance_delivery === 'deferred_next_turn') {
              if (!enqueueDeferredActivation(existing, existing.activationId)) throw new Error('deferred context missing')
            } else {
              const wake = createBridgeUserMessage([{ type: 'text', text: `VOICE SHADOW ACTIVATION\nActivation: ${existing.activationId}\nRevision: ${existing.revision}\nJudge and execute the current task. End only with a Voice Bridge terminal tool.` }])
              if (existing.handle!.agent.status === 'idle') existing.handle!.agent.followup(wake)
              else existing.handle!.agent.steer(wake)
            }
            armRuntimeReminder(existing)
            response = result('accepted')
          }
        } else if (path === 'task.deliver') {
          const taskId = String(body.task_id ?? '')
          const occurrenceId = String(body.occurrence_id ?? '')
          const text = String(body.text ?? '').trim()
          const target = String(body.target ?? 'shadow_agent')
          if (!taskId || !occurrenceId || !text) response = result('conflict')
          else {
            const message = createUserMessage({
              content: [{
                type: 'text',
                text: `定时任务 ${taskId} 已到期。请在当前主线会话中按普通消息处理。需要创建时的背景可调用 read_wake_reference(task_id) 按需读取。目标：${target}。若需向 Voice 汇报，请调用 publish_voice_event；目标为 voice_llm 或 both 时应主动投递语音事件。\n\n任务内容：${text}`,
              }],
              source: { kind: 'schedule' },
            }, occurrenceId)
            existing.handle!.agent.followup(message)
            const flushed = await ctx.sessions.flush(existing.handle!.agent.session)
            if (!flushed) throw new Error('DSH session persistence did not confirm task delivery')
            response = result('accepted', { dsh_session_id: existing.dshSessionId, message_id: occurrenceId })
          }
        } else if (path === 'session.cancel') {
          stopRuntimeActivity(existing)
          stopRuntimeReminder(existing, true); existing.handle!.agent.cancel({ kind: 'hook', reason: 'voice-terminal-cancel' }, { keepInbox: false }); existing.pendingContext = undefined; existing.queuedMessages?.clear(); existing.heldContext = undefined; existing.deferredRevision = undefined; existing.pendingReminder = undefined; delete existing.activationId
          // whenIdle（结算至空闲）包括工具取消与最终记录，不能只确认取消信号。
          await existing.handle!.agent.whenIdle()
          if (!await ctx.sessions.flush(existing.handle!.agent.session)) throw new Error('session cancel persistence failed')
          response = result('accepted')
        } else if (path === 'session.retire') {
          stopRuntimeActivity(existing)
          stopRuntimeReminder(existing, true); existing.handle!.agent.cancel({ kind: 'hook', reason: 'voice-terminal-retire' }, { keepInbox: false }); existing.pendingContext = undefined; existing.queuedMessages?.clear(); existing.heldContext = undefined; existing.deferredRevision = undefined; existing.pendingReminder = undefined; delete existing.activationId
          await existing.handle!.agent.whenIdle()
          if (!await ctx.sessions.flush(existing.handle!.agent.session)) throw new Error('session retire persistence failed')
          existing.state = 'retired'
          // Retire（解绑归档）后的后台回执不再属于活跃会话，取消并等待释放。
          await existing.callbacks?.dispose()
          await existing.actionQueue
          existing.exportSnapshot = undefined
          const voiceLabel = existing.voiceSessionId.replace(/^conversation[-_]/, '').slice(0, 8)
          ctx.sessionTitle.rename(existing.handle!.agent.session, `Voice Shadow · ${voiceLabel} · epoch ${existing.epoch} · retired`)
          response = result('accepted')
        } else if (path === 'session.state') {
          response = result('accepted', {
            state: existing.state,
            context_revision: existing.revision,
            activation_id: existing.activationId ?? '',
            activation_origin: existing.activationOrigin ?? 'voice',
            terminal: !existing.activationId,
            agent_state: existing.handle!.agent.status,
            ready_for_handoff: !existing.activationId && existing.handle!.agent.status === 'idle',
            record_count: existing.handle!.agent.session.snapshotEvents().length, // record_count（归档记录水位）。
            queued_revision: existing.queuedRevision ?? 0,
            claimed_revision: existing.claimedRevision ?? 0,
            deferred_revision: existing.deferredRevision ?? 0,
          })
        } else if (path === 'session.export') {
          if (!await ctx.sessions.flush(existing.handle!.agent.session)) throw new Error('session export persistence failed')
          response = result('accepted', exportSessionPage(existing, body))
        } else if (path === 'session.context') {
          const budget = { chars: 100_000 }
          const rows: Array<{ role: string; text: string }> = []
          for (const item of existing.handle!.agent.session.snapshotEvents().slice(-240).reverse()) {
            if (!['user/message', 'assistant/message', 'tool/result'].includes(String(item?.type))) continue
            const data = record(item.data)
            const message = record(data.message)
            if (item.type === 'user/message' && record(message.source).kind === 'plugin:' + name) continue
            const raw = message.content ?? data.content ?? data.message ?? data
            const rendered = typeof raw === 'string' ? raw : JSON.stringify(raw ?? '')
            const text = rendered.slice(0, budget.chars).trim()
            budget.chars -= text.length
            if (text) rows.unshift({ role: String(item.type), text })
            if (budget.chars <= 0) break
          }
          response = result('accepted', { rows: rows })
        } else if (path === 'observer.snapshot' || path === 'observer.update') {
          const since = Number(body.trace_revision ?? 0)
          const first = existing.records[0]?.trace_revision ?? existing.traceRevision
          if (!Number.isSafeInteger(since) || since < 0 || since > existing.traceRevision) response = result('resync_required')
          else if (since > 0 && since < first - 1) response = result('resync_required')
          else {
            const records = existing.records.filter(item => item.trace_revision > since)
            response = result('accepted', {
              mode: since === 0 ? 'snapshot' : (records.length ? 'update' : 'unchanged'),
              trace_revision: existing.traceRevision,
              agent_state: existing.agentState,
              committed_model_steps: existing.committedSteps,
              telemetry: readSessionTelemetry(existing, ctx.get('tokenMeter')),
              pending_tools: [...existing.pendingTools.entries()].map(([call_id, name]) => ({ call_id, name })),
              pending_subagents: [...existing.pendingTools.entries()]
                .filter(([, name]) => name.includes('subagent'))
                .map(([call_id, tool]) => ({ call_id, tool })),
              records: records.map(item => ({ ...item })),
            })
          }
        } else response = result('not_found')
        existing.requests.set(body.request_id, { digest: bodyDigest, response })
        json(res, response && record(response).result === 'not_found' ? 404 : 200, response)
      } catch (error) {
        json(res, 400, result('conflict', { message: String(error instanceof Error ? error.message : error).slice(0, 160) }))
      }
    },
  }
}

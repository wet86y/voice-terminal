import assert from 'node:assert/strict'
import test from 'node:test'

import {
  clearActivationIfCurrent,
  exportSessionPage,
  deliverRuntimeReminder,
  enqueueAuthoritativeContext,
  enqueueDeferredActivation,
  holdAuthoritativeContext,
  releaseHeldContext,
  stableBridgeToolId,
  stopRuntimeReminder,
} from '../../../../.runtime/dsh/bundles/voice-terminal-shadow-bridge-test/lib/index.js'
import { assertV4MessageSources } from '../../../../.runtime/dsh/source/packages/session/session-format-v3-to-v4/src/message-sources.ts'

function fixture() {
  const nextStep = []
  const nextTurn = []
  const agent = {
    inbox: {
      replace(id, message) {
        for (const queue of [nextStep, nextTurn]) {
          const index = queue.findIndex(item => item.id === id)
          if (index >= 0) { queue.splice(index, 1, message); return true }
        }
        return false
      },
      remove(id) {
        const index = nextStep.findIndex(item => item.id === id)
        if (index < 0) return false
        nextStep.splice(index, 1)
        return true
      },
    },
    inject(message) { nextStep.push(message) },
    followup(message) { nextTurn.push(message) },
  }
  const binding = {
    voiceSessionId: 'voice-session', dshSessionId: 'dsh-session', epoch: 1,
    handle: { agent }, traceRevision: 0, records: [],
  }
  return { binding, nextStep, nextTurn }
}

function envelope(revision, frames, fromRevision = undefined) {
  return {
    protocol_version: '2.0', request_id: `request-${revision}`,
    voice_session_id: 'voice-session', dsh_session_id: 'dsh-session', epoch: 1,
    context_revision: revision, ...(fromRevision === undefined ? {} : { from_revision: fromRevision }),
    frames,
  }
}

function textOf(message) {
  return message.content[0].text
}

test('session export（完整归档）保留长记录并冻结分页，不受后续活动影响', () => {
  const events = Array.from({ length: 401 }, (_, seq) => ({ seq, type: 'user/message', data: { text: `记录${seq}` } }))
  events[200].data.text = '工具结果'.repeat(40000)
  const binding = { handle: { agent: { session: { snapshotEvents: () => events } } } }
  const first = exportSessionPage(binding, { export_id: 'archive-a', limit: 100 })
  events.push({ seq: 401, type: 'user/message', data: { text: '导出期间新增' } })
  events[0].data.text = '后续修改'
  const rows = [...first.records]
  let offset = first.next_offset
  while (offset != null) {
    const page = exportSessionPage(binding, { export_id: 'archive-a', offset, limit: 100, total: first.total })
    rows.push(...page.records)
    offset = page.next_offset
  }
  assert.equal(rows.length, 401)
  assert.equal(rows[0].data.text, '记录0')
  assert.equal(rows[200].data.text.length, 160000)
  assert.equal(exportSessionPage(binding, { export_id: 'archive-b', offset: 400, limit: 10 }).records.length, 2)
  assert.throws(() => exportSessionPage(binding, { export_id: 'archive-b', offset: -1 }))
})

test('Bridge inbox 消息使用 DSH v4 接受的 source（来源字段）', () => {
  const { binding, nextStep } = fixture()
  enqueueAuthoritativeContext(binding, envelope(1, [{ revision: 1 }]), 'snapshot')
  Object.assign(binding, {
    state: 'active', activationId: 'activation-1',
    activationStartedAt: 0, reminderIntervalMs: 10, nextReminderMs: 10,
  })
  assert.equal(deliverRuntimeReminder(binding, 10), 'inserted')

  for (const message of nextStep) {
    assert.doesNotThrow(() => assertV4MessageSources({
      type: 'user/message', seq: 0, time: 1, surfaceOp: 'append', data: message,
    }))
  }
})

test('updates before claim replace one pending input and preserve every frame', () => {
  const { binding, nextStep } = fixture()
  assert.equal(enqueueAuthoritativeContext(binding, envelope(2, [{ revision: 2 }]), 'snapshot'), 'inserted')
  const id = nextStep[0].id
  assert.equal(enqueueAuthoritativeContext(binding, envelope(3, [{ revision: 3 }], 2), 'delta'), 'replaced')
  assert.equal(enqueueAuthoritativeContext(binding, envelope(4, [{ revision: 4 }], 3), 'delta'), 'replaced')

  assert.equal(nextStep.length, 1)
  assert.equal(nextStep[0].id, id)
  assert.match(textOf(nextStep[0]), /Revision: 4/)
  assert.match(textOf(nextStep[0]), /"revision":2/)
  assert.match(textOf(nextStep[0]), /"revision":3/)
  assert.match(textOf(nextStep[0]), /"revision":4/)
})

test('a snapshot supersedes an unclaimed delta', () => {
  const { binding, nextStep } = fixture()
  enqueueAuthoritativeContext(binding, envelope(2, [{ revision: 2 }], 1), 'delta')
  enqueueAuthoritativeContext(binding, envelope(5, [{ revision: 5, full: true }]), 'snapshot')

  assert.equal(nextStep.length, 1)
  assert.match(textOf(nextStep[0]), /Update: snapshot/)
  assert.doesNotMatch(textOf(nextStep[0]), /"revision":2/)
  assert.match(textOf(nextStep[0]), /"revision":5/)
})

test('an update after claim opens a new safe-window input', () => {
  const { binding, nextStep } = fixture()
  enqueueAuthoritativeContext(binding, envelope(2, [{ revision: 2 }]), 'snapshot')
  const claimed = nextStep.shift()

  assert.equal(enqueueAuthoritativeContext(binding, envelope(3, [{ revision: 3 }], 2), 'delta'), 'already_claimed')
  assert.equal(nextStep.length, 1)
  assert.notEqual(nextStep[0].id, claimed.id)
  assert.doesNotMatch(textOf(nextStep[0]), /"revision":2/)
  assert.match(textOf(nextStep[0]), /"revision":3/)
})

test('held authority is synchronized without model injection and releases in revision order', () => {
  const { binding, nextStep } = fixture()
  holdAuthoritativeContext(binding, envelope(3, [{ revision: 3 }], 2), 'delta')
  holdAuthoritativeContext(binding, envelope(4, [{ revision: 4 }], 3), 'delta')
  assert.equal(nextStep.length, 0)
  assert.equal(releaseHeldContext(binding, 4), 'inserted')
  assert.equal(nextStep.length, 1)
  assert.match(textOf(nextStep[0]), /"revision":3.*"revision":4/)
})

test('a guidance decision releases only its version when a newer update is already held', () => {
  const { binding, nextStep } = fixture()
  holdAuthoritativeContext(binding, envelope(3, [{ revision: 3 }], 2), 'delta')
  holdAuthoritativeContext(binding, envelope(4, [{ revision: 4 }], 3), 'delta')
  assert.equal(releaseHeldContext(binding, 3), 'inserted')
  assert.match(textOf(nextStep[0]), /"revision":3/)
  assert.doesNotMatch(textOf(nextStep[0]), /"revision":4/)
  assert.equal(binding.heldContext.revision, 4)
  assert.equal(binding.heldContext.fromRevision, 3)
  assert.equal(releaseHeldContext(binding, 4), 'replaced')
  assert.match(textOf(nextStep[0]), /"revision":3.*"revision":4/)
})

test('user answer merges into unclaimed deferred activation before its next turn', () => {
  const { binding, nextStep, nextTurn } = fixture()
  holdAuthoritativeContext(binding, envelope(3, [{ revision: 3 }], 2), 'delta')
  binding.deferredRevision = 3
  assert.equal(enqueueDeferredActivation(binding, 'activation-b'), true)
  const id = nextTurn[0].id
  holdAuthoritativeContext(binding, envelope(4, [{ revision: 4 }], 3), 'delta')
  assert.equal(releaseHeldContext(binding, 4), 'replaced')
  assert.equal(nextStep.length, 0)
  assert.equal(nextTurn.length, 1)
  assert.equal(nextTurn[0].id, id)
  assert.match(textOf(nextTurn[0]), /VOICE SHADOW ACTIVATION/)
  assert.match(textOf(nextTurn[0]), /VOICE GATE DEFERRED CONTEXT/)
  assert.match(textOf(nextTurn[0]), /"revision":3.*"revision":4/)
})

test('user answer after deferred activation claim enters the next step', () => {
  const { binding, nextStep, nextTurn } = fixture()
  holdAuthoritativeContext(binding, envelope(3, [{ revision: 3 }], 2), 'delta')
  binding.deferredRevision = 3
  enqueueDeferredActivation(binding, 'activation-b')
  nextTurn.shift()
  holdAuthoritativeContext(binding, envelope(4, [{ revision: 4 }], 3), 'delta')
  assert.equal(releaseHeldContext(binding, 4), 'already_claimed')
  assert.equal(nextStep.length, 1)
  assert.doesNotMatch(textOf(nextStep[0]), /"revision":3/)
  assert.match(textOf(nextStep[0]), /"revision":4/)
})

function reminderFixture() {
  const value = fixture()
  Object.assign(value.binding, {
    state: 'active', activationId: 'activation-1',
    reminderIntervalMs: 20, activationStartedAt: 1000, nextReminderMs: 20,
    traceRevision: 0, records: [],
  })
  return value
}

test('runtime reminders start at the interval and replace before claim', () => {
  const { binding, nextStep } = reminderFixture()
  assert.equal(deliverRuntimeReminder(binding, 1019), false)
  assert.equal(nextStep.length, 0)

  assert.equal(deliverRuntimeReminder(binding, 1020), 'inserted')
  const id = nextStep[0].id
  assert.match(textOf(nextStep[0]), /运行时限提醒/)
  assert.match(textOf(nextStep[0]), /立即收敛/)
  assert.equal(deliverRuntimeReminder(binding, 1040), 'replaced')
  assert.equal(nextStep.length, 1)
  assert.equal(nextStep[0].id, id)
  assert.equal(binding.nextReminderMs, 60)
})

test('a claimed reminder opens one new input at the next interval', () => {
  const { binding, nextStep } = reminderFixture()
  deliverRuntimeReminder(binding, 1020)
  const claimed = nextStep.shift()

  assert.equal(deliverRuntimeReminder(binding, 1040), 'already_claimed')
  assert.equal(nextStep.length, 1)
  assert.notEqual(nextStep[0].id, claimed.id)
})

test('a delayed timer emits only the newest crossed-interval reminder', () => {
  const { binding, nextStep } = reminderFixture()
  deliverRuntimeReminder(binding, 1020)

  assert.equal(deliverRuntimeReminder(binding, 1105), 'replaced')
  assert.equal(nextStep.length, 1)
  assert.equal(binding.nextReminderMs, 120)
  assert.equal(binding.records.at(-1).data.crossed_intervals, 5)
})

test('settled activations never enqueue a runtime reminder', () => {
  const { binding, nextStep } = reminderFixture()
  delete binding.activationId

  assert.equal(deliverRuntimeReminder(binding, 1100), false)
  assert.equal(nextStep.length, 0)
})

test('a late terminal callback cannot clear a newer activation', () => {
  const binding = { activationId: 'activation-new' }
  assert.equal(clearActivationIfCurrent(binding, 'activation-old'), false)
  assert.equal(binding.activationId, 'activation-new')
  assert.equal(clearActivationIfCurrent(binding, 'activation-new'), true)
  assert.equal(binding.activationId, undefined)
})

test('tool callback ids are stable per DSH call and separated by purpose', () => {
  const first = stableBridgeToolId('event', 'dsh-session', 'call-1')
  assert.equal(first, stableBridgeToolId('event', 'dsh-session', 'call-1'))
  assert.notEqual(first, stableBridgeToolId('request', 'dsh-session', 'call-1'))
  assert.notEqual(first, stableBridgeToolId('event', 'dsh-session', 'call-2'))
})

test('stopping an activation clears its timer and pending reminder', async () => {
  const { binding, nextStep } = reminderFixture()
  deliverRuntimeReminder(binding, 1020)
  binding.reminderTimer = setTimeout(() => assert.fail('cleared timer fired'), 1000)
  binding.reminderTimer.unref()

  stopRuntimeReminder(binding, true)
  await new Promise(resolve => setImmediate(resolve))

  assert.equal(binding.reminderTimer, undefined)
  assert.equal(binding.activationStartedAt, undefined)
  assert.equal(binding.pendingReminder, undefined)
  assert.equal(nextStep.length, 0)
})

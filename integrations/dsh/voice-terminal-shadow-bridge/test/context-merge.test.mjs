import assert from 'node:assert/strict'
import test from 'node:test'

import {
  deliverRuntimeReminder,
  enqueueAuthoritativeContext,
  stopRuntimeReminder,
} from '../../../../.runtime/dsh/bundles/voice-terminal-shadow-bridge-test/lib/index.js'

function fixture() {
  const nextStep = []
  const agent = {
    inbox: {
      replace(id, message) {
        const index = nextStep.findIndex(item => item.id === id)
        if (index < 0) return false
        nextStep.splice(index, 1, message)
        return true
      },
      remove(id) {
        const index = nextStep.findIndex(item => item.id === id)
        if (index < 0) return false
        nextStep.splice(index, 1)
        return true
      },
    },
    inject(message) { nextStep.push(message) },
  }
  const binding = {
    voiceSessionId: 'voice-session', dshSessionId: 'dsh-session', epoch: 1,
    handle: { agent },
  }
  return { binding, nextStep }
}

function envelope(revision, frames, fromRevision = undefined) {
  return {
    protocol_version: '1.0', request_id: `request-${revision}`,
    voice_session_id: 'voice-session', dsh_session_id: 'dsh-session', epoch: 1,
    context_revision: revision, ...(fromRevision === undefined ? {} : { from_revision: fromRevision }),
    frames,
  }
}

function textOf(message) {
  return message.content[0].text
}

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

function reminderFixture() {
  const value = fixture()
  Object.assign(value.binding, {
    state: 'active', terminal: false, activationId: 'activation-1',
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

test('terminal activations never enqueue a runtime reminder', () => {
  const { binding, nextStep } = reminderFixture()
  binding.terminal = true

  assert.equal(deliverRuntimeReminder(binding, 1100), false)
  assert.equal(nextStep.length, 0)
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

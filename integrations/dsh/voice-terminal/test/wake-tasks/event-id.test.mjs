import assert from 'node:assert/strict'
import test from 'node:test'

import { stableVoiceEventId } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/wake-tasks/event-id.js'

test('publish_voice_event（投递语音事件）复用 callId（工具调用标识）生成稳定幂等键', () => {
  const first = stableVoiceEventId('voice-shadow-session', 'call-1')
  assert.equal(first, stableVoiceEventId('voice-shadow-session', 'call-1'))
  assert.notEqual(first, stableVoiceEventId('voice-shadow-session', 'call-2'))
  assert.notEqual(first, stableVoiceEventId('voice-shadow-other', 'call-1'))
  assert.match(first, /^[a-f0-9]{64}$/u)
})

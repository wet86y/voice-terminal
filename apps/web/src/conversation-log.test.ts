import { describe, expect, it } from 'vitest'

import { ConversationLog, readConversationEntry } from './conversation-log'
import type { ConversationEntry } from './types'

function entry(overrides: Partial<ConversationEntry> = {}): ConversationEntry {
  return {
    seq: 1,
    kind: 'user_turn',
    conversation_id: 'conversation_a',
    turn_id: 'input_1',
    speech_id: '',
    text: '第一句',
    status: 'completed',
    delivery: {},
    termination: 'completed',
    at: '2026-09-17T23:16:00.000Z',
    ...overrides,
  }
}

function labels(log: ConversationLog) {
  return log.entries().map(item => `${item.seq}:${item.kind}:${item.text}`)
}

describe('ConversationLog', () => {
  it('renders entries in server order', () => {
    const log = new ConversationLog()
    log.applyEntry(entry({ seq: 2, kind: 'system_speech', turn_id: 'wake_1', speech_id: 'speech_wake', text: '我在，请说。' }))
    log.applyEntry(entry({ seq: 1, text: '灵月是什么游戏？' }))
    log.applyEntry(entry({ seq: 3, kind: 'assistant_speech', speech_id: 'speech_1', text: '它应该是……' }))

    expect(labels(log)).toEqual([
      '1:user_turn:灵月是什么游戏？',
      '2:system_speech:我在，请说。',
      '3:assistant_speech:它应该是……',
    ])
    expect(log.latestSeq()).toBe(3)
  })

  it('never moves an existing line when later turns arrive', () => {
    // Regression: the wake acknowledgement used to be anchored to the newest
    // turn, so every later turn pushed it further down the rail.
    const log = new ConversationLog()
    log.applyEntry(entry({ seq: 1, text: '灵月是什么游戏？' }))
    log.applyEntry(entry({ seq: 2, kind: 'assistant_speech', speech_id: 'speech_1', text: '回复一' }))
    log.applyEntry(entry({ seq: 3, kind: 'system_speech', turn_id: 'wake_1', speech_id: 'speech_wake', text: '我在，请说。' }))
    const before = labels(log)

    log.applyEntry(entry({ seq: 4, turn_id: 'input_2', text: '补充一点。' }))
    log.applyEntry(entry({ seq: 5, turn_id: 'input_3', text: '还有一件事。' }))

    const after = labels(log)
    expect(after.slice(0, before.length)).toEqual(before)
    expect(after.slice(before.length)).toEqual(['4:user_turn:补充一点。', '5:user_turn:还有一件事。'])
  })

  it('refreshes a known line in place instead of appending it twice', () => {
    const log = new ConversationLog()
    log.applyEntry(entry({ seq: 1, text: '进行中', status: 'pending' }))
    log.applyEntry(entry({ seq: 2, kind: 'assistant_speech', speech_id: 'speech_1', text: '回复一' }))
    // The server re-states the same turn after settlement with the same seq.
    log.applyEntry(entry({ seq: 1, text: '最终转写', status: 'empty_input' }))

    expect(log.size()).toBe(2)
    expect(labels(log)).toEqual(['1:user_turn:最终转写', '2:assistant_speech:回复一'])
    expect(log.entries()[0].status).toBe('empty_input')
  })

  it('treats a re-delivered line as a no-op', () => {
    const log = new ConversationLog()
    const line = entry({ seq: 1 })
    log.applyEntry(line)
    log.applyEntry({ ...line })

    expect(log.size()).toBe(1)
    expect(labels(log)).toEqual(['1:user_turn:第一句'])
  })

  it('replaces the whole view from an authoritative snapshot', () => {
    const log = new ConversationLog()
    log.applyEntry(entry({ seq: 9, text: '旧会话' }))

    log.applySnapshot({
      entries: [
        entry({ seq: 1, text: '新会话第一句' }),
        entry({ seq: 2, kind: 'assistant_speech', speech_id: 'speech_1', text: '新会话回复' }),
      ],
      latest_seq: 2,
    })

    expect(labels(log)).toEqual(['1:user_turn:新会话第一句', '2:assistant_speech:新会话回复'])
    expect(log.latestSeq()).toBe(2)
  })

  it('ignores a snapshot without entries and keeps the current view', () => {
    const log = new ConversationLog()
    log.applyEntry(entry({ seq: 1 }))

    log.applySnapshot({ latest_seq: 4 })

    expect(log.size()).toBe(1)
    expect(log.latestSeq()).toBe(1)
  })

  it('exposes which turn or speech is already visible', () => {
    const log = new ConversationLog()
    log.applyEntry(entry({ seq: 1, turn_id: 'input_1' }))
    log.applyEntry(entry({ seq: 2, kind: 'assistant_speech', turn_id: 'input_1', speech_id: 'speech_1' }))

    expect(log.hasTurn('input_1')).toBe(true)
    expect(log.hasTurn('input_2')).toBe(false)
    expect(log.hasSpeech('conversation_a', 'speech_1')).toBe(true)
    expect(log.hasSpeech('conversation_b', 'speech_1')).toBe(false)
    expect(log.hasTurn('')).toBe(false)
  })

  it('clears on a session boundary', () => {
    const log = new ConversationLog()
    log.applyEntry(entry({ seq: 7 }))
    log.clear()

    expect(log.entries()).toEqual([])
    expect(log.latestSeq()).toBe(0)
  })
})

describe('readConversationEntry', () => {
  it('reads a full payload', () => {
    expect(readConversationEntry({
      seq: 3, kind: 'assistant_speech', conversation_id: 'conversation_a', turn_id: 'input_1',
      speech_id: 'speech_1', text: '回复一', status: 'interrupted',
      delivery: { status: 'partial', termination: 'interrupted', heard_text: '回复' },
      termination: 'interrupted', at: '2026-09-17T23:18:00.000Z',
    })).toEqual({
      seq: 3, kind: 'assistant_speech', conversation_id: 'conversation_a', turn_id: 'input_1',
      speech_id: 'speech_1', text: '回复一', status: 'interrupted',
      delivery: { status: 'partial', termination: 'interrupted', heard_text: '回复' },
      termination: 'interrupted', at: '2026-09-17T23:18:00.000Z',
    })
  })

  it('falls back to the envelope turn id and tolerates missing fields', () => {
    const parsed = readConversationEntry(
      { seq: 1, kind: 'user_turn', conversation_id: 'conversation_a', text: '你好' },
      'input_9',
    )

    expect(parsed).toMatchObject({ turn_id: 'input_9', speech_id: '', status: '', delivery: {} })
  })

  it('rejects anything that cannot be ordered', () => {
    expect(readConversationEntry(null)).toBeNull()
    expect(readConversationEntry({})).toBeNull()
    expect(readConversationEntry({ seq: 1, kind: 'unknown' })).toBeNull()
    expect(readConversationEntry({ kind: 'user_turn' })).toBeNull()
    expect(readConversationEntry({ seq: Number.NaN, kind: 'user_turn' })).toBeNull()
  })
})

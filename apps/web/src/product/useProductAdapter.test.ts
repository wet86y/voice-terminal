import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { describe, expect, it } from 'vitest'
import type { ConversationEntry } from '../types'
import type { ProductSnapshot } from './types'
import { useProductAdapter } from './useProductAdapter'

function entry(overrides: Partial<ConversationEntry>): ConversationEntry {
  return {
    seq: 1, kind: 'user_turn', conversation_id: 'conversation_a', turn_id: 'input_1',
    speech_id: '', text: '', status: 'empty_input', delivery: {}, termination: 'empty_input', at: '',
    ...overrides,
  }
}

describe('产品对话区的空输入展示', () => {
  for (const live of [false, true]) {
    it(`${live ? '实时连接' : '离线快照'}隐藏空输入，保留诊断记录、短回答及助手消息`, async () => {
      const entries = [
        entry({ seq: 1, turn_id: 'empty_1' }),
        entry({ seq: 2, turn_id: 'empty_2', text: ' \n\t' }),
        entry({ seq: 3, turn_id: 'short_1', text: '嗯', status: 'completed' }),
        entry({ seq: 4, turn_id: 'refreshed_1', text: '最终转写' }),
        entry({ seq: 5, kind: 'assistant_speech', speech_id: 'speech_1', text: '回复' }),
        entry({ seq: 6, kind: 'system_speech', speech_id: 'wake_1', text: '我在，请说。' }),
        entry({ seq: 7, turn_id: 'error_1', status: 'error' }),
      ]
      const app = createSSRApp({
        setup() {
          const adapter = useProductAdapter()
          // SSR（服务端渲染）不启动网络连接；分别验证两个产品数据来源。
          if (live) {
            adapter.voice.connection.value = 'connected'
            adapter.voice.conversationEntries.value = entries
          } else {
            adapter.snapshot.value = { conversation: { entries } } as ProductSnapshot
          }
          return () => h('div', adapter.conversationRows.value.map(row => h('p', row.text)))
        },
      })

      expect(await renderToString(app)).toBe('<div><p>嗯</p><p>最终转写</p><p>回复</p><p>我在，请说。</p><p></p></div>')
      expect(entries).toHaveLength(7)
      expect(entries.map(item => item.seq)).toEqual([1, 2, 3, 4, 5, 6, 7])
    })
  }
})

import { describe, expect, it } from 'vitest'

import productAppSource from './ProductApp.vue?raw'
import productAdapterSource from './useProductAdapter.ts?raw'

describe('fixed Shadow reasoning policy', () => {
  it('renders a read-only High indicator without the reasoning slider', () => {
    expect(productAppSource).toContain('High · 固定')
    expect(productAppSource).not.toContain('class="thinking-slider"')
    expect(productAppSource).not.toContain('Shadow 思考等级设置')
  })

  it('does not submit a mutable Shadow reasoning field', () => {
    expect(productAdapterSource).not.toContain('shadow_reasoning_effort:')
    expect(productAdapterSource).not.toContain('setShadowThinking')
  })

  it('renders the conversation rail chronologically and keeps it anchored to the tail', () => {
    expect(productAppSource).toContain('const orderedConversation = computed(() => [...conversation.value].reverse())')
    expect(productAppSource).toContain('ref="conversationScrollElement"')
    expect(productAppSource).toContain('@scroll="captureConversationScrollPosition"')
    expect(productAppSource).toContain('class="conversation-stack"')
    expect(productAppSource).toContain('v-for="record in orderedConversation"')
  })
})

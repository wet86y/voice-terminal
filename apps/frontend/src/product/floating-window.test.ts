// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import VoiceOrb from '../interface/VoiceOrb.vue'
import FloatingWindowShell from './FloatingWindowShell.vue'
import type { FloatingWindowModel } from './floating-window'

describe('悬浮窗动效扩展接口', () => {
  it('复用同一语音动效的五频带归一化与运行状态映射', async () => {
    const wrapper = mount(VoiceOrb, { props: { model: {
      state: 'listening', sessionActive: true, audioLevels: [-1, 0.2, 0.5, 1, 2],
    } } })

    expect(wrapper.get('.orb-stage').classes()).toContain('orb-listening')
    expect(wrapper.findAll('.voice-waveform i').map(bar => bar.attributes('style')))
      .toEqual([
        '--voice-level: 0;', '--voice-level: 0.2;', '--voice-level: 0.5;', '--voice-level: 1;', '--voice-level: 1;',
      ])
    await wrapper.setProps({ model: { state: 'thinking', sessionActive: true, audioLevels: [1, 1, 1, 1, 1] } })
    expect(wrapper.findAll('.voice-waveform i').every(bar => bar.attributes('style')?.includes('--voice-level: 0;'))).toBe(true)
    expect(wrapper.get('.orb-stage').classes()).toContain('orb-thinking')
  })

  it('内容插槽收到类型化模型，拖动与双击切换由外壳统一处理', async () => {
    const model: FloatingWindowModel = {
      revision: 3, state: 'speaking', sessionActive: true, audioLevels: [0.1, 0.2, 0.3, 0.4, 0.5],
    }
    const wrapper = mount(FloatingWindowShell, {
      props: { model },
      slots: { content: ({ model: slotModel }: { model: FloatingWindowModel }) => h('span', { 'data-state': slotModel.state }) },
    })

    expect(wrapper.get('[data-state="speaking"]').attributes('data-state')).toBe('speaking')
    await wrapper.get('main').trigger('pointerdown')
    await wrapper.get('main').trigger('dblclick')
    expect(wrapper.emitted('drag')).toHaveLength(1)
    expect(wrapper.emitted('returnToWorkbench')).toHaveLength(1)
  })
})

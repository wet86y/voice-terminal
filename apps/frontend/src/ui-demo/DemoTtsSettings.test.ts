// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import DemoTtsSettings from './DemoTtsSettings.vue'

describe('演示版在线音色设置', () => {
  it('切换在线目标后展示模型、音色及操作，并在语音调节之前替换本地音色库', async () => {
    const wrapper = mount(DemoTtsSettings, { global: { stubs: { TtsVoiceLibrary: true } } })
    expect(wrapper.find('tts-voice-library-stub').exists()).toBe(true)
    await wrapper.get('select').setValue('minimax')
    const online = wrapper.get('.online-tts-settings')
    expect(wrapper.find('tts-voice-library-stub').exists()).toBe(false)
    expect(wrapper.find('.tts-quality').exists()).toBe(false)
    expect(online.element.compareDocumentPosition(wrapper.get('.speech-control-row').element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(online.findAll('.online-model-choice').map(button => button.text())).toEqual(['Turbo低时延', 'HD高音质'])
    expect(online.get('.online-voice-link').text()).toContain('官方音色库')
    expect(online.findAll('.online-tts-buttons button').map(button => button.text())).toEqual(['试音', '保存'])
    await online.findAll('.online-model-choice')[0].trigger('click')
    await online.get('input').setValue('  demo-voice  ')
    await online.get('.save-button').trigger('click')
    expect((online.get('input').element as HTMLInputElement).value).toBe('demo-voice')
    expect(wrapper.text()).toContain('演示设置已保存，仅用于当前展示。')
    await online.get('.online-tts-buttons button').trigger('click')
    expect(wrapper.text()).toContain('演示界面未连接在线语音接口，无法试听。')
    await wrapper.get('select').setValue('voxcpm2')
    expect(wrapper.find('.online-tts-settings').exists()).toBe(false)
    expect(wrapper.find('.tts-quality').exists()).toBe(true)
    await wrapper.get('select').setValue('minimax')
    expect((wrapper.get('.online-voice-row input').element as HTMLInputElement).value).toBe('demo-voice')
    expect(wrapper.get('.online-model-choice').attributes('aria-pressed')).toBe('true')
    wrapper.unmount()
  })
  it('空音色标识时禁用试音和保存，防止演示产生错误的可用状态', async () => {
    const wrapper = mount(DemoTtsSettings, { global: { stubs: { TtsVoiceLibrary: true } } })
    await wrapper.get('select').setValue('minimax')
    await wrapper.get('.online-voice-row input').setValue('   ')
    expect(wrapper.findAll('.online-tts-buttons button').every(button => (button.element as HTMLButtonElement).disabled)).toBe(true)
    wrapper.unmount()
  })
})

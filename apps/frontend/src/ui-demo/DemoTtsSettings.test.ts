// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { productApi } from '../product/api'
import DemoTtsSettings from './DemoTtsSettings.vue'

describe('演示版在线音色设置', () => {
  it('切换在线目标后展示模型、音色及操作，并在语音调节之前替换本地音色库', async () => {
    const lookup = vi.spyOn(productApi, 'minimaxVoiceName').mockResolvedValue({ voice_id: 'demo-voice', voice_name: '不应请求' })
    const wrapper = mount(DemoTtsSettings, { global: { stubs: { TtsVoiceLibrary: true } } })
    await wrapper.get('[aria-label="语音播放目标"] button:last-child').trigger('click')
    expect(wrapper.find('tts-voice-library-stub').exists()).toBe(true)
    expect(wrapper.get('.demo-target-row .instruction-hint').text()).toContain('当前生效：在线语音')
    await wrapper.get('button[aria-label="保存播放目标"]').trigger('click')
    await wrapper.get('[aria-label="语音播放目标"] button:first-child').trigger('click')
    const online = wrapper.get('.online-tts-settings')
    expect(wrapper.find('tts-voice-library-stub').exists()).toBe(false)
    expect(wrapper.find('.tts-quality').exists()).toBe(false)
    expect(online.element.compareDocumentPosition(wrapper.get('.speech-control-row').element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(online.findAll('.online-model-choice').map(button => button.text())).toEqual(['Turbo低时延', 'HD高音质'])
    expect(online.get('.online-voice-link').text()).toContain('官方音色库')
    expect(online.findAll('.online-tts-buttons button').map(button => button.text())).toEqual(['试音', '保存'])
    await online.findAll('.online-model-choice')[0].trigger('click')
    await online.get('input').setValue('  demo-voice  ')
    expect(online.get('.online-voice-name strong').text()).toBe('演示模式')
    await online.get('.save-button').trigger('click')
    expect((online.get('input').element as HTMLInputElement).value).toBe('demo-voice')
    expect(wrapper.text()).toContain('演示设置已保存，仅用于当前展示。')
    await online.get('.online-tts-buttons button').trigger('click')
    expect(wrapper.text()).toContain('演示界面未连接在线语音接口，无法试听。')
    await wrapper.get('[aria-label="语音播放目标"] button:last-child').trigger('click')
    expect(wrapper.find('.online-tts-settings').exists()).toBe(false)
    expect(wrapper.find('tts-voice-library-stub').exists()).toBe(true)
    await wrapper.get('[aria-label="语音播放目标"] button:first-child').trigger('click')
    expect((wrapper.get('.online-voice-row input').element as HTMLInputElement).value).toBe('demo-voice')
    expect(wrapper.get('.online-model-choice').attributes('aria-pressed')).toBe('true')
    expect(lookup).not.toHaveBeenCalled()
    wrapper.unmount()
    lookup.mockRestore()
  })
  it('空音色标识时禁用试音和保存，防止演示产生错误的可用状态', async () => {
    const wrapper = mount(DemoTtsSettings, { global: { stubs: { TtsVoiceLibrary: true } } })
    await wrapper.get('.online-voice-row input').setValue('   ')
    expect(wrapper.findAll('.online-tts-buttons button').every(button => (button.element as HTMLButtonElement).disabled)).toBe(true)
    wrapper.unmount()
  })
})

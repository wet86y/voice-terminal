// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, reactive, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { provideProductContext } from '../product-context'
import { productApi } from '../api'
import * as desktopBridge from '../desktop-port'
import TtsSettings from './TtsSettings.vue'
import type { ProductLocalTtsStatus, ProductTtsSettings } from '../types'

const state = vi.hoisted(() => ({ live: undefined as unknown }))
vi.mock('../useLocalTtsStatus', () => ({ useLocalTtsStatus: () => state.live }))

describe('音色设置只提供资源完整的本地模型', () => {
  let settings: { tts: ProductTtsSettings }
  beforeEach(() => {
    vi.restoreAllMocks()
    settings = reactive({ tts: { backend: 'minimax', local_model: 'voxcpm1' } })
    state.live = { status: ref<ProductLocalTtsStatus>({ backend: 'minimax', local_model: 'voxcpm1', loaded_model: null,
      load_state: 'disabled', error: null, models: [
        { id: 'voxcpm1', name: '第一代', available: false, loaded: false, native_sample_rate: 16000 },
        { id: 'voxcpm2', name: '第二代', available: true, loaded: false, native_sample_rate: 48000 },
      ] }), loading: ref(false), error: ref(''), refresh: vi.fn() }
  })
  function render() {
    return mount(defineComponent({ setup() { provideProductContext({ settings, setNotice: vi.fn() } as never); return () => h(TtsSettings) } }),
      { global: { stubs: { TtsVoiceLibrary: true } } })
  }
  it('语音调节缺省开启，保存关闭后保持界面与配置一致', async () => {
    const save = vi.spyOn(productApi, 'saveTts').mockResolvedValue({ settings: { tts: { speech_control_enabled: false } } } as never)
    const wrapper = render()
    const control = wrapper.get('input[aria-label="语音调节"]')
    expect((control.element as HTMLInputElement).checked).toBe(true)
    await control.setValue(false)
    await flushPromises()
    expect(save).toHaveBeenCalledWith({ speech_control_enabled: false })
    expect(settings.tts.speech_control_enabled).toBe(false)
    expect((control.element as HTMLInputElement).checked).toBe(false)
    wrapper.unmount()
  })
  it('语音调节保存失败恢复原开关，显式关闭值不会被缺省值覆盖', async () => {
    settings.tts.speech_control_enabled = false
    vi.spyOn(productApi, 'saveTts').mockRejectedValue(new Error('设置无法保存'))
    const wrapper = render()
    const control = wrapper.get('input[aria-label="语音调节"]')
    expect((control.element as HTMLInputElement).checked).toBe(false)
    await control.setValue(true)
    await flushPromises()
    expect(settings.tts.speech_control_enabled).toBe(false)
    expect((control.element as HTMLInputElement).checked).toBe(false)
    expect(wrapper.text()).toContain('设置无法保存')
    wrapper.unmount()
  })
  it('尚未加载但已具备运行条件的模型可以选择，缺失模型隐藏', () => {
    const wrapper = render()
    expect(wrapper.findAll('select option').map(item => item.attributes('value'))).toEqual(['minimax', 'voxcpm2'])
    expect(settings.tts.backend).toBe('minimax')
    wrapper.unmount()
  })
  it('隐藏失效选择不改写保存的后端或模型', () => {
    settings.tts.backend = 'voxcpm1'
    const wrapper = render()
    expect(wrapper.find('option[value="voxcpm1"]').exists()).toBe(false)
    expect(wrapper.find('tts-voice-library-stub').exists()).toBe(false)
    expect(settings.tts.backend).toBe('voxcpm1')
    expect(settings.tts.local_model).toBe('voxcpm1')
    wrapper.unmount()
  })
  it('在线 API 显示 Turbo/HD 与音色 ID；保存时提交去除首尾空白后的设置', async () => {
    const save = vi.spyOn(productApi, 'saveTts').mockResolvedValue({
      settings: { tts: { online_model: 'speech-2.8-turbo', online_voice_id: 'my-clone' } },
      message: '已保存。',
    } as never)
    const wrapper = render()
    expect(wrapper.find('.online-tts-settings').exists()).toBe(true)
    expect(wrapper.get('.tts-target').find('.online-tts-settings').exists()).toBe(false)
    expect(wrapper.get('.online-tts-settings').element.compareDocumentPosition(wrapper.get('.speech-control-row').element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(wrapper.findAll('.online-model-choice').map(button => button.text())).toEqual(['Turbo低时延', 'HD高音质'])
    await wrapper.findAll('.online-model-choice')[0].trigger('click')
    await wrapper.find('.online-voice-row input').setValue('  my-clone  ')
    await wrapper.find('.online-tts-actions .save-button').trigger('click')
    await flushPromises()
    expect(save).toHaveBeenCalledWith({ online_model: 'speech-2.8-turbo', online_voice_id: 'my-clone' })
    expect(settings.tts.online_model).toBe('speech-2.8-turbo')
    expect(settings.tts.online_voice_id).toBe('my-clone')
    wrapper.unmount()
  })
  it('试音按钮位于保存左侧，并用当前模型与音色 ID 发起默认试听', async () => {
    const save = vi.spyOn(productApi, 'saveTts').mockResolvedValue({} as never)
    const preview = vi.spyOn(productApi, 'startMinimaxTtsPreview').mockResolvedValue({
      operation_id: 'preview-1', model: 'speech-2.8-turbo', kind: 'minimax_preview', state: 'succeeded',
    } as never)
    const wrapper = render()
    await wrapper.findAll('.online-model-choice')[0].trigger('click')
    await wrapper.find('.online-voice-row input').setValue('  clone-future-1  ')
    expect(wrapper.findAll('.online-tts-buttons button').map(button => button.text())).toEqual(['试音', '保存'])
    await wrapper.find('.online-tts-buttons button').trigger('click')
    await flushPromises()
    expect(preview).toHaveBeenCalledWith('speech-2.8-turbo', 'clone-future-1')
    expect(save).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('官方音色库按钮使用固定链接打开', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = render()
    await wrapper.find('.online-voice-link').trigger('click')
    expect(open).toHaveBeenCalledWith('https://solutions.minimaxi.com/voices', '_blank', 'noopener,noreferrer')
    wrapper.unmount()
  })
  it('桌面模式通过固定原生命令打开官方音色库', async () => {
    const nativeOpen = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(desktopBridge, 'desktopPort').mockReturnValue({
      integrityStatus: async () => ({ state: 'idle', current_version: '', installer_ready: false, fields: {} }),
      available: true, openMinimaxVoiceLibrary: nativeOpen,
    } as never)
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = render()
    await wrapper.find('.online-voice-link').trigger('click')
    expect(nativeOpen).toHaveBeenCalledOnce()
    expect(nativeOpen).toHaveBeenCalledWith()
    expect(open).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('本地语音目标不显示在线模型和音色设置', () => {
    settings.tts.backend = 'voxcpm1'
    const wrapper = render()
    expect(wrapper.find('.online-tts-settings').exists()).toBe(false)
    wrapper.unmount()
  })
  it('本地合成质量固定展示三个档位，保存当前模型且保留另一模型步数', async () => {
    settings.tts.backend = 'voxcpm1'
    settings.tts.local_model = 'voxcpm2'
    settings.tts.inference_steps_by_model = { voxcpm1: 12, voxcpm2: 20 }
    const save = vi.spyOn(productApi, 'saveTts').mockResolvedValue({} as never)
    const wrapper = render()
    const choices = wrapper.findAll('.tts-quality-options button')
    expect(choices.map(button => button.text())).toEqual(['低', '中', '高'])
    expect(choices[2].attributes('aria-pressed')).toBe('true')
    expect(wrapper.find('.tts-quality summary').exists()).toBe(false)
    const target = wrapper.get('.tts-target').element
    const control = wrapper.get('.speech-control-row').element
    const quality = wrapper.get('.tts-quality').element
    const library = wrapper.get('tts-voice-library-stub').element
    expect(target.compareDocumentPosition(library) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(library.compareDocumentPosition(control) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(control.compareDocumentPosition(quality) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    for (const [index, steps] of [10, 15, 20].entries()) {
      await choices[index].trigger('click')
      await flushPromises()
      expect(save).toHaveBeenLastCalledWith({ inference_steps_by_model: { voxcpm1: 12, voxcpm2: steps } })
      expect(settings.tts.inference_steps_by_model).toEqual({ voxcpm1: 12, voxcpm2: steps })
      expect(choices[index].attributes('aria-pressed')).toBe('true')
    }
    wrapper.unmount()
  })
  it('保存合成质量期间禁用档位，失败后恢复原步数和选中状态', async () => {
    settings.tts.backend = 'voxcpm1'
    settings.tts.local_model = 'voxcpm2'
    settings.tts.inference_steps_by_model = { voxcpm1: 10, voxcpm2: 15 }
    let rejectSave!: (error: Error) => void // rejectSave（模拟保存失败）：验证在途状态与回滚。
    vi.spyOn(productApi, 'saveTts').mockImplementation(() => new Promise((_resolve, reject) => { rejectSave = reject }))
    const wrapper = render()
    const choices = wrapper.findAll('.tts-quality-options button')
    await choices[0].trigger('click')
    expect(choices.every(button => (button.element as HTMLButtonElement).disabled)).toBe(true)
    rejectSave(new Error('质量设置保存失败'))
    await flushPromises()
    expect(choices[1].attributes('aria-pressed')).toBe('true')
    expect(choices.every(button => !(button.element as HTMLButtonElement).disabled)).toBe(true)
    expect(settings.tts.inference_steps_by_model.voxcpm2).toBe(15)
    expect(wrapper.text()).toContain('质量设置保存失败')
    wrapper.unmount()
  })
  it('在线目标保留语音调节并隐藏本地合成质量', () => {
    const wrapper = render()
    expect(wrapper.find('.speech-control-row').exists()).toBe(true)
    expect(wrapper.find('.tts-quality').exists()).toBe(false)
    wrapper.unmount()
  })
})

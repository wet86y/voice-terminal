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
    settings = reactive({ tts: { backend: 'minimax', local_model: 'minimax-female-shaonv-zh' } })
    state.live = { status: ref<ProductLocalTtsStatus>({ backend: 'minimax', local_model: 'minimax-female-shaonv-zh', loaded_model: null,
      load_state: 'disabled', error: null, models: [
        { id: 'minimax-female-shaonv-zh', name: '第一代', available: false, loaded: false, native_sample_rate: 16000 },
        { id: 'tangyuge-v2pp-d78cf598', name: '第二代', available: true, loaded: false, native_sample_rate: 48000 },
      ] }), loading: ref(false), error: ref(''), refresh: vi.fn() }
  })
  function render(stubs: Record<string, unknown> = {}) {
    return mount(defineComponent({ setup() { provideProductContext({ settings, setNotice: vi.fn() } as never); return () => h(TtsSettings) } }),
      { global: { stubs: { TtsVoiceLibrary: true, ...stubs } } })
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
  it('尚未加载和缺失的模型均可浏览，但缺失模型不能保存', async () => {
    const wrapper = render()
    await wrapper.get('[aria-label="语音播放目标"] button:last-child').trigger('click')
    expect(wrapper.findAll('.tts-model-row select option').map(item => item.attributes('value'))).toEqual(['minimax-female-shaonv-zh', 'tangyuge-v2pp-d78cf598'])
    expect(wrapper.get('.tts-model-row select').text()).toContain('未就绪')
    expect(wrapper.get('button[aria-label="保存播放目标"]').attributes('disabled')).toBeDefined()
    expect(settings.tts.backend).toBe('minimax')
    wrapper.unmount()
  })
  it('切换播放目标只改变设置草稿，显式保存后才提交后端选择', async () => {
    const save = vi.spyOn(productApi, 'saveTts').mockResolvedValue({
      settings: { tts: { backend: 'aniflive', local_model: 'tangyuge-v2pp-d78cf598' } },
    } as never)
    const wrapper = render()
    await wrapper.get('[aria-label="语音播放目标"] button:last-child').trigger('click')
    await wrapper.get('.tts-model-row select').setValue('tangyuge-v2pp-d78cf598')
    expect(save).not.toHaveBeenCalled()
    expect(wrapper.find('tts-voice-library-stub').exists()).toBe(true)
    expect(wrapper.get('.tts-target').text()).toContain('当前生效：在线 API（网络语音接口）')
    expect(wrapper.get('button[aria-label="保存播放目标"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('button[aria-label="保存播放目标"]').trigger('click')
    await flushPromises()
    expect(save).toHaveBeenCalledOnce()
    expect(save).toHaveBeenCalledWith({ backend: 'aniflive', local_model: 'tangyuge-v2pp-d78cf598' })
    wrapper.unmount()
  })
  it('模型导入使用统一跳转入口，模型选择没有额外保存按钮', async () => {
    const wrapper = render()
    await wrapper.get('[aria-label="语音播放目标"] button:last-child').trigger('click')
    await wrapper.get('.tts-model-row select').setValue('tangyuge-v2pp-d78cf598')
    expect(wrapper.find('.tts-model-row .save-button').exists()).toBe(false)
    wrapper.getComponent({ name: 'TtsVoiceLibrary' }).vm.$emit('modelImport')
    await flushPromises()
    expect(wrapper.getComponent(TtsSettings).emitted('open-aniflive-components')).toHaveLength(1)
    expect(settings.tts.backend).toBe('minimax')
    wrapper.unmount()
  })
  it('播放目标切换失败时保留原后端和未保存草稿', async () => {
    vi.spyOn(productApi, 'saveTts').mockRejectedValue(new Error('模型切换失败'))
    const wrapper = render()
    await wrapper.get('[aria-label="语音播放目标"] button:last-child').trigger('click')
    await wrapper.get('.tts-model-row select').setValue('tangyuge-v2pp-d78cf598')
    await wrapper.get('button[aria-label="保存播放目标"]').trigger('click')
    await flushPromises()
    expect((wrapper.get('.tts-model-row select').element as HTMLSelectElement).value).toBe('tangyuge-v2pp-d78cf598')
    expect(settings.tts.backend).toBe('minimax')
    expect(wrapper.text()).toContain('模型切换失败')
    wrapper.unmount()
  })
  it('试听草稿模型时不隐式切换播放目标', async () => {
    let ensureSupport!: (model: 'minimax-female-shaonv-zh' | 'tangyuge-v2pp-d78cf598') => Promise<void>
    const LibraryStub = defineComponent({
      name: 'TtsVoiceLibrary',
      props: { ensureSupport: { type: Function, required: true } },
      setup(props) {
        ensureSupport = props.ensureSupport as (model: 'minimax-female-shaonv-zh' | 'tangyuge-v2pp-d78cf598') => Promise<void>
        return () => h('div', { class: 'tts-voice-library-stub' })
      },
    })
    const save = vi.spyOn(productApi, 'saveTts').mockResolvedValue({} as never)
    const wrapper = render({ TtsVoiceLibrary: LibraryStub })
    await wrapper.get('[aria-label="语音播放目标"] button:last-child').trigger('click')
    await wrapper.get('.tts-model-row select').setValue('tangyuge-v2pp-d78cf598')
    await ensureSupport('tangyuge-v2pp-d78cf598')
    expect(save).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('展示失效选择不改写保存的后端或模型', () => {
    settings.tts.backend = 'aniflive'
    const wrapper = render()
    expect(wrapper.find('option[value="minimax-female-shaonv-zh"]').text()).toContain('未就绪')
    expect(wrapper.find('tts-voice-library-stub').exists()).toBe(false)
    expect(settings.tts.backend).toBe('aniflive')
    expect(settings.tts.local_model).toBe('minimax-female-shaonv-zh')
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
  it('MiniMax 音色名随输入在线查询，未匹配或查询失败统一显示音色错误', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    let resolveSlow!: (result: { voice_id: string; voice_name: string | null }) => void
    const lookup = vi.spyOn(productApi, 'minimaxVoiceName').mockImplementation(async voiceId => {
      if (voiceId === 'known-voice') return { voice_id: voiceId, voice_name: '在线名称' }
      if (voiceId === 'failed-voice') throw new Error('在线接口暂不可用')
      if (voiceId === 'slow-voice') return await new Promise(resolve => { resolveSlow = resolve })
      if (voiceId === 'latest-voice') return { voice_id: voiceId, voice_name: '最新名称' }
      return { voice_id: voiceId, voice_name: null }
    })
    const wrapper = render()
    try {
      const input = wrapper.get('.online-voice-row input')
      await input.setValue('known-voice')
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      expect(lookup).toHaveBeenCalledWith('known-voice')
      expect(wrapper.get('.online-voice-name strong').text()).toBe('在线名称')
      await input.setValue('unknown-voice')
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      expect(wrapper.get('.online-voice-name strong').text()).toBe('音色错误')
      await input.setValue('failed-voice')
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      expect(wrapper.get('.online-voice-name strong').text()).toBe('音色错误')
      await input.setValue('slow-voice')
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      await input.setValue('latest-voice')
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      expect(wrapper.get('.online-voice-name strong').text()).toBe('最新名称')
      resolveSlow({ voice_id: 'slow-voice', voice_name: '过期名称' })
      await flushPromises()
      expect(wrapper.get('.online-voice-name strong').text()).toBe('最新名称')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
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
    settings.tts.backend = 'aniflive'
    const wrapper = render()
    expect(wrapper.find('.online-tts-settings').exists()).toBe(false)
    wrapper.unmount()
  })
  it('本地音色选项保存分块开关与等待毫秒，且不改变播放目标', async () => {
    settings.tts.backend = 'aniflive'
    settings.tts.local_model = 'tangyuge-v2pp-d78cf598'
    const save = vi.spyOn(productApi, 'saveTts').mockResolvedValue({} as never)
    const wrapper = render()
    const chunk = wrapper.get('input[aria-label="分块优化"]')
    expect((chunk.element as HTMLInputElement).checked).toBe(true)
    await chunk.setValue(false); await flushPromises()
    expect(save).toHaveBeenLastCalledWith({ aniflive_chunk_optimization: false })
    const wait = wrapper.get('input[aria-label="首音等待"]')
    expect(wait.attributes('min')).toBe('0'); expect(wait.attributes('max')).toBe('200'); expect(wait.attributes('step')).toBe('1')
    await wait.setValue('137'); await flushPromises()
    expect(save).toHaveBeenLastCalledWith({ aniflive_first_audio_wait_ms: 137 })
    expect(settings.tts.backend).toBe('aniflive')
    expect(wrapper.find('.tts-quality').exists()).toBe(false)
    wrapper.unmount()
  })
  it('保存本地选项期间禁用控件，失败后恢复参数', async () => {
    settings.tts.backend = 'aniflive'; settings.tts.local_model = 'tangyuge-v2pp-d78cf598'
    let rejectSave!: (error: Error) => void
    vi.spyOn(productApi, 'saveTts').mockImplementation(() => new Promise((_resolve, reject) => { rejectSave = reject }))
    const wrapper = render(); const control = wrapper.get('input[aria-label="显卡高性能"]')
    await control.setValue(true)
    expect(control.attributes('disabled')).toBeDefined()
    rejectSave(new Error('本地设置保存失败')); await flushPromises()
    expect((control.element as HTMLInputElement).checked).toBe(false)
    expect(control.attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('本地设置保存失败')
    wrapper.unmount()
  })
  it('在线目标保留语音调节并隐藏本地合成质量', () => {
    const wrapper = render()
    expect(wrapper.find('.speech-control-row').exists()).toBe(true)
    expect(wrapper.find('.tts-quality').exists()).toBe(false)
    wrapper.unmount()
  })
})

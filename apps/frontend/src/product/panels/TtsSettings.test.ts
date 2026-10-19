// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { defineComponent, h, reactive, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { provideProductContext } from '../product-context'
import TtsSettings from './TtsSettings.vue'
import type { ProductLocalTtsStatus, ProductTtsSettings } from '../types'

const state = vi.hoisted(() => ({ live: undefined as unknown }))
vi.mock('../useLocalTtsStatus', () => ({ useLocalTtsStatus: () => state.live }))

describe('音色设置只提供资源完整的本地模型', () => {
  let settings: { tts: ProductTtsSettings }
  beforeEach(() => {
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
})

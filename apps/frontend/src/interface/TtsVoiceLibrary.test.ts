// @vitest-environment jsdom
import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import TtsVoiceLibrary from './TtsVoiceLibrary.vue'
import { provideTtsVoiceState, type TtsVoiceState } from './useTtsVoiceState'
import type { TtsVoicePort, ProductLocalTtsModelId, ProductTtsOperation, ProductTtsVoice } from './tts-voice-types'

beforeEach(() => { vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {}) })
afterEach(() => vi.restoreAllMocks())

function harness(port?: TtsVoicePort) {
  let state!: TtsVoiceState // state（应用根层草稿状态）。
  const Host = defineComponent({
    props: { model: { type: String, default: 'voxcpm2' }, steps: { type: Number, default: 20 } },
    setup(props) {
      state = provideTtsVoiceState()
      return () => h(TtsVoiceLibrary, { model: props.model as ProductLocalTtsModelId, steps: props.steps, demo: !port, port })
    },
  })
  const wrapper = mount(Host)
  return { wrapper, get state() { return state } }
}
function button(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll('button').find(node => node.text() === label)!
}

describe('本地音色卡片', () => {
  it('两模型草稿在切换后恢复，一代隐藏设计区域', async () => {
    const view = harness()
    await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    const draft = Object.values(view.state.models.voxcpm2.drafts).find(item => !item.id)!
    draft.name = '二代草稿'; draft.description = '自然女声'
    await view.wrapper.setProps({ model: 'voxcpm1', steps: 10 })
    await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    expect(view.wrapper.text()).not.toContain('音色设计 可选')
    expect(view.wrapper.findAll('textarea')).toHaveLength(2)
    await view.wrapper.setProps({ model: 'voxcpm2', steps: 20 })
    await flushPromises()
    expect(view.wrapper.find<HTMLInputElement>('input[aria-label="音色名称"]').element.value).toBe('二代草稿')
    expect(view.wrapper.find('textarea').element.value).toBe('自然女声')
    view.wrapper.unmount()
  })

  it('设计样音固定30步，正式步数不使候选失效，修改制作正文仍须重新生成', async () => {
    const view = harness(); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    const card = view.wrapper.findAll('article').at(-1)!
    await card.find('input[maxlength="80"]').setValue('设计音色')
    await card.find('textarea').setValue('温和清晰的女声')
    expect(button(card as unknown as ReturnType<typeof mount>, '保存').attributes('disabled')).toBeDefined()
    await button(card as unknown as ReturnType<typeof mount>, '试音').trigger('click')
    await flushPromises()
    expect(button(card as unknown as ReturnType<typeof mount>, '保存').attributes('disabled')).toBeUndefined()
    await view.wrapper.setProps({ steps: 21 })
    expect(button(card as unknown as ReturnType<typeof mount>, '保存').attributes('disabled')).toBeUndefined()
    const draft = Object.values(view.state.models.voxcpm2.drafts).find(item => !item.id)!
    expect(draft.candidate?.inference_steps).toBe(30)
    await card.find('textarea[aria-label="目标样音转写"]').setValue('按实际发音校对的转写。')
    expect(button(card as unknown as ReturnType<typeof mount>, '保存').attributes('disabled')).toBeUndefined()
    await card.find('.voice-footer textarea').setValue('新的生成正文')
    expect(button(card as unknown as ReturnType<typeof mount>, '保存').attributes('disabled')).toBeDefined()
    view.wrapper.unmount()
  })

  // label（场景）、description（设计描述）、hasSource（是否含参考音频）、name（预期默认名称）。
  it.each([
    { label: '纯音色设计', description: '空灵、特殊的声音', hasSource: false, name: '设计音色' },
    { label: '混合设计', description: '温和清晰的女声', hasSource: true, name: '混合音色' },
    { label: '纯音色克隆', description: '', hasSource: true, name: '克隆音色' },
  ])('$label 支持空名称保存，并在重新加载后保留对应素材', async ({ description, hasSource, name }) => {
    let voices: ProductTtsVoice[] = [] // voices（模拟持久音色库）。
    const port = {
      ttsVoices: vi.fn(async () => ({ voices })),
      uploadTtsAsset: vi.fn().mockResolvedValue({ asset: { asset_id: 'source', duration_ms: 5680 } }),
      ttsAssetAudioUrl: vi.fn().mockReturnValue('http://localhost/reference.wav'),
      startTtsTranscribe: vi.fn().mockResolvedValue({ operation_id: 'transcribe', state: 'succeeded', result: { text: '参考音频的准确转写。' } }),
      startTtsPreview: vi.fn(async (_model, voice, text) => ({
        operation_id: 'preview', state: 'succeeded', result: { voice: {
          ...voice, source: 'design', reference_asset_id: 'target', reference_text: text,
          design_text: text, inference_steps: 30, generation_recipe: 'voxcpm2-full-decode-v2',
        } },
      })),
      createTtsVoice: vi.fn(async (_model, payload) => {
        const voice = { ...payload, id: 'saved', revision: 1, model: 'voxcpm2', enabled: false, is_default: false } as ProductTtsVoice
        voices = [voice]
        return { voice }
      }),
    } as unknown as TtsVoicePort
    const view = harness(port); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    if (hasSource) {
      await view.wrapper.find('.reference-input').trigger('drop', { dataTransfer: { files: [new File(['audio'], 'sample.wav')] } })
      await flushPromises()
    }
    await view.wrapper.find('input[aria-label="音色名称"]').setValue('   ')
    await view.wrapper.find('textarea').setValue(description)
    if (description) {
      expect(button(view.wrapper, '保存').attributes('disabled')).toBeDefined()
      await button(view.wrapper, '试音').trigger('click'); await flushPromises()
      expect(port.startTtsPreview).toHaveBeenCalledWith('voxcpm2', expect.objectContaining({
        name, design_description: description, source_reference_asset_id: hasSource ? 'source' : undefined,
      }), expect.any(String))
    }
    expect(button(view.wrapper, '保存').attributes('disabled')).toBeUndefined()
    expect(view.wrapper.text()).not.toContain('需要以当前参数完成一次试音')
    await button(view.wrapper, '保存').trigger('click'); await flushPromises()
    expect(port.createTtsVoice).toHaveBeenCalledWith('voxcpm2', expect.objectContaining({
      name, source: description ? 'design' : 'import', reference_asset_id: description ? 'target' : 'source',
    }))
    expect(port.startTtsPreview).toHaveBeenCalledTimes(description ? 1 : 0)
    expect(view.wrapper.find('.voice-name strong').text()).toBe(name)
    view.wrapper.unmount()

    const restored = harness(port); await flushPromises()
    const draft = restored.state.models.voxcpm2.drafts.saved
    expect(draft.name).toBe(name)
    expect(draft.description).toBe(description)
    expect(draft.source?.asset_id).toBe(hasSource ? 'source' : undefined)
    expect(restored.state.models.voxcpm2.voices[0].reference_asset_id).toBe(description ? 'target' : 'source')
    restored.wrapper.unmount()
  })

  it('一代克隆缺少准确转写时说明原因，填入后可直接保存', async () => {
    const view = harness()
    await view.wrapper.setProps({ model: 'voxcpm1', steps: 10 }); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    const draft = Object.values(view.state.models.voxcpm1.drafts).find(item => !item.id)!
    draft.source = { asset_id: 'source', duration_ms: 5680, filename: 'sample.wav' }
    await flushPromises()
    expect(button(view.wrapper, '保存').attributes('disabled')).toBeDefined()
    expect(view.wrapper.text()).toContain('请填写参考音频的准确转写文本后保存克隆音色。')
    await view.wrapper.find('textarea').setValue('参考音频的准确转写。')
    expect(button(view.wrapper, '保存').attributes('disabled')).toBeUndefined()
    await button(view.wrapper, '保存').trigger('click'); await flushPromises()
    expect(view.state.models.voxcpm1.voices.at(-1)).toMatchObject({
      name: '克隆音色', source: 'import', reference_asset_id: 'source', reference_text: '参考音频的准确转写。',
    })
    view.wrapper.unmount()
  })

  it('自动精修迟到不覆盖人工校对，附件在任务失败后保留', async () => {
    let resolve!: (operation: ProductTtsOperation) => void // resolve（延迟识别回执）。
    const port = {
      ttsVoices: vi.fn().mockResolvedValue({ voices: [] }),
      uploadTtsAsset: vi.fn().mockResolvedValue({ asset: { asset_id: 'source', duration_ms: 5680 } }),
      ttsAssetAudioUrl: vi.fn().mockReturnValue('http://localhost/reference.wav'),
      startTtsTranscribe: vi.fn(() => new Promise<ProductTtsOperation>(done => { resolve = done })),
      cancelTtsOperation: vi.fn().mockResolvedValue({}),
    } as unknown as TtsVoicePort
    const view = harness(port); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    const draft = Object.values(view.state.models.voxcpm2.drafts)[0]
    await view.wrapper.find('.reference-input').trigger('drop', { dataTransfer: { files: [new File(['audio'], 'sample.wav')] } })
    await flushPromises()
    const transcript = view.wrapper.findAll('textarea')[1]
    await transcript.setValue('人工校对后的正文')
    resolve({ operation_id: 'late', model: 'voxcpm2', kind: 'transcribe', state: 'succeeded', result: { asset_id: 'source', duration_ms: 5680, text: '迟到的自动文本' } })
    await flushPromises()
    expect(draft.transcript).toBe('人工校对后的正文')
    expect(draft.source?.asset_id).toBe('source')
    expect(port.cancelTtsOperation).toHaveBeenCalledWith('late')
    view.wrapper.unmount()
  })

  it('名称在标题原位编辑，保存后收起并保留已有设计素材', async () => {
    const voice = {
      id: 'designed', revision: 3, name: '原音色', source: 'design', model: 'voxcpm2', enabled: true, is_default: false,
      reference_asset_id: 'target', reference_text: '原始目标样音', design_text: '原始试音正文', design_description: '温和女声', inference_steps: 10,
    }
    const port = {
      ttsVoices: vi.fn().mockResolvedValue({ voices: [voice] }),
      updateTtsVoice: vi.fn().mockImplementation((_model, _id, payload) => {
        const saved = { ...voice, ...payload, revision: 4 }
        port.ttsVoices.mockResolvedValue({ voices: [saved] })
        return Promise.resolve({ voice: saved })
      }),
    } as unknown as TtsVoicePort & { ttsVoices: ReturnType<typeof vi.fn> }
    const view = harness(port); await flushPromises()
    await button(view.wrapper, '编辑').trigger('click')
    const card = view.wrapper.find('article')
    expect(card.findAll('input[maxlength="80"]')).toHaveLength(1)
    expect(card.find('.voice-heading input[aria-label="音色名称"]').exists()).toBe(true)
    expect(card.find('.voice-editor input[maxlength="80"]').exists()).toBe(false)
    await card.find('input[aria-label="音色名称"]').setValue('新音色')
    await button(view.wrapper, '保存').trigger('click'); await flushPromises()
    expect(port.updateTtsVoice).toHaveBeenCalledWith('voxcpm2', 'designed', expect.objectContaining({ name: '新音色', source: 'design', reference_asset_id: 'target', inference_steps: 10 }), 3)
    expect(card.find('.voice-editor').exists()).toBe(false)
    expect(card.find('.voice-name strong').text()).toBe('新音色')
    view.wrapper.unmount()
  })

  it('整行上传区与播放器互斥切换，删除附件恢复上传并清空校对文本', async () => {
    const port = {
      ttsVoices: vi.fn().mockResolvedValue({ voices: [] }),
      uploadTtsAsset: vi.fn().mockResolvedValue({ asset: { asset_id: 'source', duration_ms: 5680 } }),
      ttsAssetAudioUrl: vi.fn().mockReturnValue('http://localhost/reference.wav'),
      startTtsTranscribe: vi.fn().mockResolvedValue({ operation_id: 'recognized', state: 'succeeded', result: { text: '识别正文' } }),
    } as unknown as TtsVoicePort
    const view = harness(port); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    expect(view.wrapper.findAll('input[type="file"]')).toHaveLength(1)
    expect(view.wrapper.find('.voice-inputs input[type="file"]').exists()).toBe(false)
    expect(view.wrapper.find('.voice-reference .attachment-upload').exists()).toBe(true)
    expect(view.wrapper.findAll('textarea[rows="1"].compact-textarea')).toHaveLength(3)
    await view.wrapper.find('.reference-input').trigger('drop', { dataTransfer: { files: [new File(['audio'], 'sample.wav')] } })
    await flushPromises()
    expect(view.wrapper.find('.voice-reference audio').attributes('src')).toBe('http://localhost/reference.wav')
    expect(view.wrapper.find('input[type="file"]').exists()).toBe(false)
    await button(view.wrapper, '开始校对文本').trigger('click'); await flushPromises()
    expect(port.startTtsTranscribe).toHaveBeenCalledTimes(2)
    await view.wrapper.find('button[aria-label="移除参考音频"]').trigger('click')
    expect(view.wrapper.find('.audio-attachment').exists()).toBe(false)
    expect(view.wrapper.find('.voice-reference input[type="file"]').exists()).toBe(true)
    expect(view.wrapper.findAll('textarea')[1].element.value).toBe('')
    view.wrapper.unmount()
  })
})

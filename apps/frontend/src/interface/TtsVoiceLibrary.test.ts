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
    props: { model: { type: String, default: 'aniflive' }, steps: { type: Number, default: 20 } },
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
  it('基底和微调共用参考草稿，界面仅展示参考克隆', async () => {
    const view = harness(); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    const draft = Object.values(view.state.models.aniflive.drafts).find(item => !item.id)!
    draft.name = '共享参考'; draft.transcript = '准确的参考正文。'
    await view.wrapper.setProps({ model: 'aniflive' }); await flushPromises()
    expect(view.wrapper.find('.voice-design').exists()).toBe(false)
    expect(view.wrapper.findAll('textarea')).toHaveLength(2)
    expect(view.wrapper.find<HTMLInputElement>('input[aria-label="音色名称"]').element.value).toBe('共享参考')
    expect(view.wrapper.text()).toContain('3–10 秒')
    expect(view.wrapper.text()).not.toContain('推理步数')
    view.wrapper.unmount()
  })
  it('缺少参考录音时不能保存或试音', async () => {
    const view = harness(); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    await view.wrapper.find('textarea').setValue('只有文字，没有录音。')
    expect(button(view.wrapper, '保存').attributes('disabled')).toBeDefined()
    expect(button(view.wrapper, '合成试音').attributes('disabled')).toBeDefined()
    expect(view.wrapper.text()).toContain('请添加参考音频')
    view.wrapper.unmount()
  })

  it.each(['zh', 'en', 'ko'])('参考语言 %s 与录音、转写共同保存并可重载', async language => {
    let voices: ProductTtsVoice[] = []
    const port = {
      ttsVoices: vi.fn(async () => ({ voices })),
      uploadTtsAsset: vi.fn().mockResolvedValue({ asset: { asset_id: 'source', duration_ms: 5680 } }),
      ttsAssetAudioUrl: vi.fn().mockReturnValue('http://localhost/reference.wav'),
      startTtsTranscribe: vi.fn().mockResolvedValue({ operation_id: 'transcribe', state: 'succeeded', result: { text: '参考音频的准确转写。' } }),
      createTtsVoice: vi.fn(async (_model, payload) => {
        const voice = { ...payload, id: 'saved', revision: 1, model: 'aniflive', enabled: false, is_default: false } as ProductTtsVoice
        voices = [voice]; return { voice }
      }),
    } as unknown as TtsVoicePort
    const view = harness(port); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    await view.wrapper.find('.reference-input').trigger('drop', { dataTransfer: { files: [new File(['audio'], 'sample.wav')] } }); await flushPromises()
    await view.wrapper.find('input[aria-label="音色名称"]').setValue('   ')
    await view.wrapper.find('select').setValue(language)
    expect(button(view.wrapper, '保存').attributes('disabled')).toBeUndefined()
    await button(view.wrapper, '保存').trigger('click'); await flushPromises()
    expect(port.createTtsVoice).toHaveBeenCalledWith('aniflive', expect.objectContaining({
      name: '克隆音色', source: 'import', reference_asset_id: 'source', reference_language: language,
      reference_text: '参考音频的准确转写。',
    }))
    view.wrapper.unmount()
    const restored = harness(port); await flushPromises()
    expect(restored.state.models.aniflive.drafts.saved.source?.asset_id).toBe('source')
    expect(restored.state.models.aniflive.drafts.saved.referenceLanguage).toBe(language)
    restored.wrapper.unmount()
  })

  it('参考克隆缺少准确转写时说明原因，填入后可直接保存', async () => {
    const view = harness()
    await view.wrapper.setProps({ model: 'aniflive', steps: 10 }); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    const draft = Object.values(view.state.models.aniflive.drafts).find(item => !item.id)!
    draft.source = { asset_id: 'source', duration_ms: 5680, filename: 'sample.wav' }
    await flushPromises()
    expect(button(view.wrapper, '保存').attributes('disabled')).toBeDefined()
    expect(view.wrapper.text()).toContain('请填写参考音频的准确转写文本后保存克隆音色。')
    await view.wrapper.find('textarea').setValue('参考音频的准确转写。')
    expect(button(view.wrapper, '保存').attributes('disabled')).toBeUndefined()
    await button(view.wrapper, '保存').trigger('click'); await flushPromises()
    expect(view.state.models.aniflive.voices.at(-1)).toMatchObject({
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
    const draft = Object.values(view.state.models.aniflive.drafts)[0]
    await view.wrapper.find('.reference-input').trigger('drop', { dataTransfer: { files: [new File(['audio'], 'sample.wav')] } })
    await flushPromises()
    const transcript = view.wrapper.findAll('textarea')[0]
    await transcript.setValue('人工校对后的正文')
    resolve({ operation_id: 'late', model: 'aniflive', kind: 'transcribe', state: 'succeeded', result: { asset_id: 'source', duration_ms: 5680, text: '迟到的自动文本' } })
    await flushPromises()
    expect(draft.transcript).toBe('人工校对后的正文')
    expect(draft.source?.asset_id).toBe('source')
    expect(port.cancelTtsOperation).toHaveBeenCalledWith('late')
    view.wrapper.unmount()
  })

  it('名称在标题原位编辑，保存后收起并保留原参考录音及语言', async () => {
    const voice = {
      id: 'designed', revision: 3, name: '原音色', source: 'import', model: 'aniflive', enabled: true, is_default: false,
      reference_asset_id: 'target', reference_text: '原始参考转写', reference_language: 'ja',
    }
    const port = {
      ttsVoices: vi.fn().mockResolvedValue({ voices: [voice] }),
      ttsAssetAudioUrl: vi.fn().mockReturnValue("http://localhost/reference.wav"),
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
    expect(port.updateTtsVoice).toHaveBeenCalledWith('aniflive', 'designed', expect.objectContaining({ name: '新音色', source: 'import', reference_asset_id: 'target', reference_language: 'ja' }), 3)
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
    expect(view.wrapper.findAll('textarea[rows="1"].compact-textarea')).toHaveLength(2)
    await view.wrapper.find('.reference-input').trigger('drop', { dataTransfer: { files: [new File(['audio'], 'sample.wav')] } })
    await flushPromises()
    expect(view.wrapper.find('.voice-reference audio').attributes('src')).toBe('http://localhost/reference.wav')
    expect(view.wrapper.find('input[type="file"]').exists()).toBe(false)
    await button(view.wrapper, '开始校对文本').trigger('click'); await flushPromises()
    expect(port.startTtsTranscribe).toHaveBeenCalledTimes(2)
    await view.wrapper.find('button[aria-label="移除参考音频"]').trigger('click')
    expect(view.wrapper.find('.audio-attachment').exists()).toBe(false)
    expect(view.wrapper.find('.voice-reference input[type="file"]').exists()).toBe(true)
    expect(view.wrapper.findAll('textarea')[0].element.value).toBe('')
    view.wrapper.unmount()
  })
})

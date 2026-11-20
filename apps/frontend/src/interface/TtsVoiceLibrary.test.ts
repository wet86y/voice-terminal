// @vitest-environment jsdom
import { defineComponent, h, ref, type Ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import TtsVoiceLibrary from './TtsVoiceLibrary.vue'
import { provideTtsVoiceState, type TtsVoiceState } from './useTtsVoiceState'
import type { TtsVoicePort, ProductLocalTtsModelId, ProductTtsOperation, ProductTtsVoice, RemoteTtsAssetImportPort } from './tts-voice-types'
import { nativeDeviceApi } from '../product/native-device-api'

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  Reflect.deleteProperty(window, '__VOICE_APPLICATION__')
})

function remoteVoice(assetId = 'shared-asset'): ProductTtsVoice {
  return {
    id: 'remote-voice', revision: 1, name: '远程音色', source: 'legacy', model: 'aniflive',
    is_default: false, enabled: false, reference_asset_id: assetId, reference_text: '基础参考转写',
    source_reference_asset_id: assetId, source_reference_text: '基础参考转写',
    references: [{ id: 'remote-expression', name: '温柔', tag: 'soft', intensity: 0.5,
      asset_id: assetId, source_asset_id: null, reference_text: '情绪参考转写', reference_language: 'zh', status: 'ready' }],
  }
}

function harness(port?: TtsVoicePort, remoteTtsAssetImport?: RemoteTtsAssetImportPort,
  remoteAudioScope?: Ref<string>) {
  let state!: TtsVoiceState // state（应用根层草稿状态）。
  const Host = defineComponent({
    props: { model: { type: String, default: 'aniflive' }, steps: { type: Number, default: 20 } },
    setup(props) {
      state = provideTtsVoiceState()
      return () => h(TtsVoiceLibrary, { model: props.model as ProductLocalTtsModelId, steps: props.steps,
        demo: !port, port, remoteMode: window.__VOICE_APPLICATION__?.remote_mode === true,
        remoteTtsAssetImport, remoteAudioScope: remoteAudioScope?.value })
    },
  })
  const wrapper = mount(Host)
  return { wrapper, get state() { return state } }
}
function button(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll('button').find(node => node.text() === label)!
}

describe('本地音色卡片', () => {
  it('独立参考可设为默认或取消，未登记时明确提示且不启用验收录音', async () => {
    let voice = { id: 'library', revision: 1, name: '模型参考音频', source: 'legacy', model: 'aniflive',
      is_default: true, enabled: false, reference_asset_id: 'validation-only', reference_text: '验收参考',
      default_reference_registered: false, default_reference_id: 'seed', references: [
        { id: 'neutral', name: '平实', tag: 'neutral', asset_id: 'audio-neutral', reference_text: '平实转写', reference_language: 'zh', intensity: .5, status: 'ready' },
        { id: 'soft', name: '温柔', tag: 'soft', asset_id: 'audio-soft', reference_text: '温柔转写', reference_language: 'zh', intensity: .5, status: 'ready' },
      ] } as ProductTtsVoice
    const setDefault = vi.fn(async (_model, _voice, reference: string | null) => {
      const row = voice.references!.find(item => item.id === reference)
      voice = { ...voice, revision: voice.revision + 1, default_reference_registered: !!row,
        ...(row ? { default_reference_id: row.id, reference_asset_id: row.asset_id!, reference_text: row.reference_text } : {}) }
      return { voice }
    })
    const port = { ttsVoices: vi.fn(async () => ({ voices: [voice] })), setDefaultTtsReference: setDefault,
      ttsAssetAudioUrl: vi.fn().mockReturnValue('http://localhost/reference.wav') } as unknown as TtsVoicePort
    const view = harness(port); await flushPromises()
    expect(view.wrapper.text()).toContain('模型默认合成参考未登记')
    expect(view.state.models.aniflive.drafts.library.source).toBeNull()
    await button(view.wrapper, '编辑').trigger('click')
    expect(view.wrapper.text()).toContain('已就绪 2 · 待完善 0')
    const selector = () => view.wrapper.get('.expression-default-toggle input[role="switch"]')
    await selector().setValue(true); await flushPromises()
    expect(setDefault).toHaveBeenLastCalledWith('aniflive', 'library', 'neutral', 1)
    expect(selector().attributes('aria-checked')).toBe('true')
    expect(view.wrapper.text()).toContain('从情绪候选中排除')
    expect(view.state.models.aniflive.drafts.library.references).toHaveLength(2)
    await selector().setValue(false); await flushPromises()
    expect(setDefault).toHaveBeenLastCalledWith('aniflive', 'library', null, 2)
    expect(view.wrapper.text()).toContain('模型默认合成参考未登记')
    expect(view.state.models.aniflive.drafts.library.references).toHaveLength(2)
    view.wrapper.unmount()
  })
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

  it('主卡上传后保留播放器，隐藏直接删除和转写入口', async () => {
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
    expect(button(view.wrapper, '开始校对文本')).toBeUndefined()
    expect(view.wrapper.find('button[aria-label="移除参考音频"]').exists()).toBe(false)
    expect(port.startTtsTranscribe).toHaveBeenCalledOnce()
    expect(view.wrapper.find('.audio-attachment').exists()).toBe(true)
    view.wrapper.unmount()
  })
})

describe('远程音色卡片文件导入', () => {
  beforeEach(() => {
    window.__VOICE_APPLICATION__ = { managed: true, origin: '', voice_api_url: '', voice_ws_url: '', remote_mode: true }
  })

  it('基础参考通过客户端原生选择器上传，不显示浏览器文件输入', async () => {
    const port = {
      ttsVoices: vi.fn().mockResolvedValue({ voices: [] }),
      ttsAssetAudioUrl: vi.fn().mockReturnValue('http://localhost/reference.wav'),
      startTtsTranscribe: vi.fn().mockResolvedValue({ operation_id: 'transcribe', state: 'succeeded', result: { text: '远程参考转写' } }),
      uploadTtsAsset: vi.fn(),
    } as unknown as TtsVoicePort
    const importAsset = vi.spyOn(nativeDeviceApi, 'remoteTtsAssetImport').mockResolvedValue({
      asset: { asset_id: 'remote-reference', duration_ms: 5680 },
    })
    const view = harness(port, nativeDeviceApi.remoteTtsAssetImport); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')

    expect(view.wrapper.findAll('input[type="file"]')).toHaveLength(0)
    expect(view.wrapper.find('button.attachment-upload').exists()).toBe(true)
    await view.wrapper.find('button.attachment-upload').trigger('click')
    await flushPromises()

    expect(importAsset).toHaveBeenCalledWith('aniflive', 'asset')
    expect(port.uploadTtsAsset).not.toHaveBeenCalled()
    expect(port.startTtsTranscribe).toHaveBeenCalledWith('aniflive', 'remote-reference')
    expect(view.state.models.aniflive.drafts[Object.keys(view.state.models.aniflive.drafts)[0]!]!.source?.asset_id)
      .toBe('remote-reference')
    view.wrapper.unmount()
  })

  it('取消基础参考文件选择时保留草稿且不启动转写', async () => {
    const port = {
      ttsVoices: vi.fn().mockResolvedValue({ voices: [] }),
      ttsAssetAudioUrl: vi.fn().mockReturnValue('http://localhost/reference.wav'),
      startTtsTranscribe: vi.fn(),
      uploadTtsAsset: vi.fn(),
    } as unknown as TtsVoicePort
    const importAsset = vi.spyOn(nativeDeviceApi, 'remoteTtsAssetImport').mockResolvedValue({
      cancelled: true, fields: { cancelled: '用户取消文件选择' },
    })
    const view = harness(port, nativeDeviceApi.remoteTtsAssetImport); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    const draft = view.state.models.aniflive.drafts[Object.keys(view.state.models.aniflive.drafts)[0]!]!
    const previousName = draft.name

    await view.wrapper.find('button.attachment-upload').trigger('click')
    await flushPromises()

    expect(importAsset).toHaveBeenCalledWith('aniflive', 'asset')
    expect(draft.source).toBeNull()
    expect(draft.name).toBe(previousName)
    expect(port.startTtsTranscribe).not.toHaveBeenCalled()
    expect(draft.error).toBe('')
    view.wrapper.unmount()
  })

  it('情绪参考通过一次原生选择同时导入可裁剪原件和参考资产', async () => {
    const port = {
      ttsVoices: vi.fn().mockResolvedValue({ voices: [] }),
      ttsAssetAudioUrl: vi.fn().mockReturnValue('http://localhost/reference.wav'),
      startTtsTranscribe: vi.fn().mockResolvedValue({ operation_id: 'transcribe', state: 'succeeded', result: { text: '情绪参考转写' } }),
      uploadTtsAsset: vi.fn(), uploadTtsSource: vi.fn(),
    } as unknown as TtsVoicePort
    const importResponse = {
      asset: { asset_id: 'remote-asset', duration_ms: 5680 },
      source: { asset_id: 'remote-source', duration_ms: 8000 },
    }
    const importAsset = vi.spyOn(nativeDeviceApi, 'remoteTtsAssetImport').mockResolvedValue(importResponse)
    const view = harness(port, nativeDeviceApi.remoteTtsAssetImport); await flushPromises()
    await button(view.wrapper, '＋ 添加音色').trigger('click')
    await button(view.wrapper, '＋ 添加情绪参考').trigger('click')
    await view.wrapper.find('button.expression-expand').trigger('click')

    expect(view.wrapper.findAll('input[type="file"]')).toHaveLength(0)
    const importButton = view.wrapper.findAll('button').find(node => node.text().includes('从客户端导入参考录音'))
    expect(importButton).toBeDefined()
    await importButton!.trigger('click')
    await flushPromises()

    expect(importAsset).toHaveBeenCalledOnce()
    expect(importAsset).toHaveBeenCalledWith('aniflive', 'source_and_asset')
    expect(port.uploadTtsSource).not.toHaveBeenCalled()
    expect(port.uploadTtsAsset).not.toHaveBeenCalled()
    expect(view.state.models.aniflive.drafts[Object.keys(view.state.models.aniflive.drafts)[0]!]!.references[0])
      .toMatchObject({ source_asset_id: 'remote-source', asset_id: 'remote-asset', status: 'ready' })
    view.wrapper.unmount()
  })
})

describe('远程参考音频试听', () => {
  const operation: ProductTtsOperation = { model: 'aniflive', kind: 'play', operation_id: 'play-1', state: 'running' }

  beforeEach(() => {
    window.__VOICE_APPLICATION__ = { managed: true, origin: '', voice_api_url: '', voice_ws_url: '', remote_mode: true }
  })

  it('主参考及情绪参考均只提交素材编号，交给程序播放器', async () => {
    const voice = remoteVoice()
    voice.references![0]!.asset_id = null
    voice.references![0]!.source_asset_id = 'source-recording'
    const startTtsPlay = vi.fn().mockResolvedValue({ ...operation, state: 'succeeded' })
    const ttsAssetAudioUrl = vi.fn()
    const port = { ttsVoices: vi.fn().mockResolvedValue({ voices: [voice] }), startTtsPlay, ttsAssetAudioUrl } as unknown as TtsVoicePort
    const view = harness(port)
    await flushPromises()
    await button(view.wrapper, '编辑').trigger('click')
    await view.wrapper.get('.voice-reference .audio-play').trigger('click')
    await flushPromises()
    await view.wrapper.get('.expression-summary .audio-play').trigger('click')
    await flushPromises()

    expect(startTtsPlay.mock.calls).toEqual([['aniflive', 'shared-asset'], ['aniflive', 'source-recording']])
    expect(ttsAssetAudioUrl).not.toHaveBeenCalled()
    expect(view.wrapper.find('audio').exists()).toBe(false)
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled()
    view.wrapper.unmount()
  })

  it('可在播放回执到达前点击停止，收到回执后取消同一个任务', async () => {
    let resolvePlay!: (response: ProductTtsOperation) => void
    const startTtsPlay = vi.fn(() => new Promise<ProductTtsOperation>(resolve => { resolvePlay = resolve }))
    const cancelTtsOperation = vi.fn().mockResolvedValue({ ...operation, state: 'cancelled' })
    const port = { ttsVoices: vi.fn().mockResolvedValue({ voices: [remoteVoice()] }),
      startTtsPlay, cancelTtsOperation } as unknown as TtsVoicePort
    const view = harness(port)
    await flushPromises()
    await button(view.wrapper, '编辑').trigger('click')

    const play = view.wrapper.get('.voice-reference .audio-play')
    await play.trigger('click')
    expect(play.attributes('aria-label')).toBe('停止参考音频')
    await play.trigger('click')
    resolvePlay(operation)
    await flushPromises()

    expect(cancelTtsOperation).toHaveBeenCalledExactlyOnceWith('play-1')
    expect(play.attributes('aria-label')).toBe('播放参考音频')
    view.wrapper.unmount()
  })

  it('切换连接后不向新服务端查询或取消旧试听任务', async () => {
    const scope = ref('host-a:1')
    let resolvePlay!: (response: ProductTtsOperation) => void
    const startTtsPlay = vi.fn(() => new Promise<ProductTtsOperation>(resolve => { resolvePlay = resolve }))
    const getTtsOperation = vi.fn()
    const cancelTtsOperation = vi.fn()
    const port = { ttsVoices: vi.fn().mockResolvedValue({ voices: [remoteVoice()] }),
      startTtsPlay, getTtsOperation, cancelTtsOperation } as unknown as TtsVoicePort
    const view = harness(port, undefined, scope)
    await flushPromises()
    await button(view.wrapper, '编辑').trigger('click')
    await view.wrapper.get('.voice-reference .audio-play').trigger('click')

    scope.value = 'host-b:1'
    await flushPromises()
    resolvePlay(operation)
    await flushPromises()

    expect(getTtsOperation).not.toHaveBeenCalled()
    expect(cancelTtsOperation).not.toHaveBeenCalled()
    expect(view.wrapper.get('.voice-reference .audio-play').attributes('aria-label')).toBe('播放参考音频')
    view.wrapper.unmount()
  })
})

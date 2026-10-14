// @vitest-environment jsdom
import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { describe, it, expect, vi } from 'vitest'
import TtsVoiceLibrary from './TtsVoiceLibrary.vue'
import { provideTtsVoiceState, type TtsVoiceState } from './useTtsVoiceState'
import type { TtsVoicePort, ProductLocalTtsModelId, ProductTtsOperation } from './tts-voice-types'

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
    expect(view.wrapper.text()).toContain('二代草稿')
    expect(view.wrapper.find('textarea').element.value).toBe('自然女声')
    view.wrapper.unmount()
  })

  it('设计必须试音后保存，修改正文或步数使候选失效', async () => {
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
    expect(button(card as unknown as ReturnType<typeof mount>, '保存').attributes('disabled')).toBeDefined()
    await button(card as unknown as ReturnType<typeof mount>, '试音').trigger('click')
    await card.findAll('textarea').at(-1)!.setValue('新的生成正文')
    expect(button(card as unknown as ReturnType<typeof mount>, '保存').attributes('disabled')).toBeDefined()
    view.wrapper.unmount()
  })

  it('自动精修迟到不覆盖人工校对，附件在任务失败后保留', async () => {
    let resolve!: (operation: ProductTtsOperation) => void // resolve（延迟识别回执）。
    const port = {
      ttsVoices: vi.fn().mockResolvedValue({ voices: [] }),
      uploadTtsAsset: vi.fn().mockResolvedValue({ asset: { asset_id: 'source', duration_ms: 5680 } }),
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
})

// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ReferenceAudioPlayer from './ReferenceAudioPlayer.vue'

afterEach(() => vi.restoreAllMocks())

describe('参考音频播放控件', () => {
  it('播放、暂停和拖动进度使用真实媒体状态，失去焦点或卸载会暂停', async () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function (this: HTMLMediaElement) {
      this.dispatchEvent(new Event('play')); return Promise.resolve()
    })
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(function (this: HTMLMediaElement) { this.dispatchEvent(new Event('pause')) })
    const wrapper = mount(ReferenceAudioPlayer, { props: { src: '/reference.wav', filename: '参考.wav', durationMs: 6000, busy: false, transcribing: false, active: true } })
    const audio = wrapper.find('audio')
    Object.defineProperty(audio.element, 'duration', { value: 8, configurable: true })
    await audio.trigger('loadedmetadata')
    await wrapper.find('button[aria-label="播放参考音频"]').trigger('click')
    expect(play).toHaveBeenCalledOnce()
    expect(wrapper.emitted('play')).toHaveLength(1)
    expect(wrapper.find('button[aria-label="暂停参考音频"]').exists()).toBe(true)
    audio.element.currentTime = 2
    await audio.trigger('timeupdate')
    expect(wrapper.find('input').attributes('aria-valuetext')).toBe('0:02 / 0:08')
    await wrapper.find('input').setValue('5')
    expect(audio.element.currentTime).toBe(5)
    await wrapper.find('button[aria-label="暂停参考音频"]').trigger('click')
    expect(pause).toHaveBeenCalledTimes(1)
    await wrapper.find('button[aria-label="播放参考音频"]').trigger('click')
    await wrapper.setProps({ active: false })
    expect(pause).toHaveBeenCalledTimes(2)
    wrapper.unmount()
    expect(pause).toHaveBeenCalledTimes(3)
  })

  it('播放失败有明确反馈，忙碌时禁止重复校对和删除', async () => {
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new Error('无法读取音频'))
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    const wrapper = mount(ReferenceAudioPlayer, { props: { src: '/missing.wav', filename: '参考.wav', durationMs: 5680, busy: true, transcribing: true, active: true } })
    await wrapper.find('button[aria-label="播放参考音频"]').trigger('click')
    expect(wrapper.emitted('error')?.[0]).toEqual(['参考音频暂时无法播放，请重试或重新上传。'])
    expect(wrapper.find('.transcribe-action').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.audio-remove').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})

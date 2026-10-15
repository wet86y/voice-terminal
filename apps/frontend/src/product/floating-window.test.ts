// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import VoiceOrb from '../interface/VoiceOrb.vue'
import FloatingWindowShell from './FloatingWindowShell.vue'
import FloatingCaption from './FloatingCaption.vue'
import { EMPTY_FLOATING_WINDOW_MODEL, readFloatingCaption, type FloatingWindowModel } from './floating-window'

const mounted: ReturnType<typeof mount>[] = []
beforeEach(() => vi.useFakeTimers())
afterEach(() => { mounted.splice(0).forEach(wrapper => wrapper.unmount()); vi.clearAllTimers(); vi.useRealTimers() })
function shell(model: Partial<FloatingWindowModel> = {}) {
  const wrapper = mount(FloatingWindowShell, {
    props: { model: { ...EMPTY_FLOATING_WINDOW_MODEL, canControl: true, ...model } },
    slots: { content: ({ model: value }: { model: FloatingWindowModel }) => h(VoiceOrb, { model: value }) },
  })
  mounted.push(wrapper)
  return wrapper
}
async function fire(element: Element, type: string, init: MouseEventInit & { pointerId?: number } = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init })
  if (init.pointerId !== undefined) Object.defineProperty(event, 'pointerId', { value: init.pointerId })
  element.dispatchEvent(event)
  await nextTick()
}

describe('悬浮窗动效扩展接口', () => {
  it('复用同一语音动效的五频带归一化与运行状态映射', async () => {
    const wrapper = mount(VoiceOrb, { props: { model: {
      state: 'listening', sessionActive: true, audioLevels: [-1, 0.2, 0.5, 1, 2],
    } } })

    expect(wrapper.get('.orb-stage').classes()).toContain('orb-listening')
    expect(wrapper.findAll('.voice-waveform i').map(bar => bar.attributes('style')))
      .toEqual([
        '--voice-level: 0;', '--voice-level: 0.2;', '--voice-level: 0.5;', '--voice-level: 1;', '--voice-level: 1;',
      ])
    await wrapper.setProps({ model: { state: 'thinking', sessionActive: true, audioLevels: [1, 1, 1, 1, 1] } })
    expect(wrapper.findAll('.voice-waveform i').every(bar => bar.attributes('style')?.includes('--voice-level: 0;'))).toBe(true)
    expect(wrapper.get('.orb-stage').classes()).toContain('orb-thinking')
  })

  it('内容插槽收到类型化模型，短按不触发拖动', async () => {
    const model: FloatingWindowModel = {
      ...EMPTY_FLOATING_WINDOW_MODEL,
      revision: 3, state: 'speaking', sessionActive: true, audioLevels: [0.1, 0.2, 0.3, 0.4, 0.5],
    }
    const wrapper = mount(FloatingWindowShell, {
      props: { model },
      slots: { content: ({ model: slotModel }: { model: FloatingWindowModel }) => h('span', { 'data-state': slotModel.state }) },
    })

    expect(wrapper.get('[data-state="speaking"]').attributes('data-state')).toBe('speaking')
    mounted.push(wrapper)
    await fire(wrapper.get('button').element, 'pointerdown', { button: 0 })
    await fire(wrapper.get('button').element, 'pointerup', { button: 0 })
    await fire(wrapper.get('button').element, 'dblclick', { button: 0 })
    expect(wrapper.emitted('drag')).toBeUndefined()
    expect(wrapper.emitted('returnToWorkbench')).toHaveLength(1)
  })

  it('单击延后确认，双击只回工作台而不切换麦克风', async () => {
    const wrapper = shell()
    await fire(wrapper.get('button').element, 'click', { button: 0, detail: 1 })
    expect(wrapper.emitted('toggleSilence')).toBeUndefined()
    await vi.advanceTimersByTimeAsync(100)
    await fire(wrapper.get('button').element, 'click', { button: 0, detail: 2 })
    await fire(wrapper.get('button').element, 'dblclick', { button: 0, detail: 2 })
    await vi.advanceTimersByTimeAsync(600)
    expect(wrapper.emitted('toggleSilence')).toBeUndefined()
    expect(wrapper.emitted('returnToWorkbench')).toHaveLength(1)
    await fire(wrapper.get('button').element, 'click', { button: 0, detail: 1 })
    await vi.advanceTimersByTimeAsync(500)
    expect(wrapper.emitted('toggleSilence')).toHaveLength(1)
  })

  it('悬浮主体长按四百毫秒才拖动，松手不触发静默', async () => {
    const wrapper = shell()
    await fire(wrapper.get('.voice-orb').element, 'pointerdown', { button: 0, pointerId: 1 })
    await vi.advanceTimersByTimeAsync(399)
    expect(wrapper.emitted('drag')).toBeUndefined()
    await vi.advanceTimersByTimeAsync(1)
    expect(wrapper.emitted('drag')).toHaveLength(1)
    await fire(wrapper.get('button').element, 'pointerup', { button: 0, pointerId: 1 })
    await fire(wrapper.get('button').element, 'click', { button: 0, detail: 1 })
    await vi.advanceTimersByTimeAsync(600)
    expect(wrapper.emitted('toggleSilence')).toBeUndefined()
  })

  it('遵守系统慢速双击设置，超过五百毫秒的双击也不先切换静默', async () => {
    const wrapper = shell({ doubleClickMs: 900 })
    await fire(wrapper.get('button').element, 'click', { button: 0, detail: 1 })
    await vi.advanceTimersByTimeAsync(700)
    expect(wrapper.emitted('toggleSilence')).toBeUndefined()
    await fire(wrapper.get('button').element, 'click', { button: 0, detail: 2 })
    await fire(wrapper.get('button').element, 'dblclick', { button: 0, detail: 2 })
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.emitted('toggleSilence')).toBeUndefined()
    expect(wrapper.emitted('returnToWorkbench')).toHaveLength(1)
  })

  it('右键、失焦和卸载不会误触操作，右键默认菜单被阻止', async () => {
    const wrapper = shell()
    await fire(wrapper.get('button').element, 'pointerdown', { button: 2 })
    await fire(wrapper.get('button').element, 'click', { button: 2 })
    await vi.advanceTimersByTimeAsync(600)
    expect(wrapper.emitted('drag')).toBeUndefined()
    expect(wrapper.emitted('toggleSilence')).toBeUndefined()
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true })
    expect(wrapper.get('button').element.dispatchEvent(event)).toBe(false)
    await fire(wrapper.get('button').element, 'pointerdown', { button: 0 })
    window.dispatchEvent(new Event('blur'))
    await vi.advanceTimersByTimeAsync(600)
    expect(wrapper.emitted('drag')).toBeUndefined()
    wrapper.unmount()
  })

  it('字幕结束后自动收起，音量快照不延长气泡停留时间', async () => {
    const caption = { key: 'input:a', role: 'user' as const, text: '刚刚识别的文字', focusStart: 4, focusEnd: 7, active: true }
    const wrapper = shell({ caption })
    expect(wrapper.find('.floating-caption-user .floating-caption-bubble').exists()).toBe(true)
    await wrapper.setProps({ model: { ...wrapper.props('model'), caption: { ...caption, active: false } } })
    await vi.advanceTimersByTimeAsync(2000)
    await wrapper.setProps({ model: { ...wrapper.props('model'), caption: { ...caption, active: false }, audioLevels: [1, 0, 0, 0, 0] } })
    await vi.advanceTimersByTimeAsync(501)
    expect(wrapper.find('.floating-caption').exists()).toBe(false)
  })

  it('单行字幕随当前片段居中，并把原文换行归为等长空白', async () => {
    const wrapper = mount(FloatingCaption, { props: { caption: {
      key: 'speech:a', role: 'assistant', text: '前文\n当前\n后文', focusStart: 3, focusEnd: 5, active: true,
    } } })
    mounted.push(wrapper)
    const viewport = wrapper.get('.floating-caption-viewport').element as HTMLElement
    const focus = wrapper.get('.floating-caption-focus').element as HTMLElement
    Object.defineProperties(viewport, { clientWidth: { value: 80 }, scrollWidth: { value: 500 } })
    Object.defineProperties(focus, { offsetLeft: { value: 200 }, offsetWidth: { value: 24 } })
    const scroll = vi.fn()
    viewport.scrollTo = scroll
    await wrapper.setProps({ caption: { ...wrapper.props('caption'), focusEnd: 6 } })
    await vi.advanceTimersByTimeAsync(0)
    expect(scroll).toHaveBeenLastCalledWith({ left: 172, behavior: 'smooth' })
    expect(wrapper.get('.floating-caption-track').text()).toBe('前文 当前 后文')
    expect(wrapper.find('.floating-caption-assistant').exists()).toBe(true)
  })

  it('截图附件、工具消息、异常下标和空字幕在接收边界被丢弃', () => {
    const valid = { key: 'input:a', role: 'user', text: '你好', focusStart: 0, focusEnd: 2, active: true }
    expect(readFloatingCaption(valid)?.text).toBe('你好')
    for (const invalid of [null, {}, { ...valid, role: 'tool' }, { ...valid, text: null },
      { ...valid, attachments: [{ kind: 'screenshot' }] }, { ...valid, text: ' ' },
      { ...valid, focusEnd: NaN }, { ...valid, focusStart: -1 }, { ...valid, focusEnd: 99 }]) {
      expect(readFloatingCaption(invalid)).toBeNull()
    }
  })
})

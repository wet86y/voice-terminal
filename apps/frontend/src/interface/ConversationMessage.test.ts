// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import ConversationMessage from './ConversationMessage.vue'

const mounted: VueWrapper[] = []
let now = 0, nextFrame = 0, hidden = false
let frames = new Map<number, FrameRequestCallback>()
let motion: { matches: boolean; addEventListener: EventTarget['addEventListener']; removeEventListener: EventTarget['removeEventListener'] }
let motionEvents: EventTarget

beforeEach(() => {
  now = nextFrame = 0
  hidden = false
  frames = new Map()
  motionEvents = new EventTarget()
  motion = { matches: false, addEventListener: motionEvents.addEventListener.bind(motionEvents), removeEventListener: motionEvents.removeEventListener.bind(motionEvents) }
  vi.stubGlobal('matchMedia', vi.fn(() => motion))
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => { frames.set(++nextFrame, callback); return nextFrame }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => { frames.delete(id) }))
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
})
afterEach(() => {
  mounted.splice(0).forEach(wrapper => wrapper.unmount())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
function render(text: string, extra: Record<string, unknown> = {}) {
  const wrapper = mount(ConversationMessage, { props: { role: 'assistant', text, ...extra } })
  mounted.push(wrapper)
  return wrapper
}
const visible = (wrapper: VueWrapper) => wrapper.get('.message-text-visible').text()
async function advance(milliseconds: number) {
  while (milliseconds > 0) {
    const elapsed = Math.min(16, milliseconds)
    now += elapsed
    milliseconds -= elapsed
    const current = [...frames.values()]
    frames.clear()
    current.forEach(callback => callback(now))
    await nextTick()
  }
}

describe('聊天正文的快速逐字呈现', () => {
  it('首次逐字出现，完整正文预留空间且辅助阅读只取全文', async () => {
    const wrapper = render('好的，需要时随时叫我。', { time: '14:12', speaking: true, status: '已完成播报' })
    expect(visible(wrapper)).toBe('')
    expect(wrapper.get('.message-text-measure').text()).toBe('好的，需要时随时叫我。')
    expect(wrapper.get('.message-text-visible').attributes('aria-hidden')).toBe('true')
    expect(wrapper.get('.message-text-source').text()).toBe('好的，需要时随时叫我。')
    expect(wrapper.find('.message-header').exists()).toBe(false)
    expect(wrapper.get('.message-author').element.children[0]?.classList.contains('avatar')).toBe(true)
    expect(wrapper.get('.message-author').element.children[1]?.tagName).toBe('TIME')
    expect(wrapper.get('.message-time').text()).toBe('14:12')
    expect(wrapper.classes()).toContain('speaking')
    expect(wrapper.find('.message-status').exists()).toBe(false)
    await advance(16)
    expect(visible(wrapper)).toBe('好')
    await advance(150)
    expect(visible(wrapper)).toBe('好的，需要时随时叫我。')
    expect(wrapper.find('.message-text-measure').exists()).toBe(false)
    expect(frames.size).toBe(0)
  })
  it('连续增量更新不重放前缀，快速修订始终收敛到最新正文', async () => {
    const wrapper = render('你好')
    await advance(32)
    expect(visible(wrapper)).toBe('你好')
    await wrapper.setProps({ text: '你好，第一段文字' })
    await advance(16)
    expect(visible(wrapper)).toBe('你好，')
    await wrapper.setProps({ text: '你好，第二段文字' })
    await wrapper.setProps({ text: '你好，最新的结果。' })
    expect(visible(wrapper)).toBe('你好，')
    expect(frames.size).toBe(1)
    await advance(150)
    expect(visible(wrapper)).toBe('你好，最新的结果。')
  })
  it('识别改字只替换变化后的内容，撤回或清空不留下旧字符', async () => {
    const wrapper = render('明天旧的内容', { animateOnMount: false })
    await wrapper.setProps({ text: '明天新的结果' })
    expect(visible(wrapper)).toBe('明天')
    await advance(16)
    expect(visible(wrapper)).toBe('明天新')
    await wrapper.setProps({ text: '明天' })
    expect(visible(wrapper)).toBe('明天')
    expect(frames.size).toBe(0)
    await wrapper.setProps({ text: '应当取消的内容' })
    await wrapper.setProps({ text: '' })
    await advance(160)
    expect(visible(wrapper)).toBe('')
    expect(frames.size).toBe(0)
  })
  it('表情、旗帜和组合音标以完整可见字符出现', async () => {
    const units = ['A', '👩🏽‍💻', '🇭🇰', 'e\u0301', '中']
    const wrapper = render(units.join(''))
    for (let count = 1; count <= units.length; count++) {
      await advance(10)
      expect(units.slice(0, count).join('')).toBe(visible(wrapper))
    }
  })
  it('长回复加速追上，保留记录初次直接显示但后续修改仍逐字呈现', async () => {
    const wrapper = render('字'.repeat(900))
    await advance(640)
    expect(visible(wrapper).length).toBeLessThan(900)
    await advance(16)
    expect(visible(wrapper)).toBe('字'.repeat(900))
    const history = render('已经显示的历史', { animateOnMount: false })
    expect(visible(history)).toBe('已经显示的历史')
    await history.setProps({ text: '已经显示的历史和补充内容' })
    expect(visible(history)).toBe('已经显示的历史')
    await advance(100)
    expect(visible(history)).toBe('已经显示的历史和补充内容')
  })
  it('减少动态效果或隐藏窗口时立即呈现完整最新文字', async () => {
    const wrapper = render('尚未完整显示的消息')
    await advance(16)
    motion.matches = true
    motionEvents.dispatchEvent(new Event('change'))
    await nextTick()
    expect(visible(wrapper)).toBe('尚未完整显示的消息')
    expect(frames.size).toBe(0)
    motion.matches = false
    await wrapper.setProps({ text: '尚未完整显示的消息，补充的文字' })
    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    await nextTick()
    expect(visible(wrapper)).toBe('尚未完整显示的消息，补充的文字')
    await wrapper.setProps({ text: '后台到达的完整消息' })
    expect(visible(wrapper)).toBe('后台到达的完整消息')
    expect(frames.size).toBe(0)
  })
  it('等待回复文案直接显示，卸载时取消尚未执行的字符更新', async () => {
    const pending = render('正在准备回复', { pending: true })
    expect(pending.get('.message-typing').text()).toBe('正在准备回复')
    expect(frames.size).toBe(0)
    const wrapper = render('尚未显示完的长句子')
    await advance(16)
    expect(frames.size).toBe(1)
    wrapper.unmount()
    expect(frames.size).toBe(0)
    await advance(160)
    expect(frames.size).toBe(0)
  })
})

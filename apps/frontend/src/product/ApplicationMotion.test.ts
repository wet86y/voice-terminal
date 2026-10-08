// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { h } from 'vue'
import ApplicationMotion from './ApplicationMotion.vue'
import { APPLICATION_EXIT_EVENT, captureStartupLogo, takeStartupLogo } from '../runtime/application-motion'

const mounted: ReturnType<typeof mount>[] = [] // mounted（需要释放的测试界面）。
let animate: ReturnType<typeof vi.fn>, cancel: ReturnType<typeof vi.fn> // animate（移动模拟）与 cancel（取消记录）。
const originalAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'animate')

function render(blocked = true) {
  const wrapper = mount(ApplicationMotion, {
    props: { blocked, stopping: false },
    slots: { default: ({ entering, exiting }: { entering: boolean; exiting: boolean }) => h('main', { class: 'app-shell' }, [
      h('section', { class: 'workbench' }, h('div', { 'data-home-logo': '', style: 'width:100px;height:100px' })),
      h('section', { class: 'startup-screen application-startup', 'data-entering': entering, 'data-exiting': exiting }, h('img', { class: 'startup-icon' })),
    ]) },
  })
  mounted.push(wrapper)
  return wrapper
}

beforeEach(() => {
  vi.useFakeTimers()
  takeStartupLogo()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return this.matches('.startup-icon') ? new DOMRect(500, 200, 72, 72)
      : this.matches('[data-home-logo]') ? new DOMRect(550, 300, 140, 140) : new DOMRect()
  })
  cancel = vi.fn()
  animate = vi.fn(() => ({ cancel }))
  Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: animate })
})

afterEach(() => {
  mounted.splice(0).forEach(wrapper => wrapper.unmount())
  takeStartupLogo()
  document.body.innerHTML = ''
  if (originalAnimate) Object.defineProperty(HTMLElement.prototype, 'animate', originalAnimate)
  else Reflect.deleteProperty(HTMLElement.prototype, 'animate')
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})

describe('Application motion（启动与退出界面过渡）', () => {
  it('完成加载后从真实起点移动，换算旋转主体尺寸，并在一秒时恢复主页', async () => {
    const wrapper = render()
    expect(wrapper.attributes('data-motion')).toBe('loading')
    await wrapper.setProps({ blocked: false })
    await flushPromises()
    expect(wrapper.attributes('inert')).toBeDefined()
    expect(wrapper.get('.application-logo-flight').attributes('style')).toContain('left: 500px')
    expect(animate).toHaveBeenCalledWith(expect.arrayContaining([
      expect.objectContaining({ transform: 'translate(56px, 106px) scale(1.7777777777777777, 1.7777777777777777)' }),
    ]), expect.objectContaining({ duration: 1000 }))
    await vi.advanceTimersByTimeAsync(999)
    expect(wrapper.attributes('data-motion')).toBe('entering')
    await vi.advanceTimersByTimeAsync(1)
    expect(wrapper.attributes('data-motion')).toBe('shown')
    expect(wrapper.attributes('inert')).toBeUndefined()
    expect(wrapper.find('.application-logo-flight').exists()).toBe(false)
  })

  it('模块挂载前已经跳过时沿用预检图标坐标，后续组件状态恢复不重播', async () => {
    document.body.innerHTML = '<div id="app"><img class="startup-icon"></div>'
    captureStartupLogo()
    const wrapper = render(false)
    await flushPromises()
    expect(animate).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1000)
    await wrapper.setProps({ blocked: true })
    await wrapper.setProps({ blocked: false })
    expect(wrapper.attributes('data-motion')).toBe('shown')
    expect(animate).toHaveBeenCalledOnce()
    expect(takeStartupLogo()).toBeNull()
  })

  it('托盘退出发生在入场期间时取消移动，并保持退出页面而不被旧定时器覆盖', async () => {
    const wrapper = render(false)
    await flushPromises()
    window.dispatchEvent(new Event(APPLICATION_EXIT_EVENT))
    await flushPromises()
    expect(cancel).toHaveBeenCalledOnce()
    expect(wrapper.attributes('data-motion')).toBe('exiting')
    expect(wrapper.get('.application-startup').attributes('data-exiting')).toBe('true')
    expect(wrapper.find('.application-logo-flight').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(2000)
    await wrapper.setProps({ blocked: false })
    expect(wrapper.attributes('data-motion')).toBe('exiting')
  })

  it('宿主开始退出后同步显示退出浮层并阻止重复操作', async () => {
    const wrapper = render()
    await wrapper.setProps({ stopping: true })
    expect(wrapper.attributes('data-motion')).toBe('exiting')
    expect(wrapper.attributes('inert')).toBeDefined()
    expect(wrapper.get('.application-startup').attributes('data-exiting')).toBe('true')
  })

  it('系统要求减少动态效果时直接进入主页，仍处理退出通知', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })))
    const wrapper = render(false)
    await flushPromises()
    expect(wrapper.attributes('data-motion')).toBe('shown')
    expect(animate).not.toHaveBeenCalled()
    window.dispatchEvent(new Event(APPLICATION_EXIT_EVENT))
    await flushPromises()
    expect(wrapper.attributes('data-motion')).toBe('exiting')
  })

  it('卸载时释放动画、退出监听与收尾定时器', async () => {
    const wrapper = render(false)
    await flushPromises()
    const remove = vi.spyOn(window, 'removeEventListener')
    wrapper.unmount()
    mounted.splice(mounted.indexOf(wrapper), 1)
    expect(cancel).toHaveBeenCalledOnce()
    expect(remove).toHaveBeenCalledWith(APPLICATION_EXIT_EVENT, expect.any(Function))
    expect(vi.getTimerCount()).toBe(0)
  })
})

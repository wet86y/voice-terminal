// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { defineComponent, h, nextTick } from 'vue'
import { mount, type VueWrapper } from '@vue/test-utils'
import UiDialog from './UiDialog.vue'

const wrappers: VueWrapper[] = [] // wrappers（待卸载的组件）。
afterEach(() => { wrappers.splice(0).reverse().forEach(wrapper => wrapper.unmount()); document.body.innerHTML = '' })
function press(key: string, shiftKey = false) { // press（模拟键盘导航）。
  const event = new KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true })
  document.dispatchEvent(event)
  return event
}

describe('UiDialog（公共弹窗行为）', () => {
  it('先聚焦说明区域，正反向导航跳过禁用与隐藏控件，并在关闭后还焦点', async () => {
    const trigger = document.createElement('button') // trigger（打开弹窗的原控件）。
    document.body.append(trigger); trigger.focus()
    const wrapper = mount(UiDialog, {
      attachTo: document.body, props: { open: true, title: '保存设置' },
      slots: { default: '<p>先检查再保存。</p><input disabled><button hidden>隐藏</button><button id="first">取消</button><button id="last">保存</button>' },
    })
    wrappers.push(wrapper)
    await nextTick()
    expect(document.activeElement).toBe(wrapper.get('.apply-dialog').element)
    expect(press('Tab', true).defaultPrevented).toBe(true)
    expect(document.activeElement?.id).toBe('last')
    press('Tab'); expect(document.activeElement?.id).toBe('first')
    press('Tab', true); expect(document.activeElement?.id).toBe('last')
    await wrapper.setProps({ open: false }); await nextTick()
    expect(document.activeElement).toBe(trigger)
  })

  it('只有最上层响应退出键，多个弹窗的标题关联独立且下层保持打开', async () => {
    const closed: string[] = [] // closed（收到关闭请求的弹窗）。
    const Host = defineComponent({
      props: { upperOpen: { type: Boolean, default: true } },
      setup: props => () => h('main', [
        h(UiDialog, { open: true, title: '资源清单', onClose: () => closed.push('资源清单') }),
        h(UiDialog, { open: props.upperOpen, title: '确认选择', onClose: () => closed.push('确认选择') }),
      ]),
    })
    const wrapper = mount(Host, { attachTo: document.body }); wrappers.push(wrapper)
    await nextTick()
    const dialogs = wrapper.findAll('[role="dialog"]')
    const titles = dialogs.map(dialog => dialog.attributes('aria-labelledby'))
    expect(new Set(titles).size).toBe(2)
    titles.forEach(id => expect(document.getElementById(id!)?.textContent).toBeTruthy())
    press('Escape'); expect(closed).toEqual(['确认选择'])
    await wrapper.setProps({ upperOpen: false }); await nextTick()
    expect(wrapper.findAll('[role="dialog"]')).toHaveLength(1)
    press('Escape'); expect(closed).toEqual(['确认选择', '资源清单'])
  })

  it('折叠摘要按自然顺序参与正反向导航，不被跳回操作区', async () => {
    const wrapper = mount(UiDialog, {
      attachTo: document.body, props: { open: true, title: '确认依赖' },
      slots: { default: '<button id="before-summary">返回</button><details open><summary id="dependencies">运行依赖</summary><button>选择依赖</button></details><button id="after-summary">开始</button>' },
    })
    wrappers.push(wrapper); await nextTick()
    ;(wrapper.get('summary').element as HTMLElement).focus()
    expect(press('Tab').defaultPrevented).toBe(false)
    expect(press('Tab', true).defaultPrevented).toBe(false)
    expect(document.activeElement?.id).toBe('dependencies')
    ;(wrapper.get('#after-summary').element as HTMLElement).focus()
    press('Tab'); expect(document.activeElement?.id).toBe('before-summary')
  })

  it('处理中阻止原生取消与退出键，恢复后关闭请求仍交由调用方处理', async () => {
    const wrapper = mount(UiDialog, { attachTo: document.body, props: { open: true, title: '同步程序', busy: true } })
    wrappers.push(wrapper); await nextTick()
    const cancel = new Event('cancel', { cancelable: true }) // cancel（浏览器原生关闭请求）。
    wrapper.get('dialog').element.dispatchEvent(cancel)
    press('Escape')
    expect(cancel.defaultPrevented).toBe(true)
    expect(wrapper.emitted('close')).toBeUndefined()
    await wrapper.setProps({ busy: false })
    wrapper.get('dialog').element.dispatchEvent(new Event('cancel', { cancelable: true }))
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
  })

  it('从内容区拖出不误关，仅从遮罩开始的点击请求关闭', async () => {
    const wrapper = mount(UiDialog, { attachTo: document.body, props: { open: true, title: '编辑内容' } })
    wrappers.push(wrapper); await nextTick()
    await wrapper.get('.apply-dialog').trigger('pointerdown')
    await wrapper.get('dialog').trigger('pointerup')
    await wrapper.get('dialog').trigger('click')
    expect(wrapper.emitted('close')).toBeUndefined()
    await wrapper.get('dialog').trigger('pointerdown')
    await wrapper.get('.apply-dialog').trigger('pointerup')
    await wrapper.get('dialog').trigger('click')
    expect(wrapper.emitted('close')).toBeUndefined()
    await wrapper.get('dialog').trigger('pointerdown')
    await wrapper.get('dialog').trigger('pointerup')
    await wrapper.get('dialog').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
    await wrapper.setProps({ busy: true })
    await wrapper.get('dialog').trigger('pointerdown')
    await wrapper.get('dialog').trigger('pointerup')
    await wrapper.get('dialog').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('关闭时原控件已禁用，不强行恢复焦点；没有操作时不渲染空页脚', async () => {
    const trigger = document.createElement('button') // trigger（关闭时可能已不可操作的控件）。
    document.body.append(trigger); trigger.focus()
    const wrapper = mount(UiDialog, { attachTo: document.body, props: { open: true, title: '查看进度' } })
    wrappers.push(wrapper); await nextTick()
    expect(wrapper.find('footer').exists()).toBe(false)
    trigger.disabled = true
    await wrapper.setProps({ open: false }); await nextTick()
    expect(document.activeElement).not.toBe(trigger)
  })
})

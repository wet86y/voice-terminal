// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import UiDemoApp from './UiDemoApp.vue'
import { voiceShadowReplayDemo } from './voice-shadow-replay'

let mounted: VueWrapper[] = []

function renderDemo(search = '') {
  history.replaceState({}, '', `/${search}`)
  const wrapper = mount(UiDemoApp)
  mounted.push(wrapper)
  return wrapper
}

async function openTools(wrapper: VueWrapper) {
  await wrapper.get('.preview-tools-trigger').trigger('click')
  await flushPromises()
}

async function selectTool(wrapper: VueWrapper, labelText: string, value: string | boolean) {
  const label = wrapper.findAll('.preview-tools label').find(item => item.text().includes(labelText))
  if (!label) throw new Error(`展示工具中缺少“${labelText}”选择项。`)
  await label.get('select').setValue(value)
  await flushPromises()
}

beforeEach(() => history.replaceState({}, '', '/'))
afterEach(() => {
  mounted.splice(0).forEach(wrapper => wrapper.unmount())
  history.replaceState({}, '', '/')
})

describe('桌面样例回放交互', () => {
  it('切换到回放后统一显示播报状态，重置会清除回放并恢复默认场景', async () => {
    const wrapper = renderDemo('?runtime=listening')
    await openTools(wrapper)
    await selectTool(wrapper, '事件回放', true)

    expect(new URLSearchParams(location.search).get('replay')).toBe('voice-shadow')
    expect(wrapper.get('.app-shell').classes()).toContain('runtime-speaking')
    expect(wrapper.get('.system-heading h2').text()).toBe('正在回答')
    expect(wrapper.find('.live-asr').exists()).toBe(false)
    expect(wrapper.find('.assistant-pending').exists()).toBe(false)
    expect(wrapper.findAll('.message.speaking')).toHaveLength(1)

    await selectTool(wrapper, '事件回放', false)
    expect(new URLSearchParams(location.search).has('replay')).toBe(false)
    expect(wrapper.get('.app-shell').classes()).toContain('runtime-listening')
    expect(wrapper.find('.live-asr').exists()).toBe(true)
    await selectTool(wrapper, '事件回放', true)

    await wrapper.get('.preview-tools').findAll('button').find(button => button.text() === '重置展示')!.trigger('click')
    await flushPromises()

    const query = new URLSearchParams(location.search)
    expect(query.has('replay')).toBe(false)
    expect(query.get('page')).toBe('home')
    expect(query.get('scene')).toBe('normal')
    expect(query.get('runtime')).toBe('standby')
    expect(wrapper.get('.app-shell').classes()).toContain('runtime-standby')
    expect(wrapper.get('.system-heading h2').text()).toBe('全天候待机')
  })

  it('回放、错误场景和聆听选择组合时错误状态优先且不显示录音或思考中画面', async () => {
    const wrapper = renderDemo('?runtime=listening')
    await openTools(wrapper)
    await selectTool(wrapper, '事件回放', true)
    await selectTool(wrapper, '内容场景', 'error')

    expect(wrapper.get('.app-shell').classes()).toContain('runtime-error')
    expect(wrapper.get('.system-heading h2').text()).toBe('需要注意')
    expect(wrapper.get('.orb-stage').classes()).toContain('orb-error')
    expect(wrapper.find('.live-asr').exists()).toBe(false)
    expect(wrapper.find('.assistant-pending').exists()).toBe(false)
    expect(wrapper.findAll('.message.speaking')).toHaveLength(0)
  })

  it('长内容回放仅高亮当前的最后一条回复', async () => {
    const wrapper = renderDemo()
    await openTools(wrapper)
    await selectTool(wrapper, '事件回放', true)
    await selectTool(wrapper, '内容场景', 'long')

    const highlighted = wrapper.findAll('.message.speaking')
    expect(wrapper.findAll('.assistant-message').length).toBeGreaterThan(1)
    expect(highlighted).toHaveLength(1)
    expect(highlighted[0].text()).toContain(voiceShadowReplayDemo.response)
  })
})

// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, expect, it, vi } from 'vitest'
import OperationNotice from './OperationNotice.vue'
import type { RuntimeOperation } from '../runtime/runtime-operations'

const running = (id = 'operation'): RuntimeOperation => ({ id, revision: 1, target: 'future', label: '未来组件', state: 'running', phase: 'switching', detail: '' })
afterEach(() => { vi.useRealTimers() })

it('shows generic stages and quickly fades only after all work is ready', async () => {
  vi.useFakeTimers()
  const first = running()
  const second = { ...running('second'), phase: 'restarting' }
  const wrapper = mount(OperationNotice, { props: { operations: [first, second] } })
  expect(wrapper.find('.operation-spinner').exists()).toBe(true)
  expect(wrapper.text()).toContain('切换中')
  expect(wrapper.text()).toContain('重启中')
  const done = { ...first, revision: 2, state: 'succeeded' as const, phase: 'ready' }
  await wrapper.setProps({ operations: [done, second] })
  await vi.advanceTimersByTimeAsync(1000)
  expect(wrapper.find('.operation-spinner').exists()).toBe(true)
  await wrapper.setProps({ operations: [done, { ...second, revision: 2, state: 'succeeded', phase: 'ready' }] })
  expect(wrapper.text()).toContain('系统已就绪')
  await vi.advanceTimersByTimeAsync(300)
  expect(wrapper.find('.is-fading').exists()).toBe(true)
  await vi.advanceTimersByTimeAsync(160)
  expect(wrapper.find('.operation-notice').exists()).toBe(false)
  wrapper.unmount()
})

it('keeps failures visible and rejects stale revisions', async () => {
  vi.useFakeTimers()
  const row = running()
  const wrapper = mount(OperationNotice, { props: { operations: [row] } })
  await wrapper.setProps({ operations: [{ ...row, revision: 3, state: 'failed', phase: 'failed', detail: '恢复原模型失败' }] })
  await wrapper.setProps({ operations: [{ ...row, revision: 2, state: 'succeeded', phase: 'ready' }] })
  await vi.advanceTimersByTimeAsync(10000)
  expect(wrapper.text()).toContain('恢复原模型失败')
  await wrapper.get('button').trigger('click')
  expect(wrapper.find('.operation-notice').exists()).toBe(false)
  wrapper.unmount()
})

it('does not replay historical success and cancels old fade when new work starts', async () => {
  vi.useFakeTimers()
  const row = running()
  const wrapper = mount(OperationNotice, { props: { operations: [{ ...row, state: 'succeeded', phase: 'ready' }] } })
  expect(wrapper.find('.operation-notice').exists()).toBe(false)
  await wrapper.setProps({ operations: [running('next')] })
  await wrapper.setProps({ operations: [{ ...running('next'), revision: 2, state: 'succeeded', phase: 'ready' }] })
  await vi.advanceTimersByTimeAsync(300)
  await wrapper.setProps({ operations: [running('third')] })
  await vi.advanceTimersByTimeAsync(1000)
  await nextTick()
  expect(wrapper.find('.operation-spinner').exists()).toBe(true)
  expect(wrapper.find('.is-fading').exists()).toBe(false)
  wrapper.unmount()
})

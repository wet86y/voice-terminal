// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import WakeTaskSchedule from './WakeTaskSchedule.vue'
import TimePicker from './TimePicker.vue'

it('修改分钟保留官方调度返回的原秒数与毫秒', () => {
  for (const ruleKind of ['at', 'daily'] as const) {
    const wrapper = mount(WakeTaskSchedule, { props: {
      ruleKind, when: '2026-10-07T08:30:17.000', time: '08:30:17.000', weekdays: '',
      intervalHours: '1', intervalMinutes: '0', loop: true, triggerCount: '1',
    } })
    wrapper.getComponent(TimePicker).vm.$emit('update:modelValue', '08:40')
    expect(wrapper.emitted(ruleKind === 'at' ? 'update:when' : 'update:time')?.[0]).toEqual([
      ruleKind === 'at' ? '2026-10-07T08:40:17.000' : '08:40:17.000',
    ])
    wrapper.unmount()
  }
})

it('基础规则选择器提供间隔，有限间隔显示次数并校验 24 小时边界', async () => {
  const wrapper = mount(WakeTaskSchedule, { props: {
    ruleKind: 'every', when: '', time: '', weekdays: '',
    intervalHours: '1', intervalMinutes: '0', loop: true, triggerCount: '1',
  } })
  expect(wrapper.find('option[value="every"]').exists()).toBe(true)
  expect(wrapper.find('option[value="cron"]').exists()).toBe(false)
  const intervalInput = wrapper.get('[aria-label="间隔时间"]').element as HTMLInputElement
  expect(intervalInput.value).toBe('01:00')
  expect(wrapper.get('[aria-label="循环"]').attributes('role')).toBe('switch')
  expect(wrapper.find('[aria-label="触发次数"]').exists()).toBe(false)
  await wrapper.setProps({ intervalHours: '24', intervalMinutes: '1', loop: false })
  expect(intervalInput.value).toBe('')
  expect(wrapper.get('[aria-label="间隔时间"]').attributes('aria-invalid')).toBe('true')
  expect(wrapper.find('[aria-label="触发次数"]').exists()).toBe(true)
  await wrapper.setProps({ intervalMinutes: '0', triggerCount: '0' })
  expect(intervalInput.value).toBe('24:00')
  expect(wrapper.get('[aria-label="间隔时间"]').attributes('aria-invalid')).toBeUndefined()
  expect(wrapper.get('[aria-label="触发次数"]').attributes('aria-invalid')).toBe('true')
  await wrapper.setProps({ triggerCount: '2' })
  expect(wrapper.get('[aria-label="触发次数"]').attributes('aria-invalid')).toBe('false')
  wrapper.unmount()
})

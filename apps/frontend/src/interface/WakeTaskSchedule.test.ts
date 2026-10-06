// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import WakeTaskSchedule from './WakeTaskSchedule.vue'
import TimePicker from './TimePicker.vue'

it('修改分钟保留官方调度返回的原秒数与毫秒', () => {
  for (const ruleKind of ['at', 'daily'] as const) {
    const wrapper = mount(WakeTaskSchedule, { props: {
      ruleKind, when: '2026-10-07T08:30:17.000', time: '08:30:17.000', weekdays: '',
    } })
    wrapper.getComponent(TimePicker).vm.$emit('update:modelValue', '08:40')
    expect(wrapper.emitted(ruleKind === 'at' ? 'update:when' : 'update:time')?.[0]).toEqual([
      ruleKind === 'at' ? '2026-10-07T08:40:17.000' : '08:40:17.000',
    ])
    wrapper.unmount()
  }
})

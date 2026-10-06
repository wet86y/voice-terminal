// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { h, nextTick, ref } from 'vue'
import DatePicker from './DatePicker.vue'

const mounted: VueWrapper[] = []
function renderDate(initial = '2026-10-03') {
  const value = ref(initial), disabled = ref(false) // value（日期值）、disabled（是否禁用）。
  const wrapper = mount({ setup: () => () => h(DatePicker, {
    modelValue: value.value, label: '触发日期', disabled: disabled.value,
    'onUpdate:modelValue': (date: string) => { value.value = date },
  }) })
  mounted.push(wrapper)
  return { wrapper, value, disabled }
}
afterEach(() => mounted.splice(0).forEach(wrapper => wrapper.unmount()))
describe('定时任务日期选择', () => {
  it('允许直接输入，显示年/月/日，同时保存原有的日期格式', async () => {
    const { wrapper, value } = renderDate()
    await wrapper.get('input').trigger('focus')
    await wrapper.get('input').setValue('2028/2/29')
    await wrapper.get('input').trigger('blur')
    expect(value.value).toBe('2028-02-29')
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('2028/02/29')
  })
  it.each(['2026/02/29', '2026/04/31', '2026/13/01', '0001/01/01'])('无效日期 %s 不会覆盖有效日期', async text => {
    const { wrapper, value } = renderDate()
    await wrapper.get('input').trigger('focus')
    await wrapper.get('input').setValue(text)
    await wrapper.get('input').trigger('blur')
    expect(value.value).toBe('2026-10-03')
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('2026/10/03')
  })
  it('按月份调整月底日期时限制到该月最后一天，按天可跨年', async () => {
    const { wrapper, value } = renderDate('2028-01-31')
    const input = wrapper.get('input'), element = input.element as HTMLInputElement
    element.setSelectionRange(5, 7)
    await input.trigger('keydown', { key: 'ArrowUp' })
    expect(value.value).toBe('2028-02-29')
    value.value = '2028-12-31'
    await nextTick()
    element.setSelectionRange(8, 10)
    await input.trigger('keydown', { key: 'ArrowUp' })
    expect(value.value).toBe('2029-01-01')
  })
  it('禁用期间输入框和日历入口都不可操作', async () => {
    const { wrapper, value, disabled } = renderDate()
    disabled.value = true
    await nextTick()
    expect((wrapper.get('input').element as HTMLInputElement).disabled).toBe(true)
    expect((wrapper.get('.date-picker-trigger').element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.get('input').trigger('keydown', { key: 'ArrowUp' })
    expect(value.value).toBe('2026-10-03')
  })
})

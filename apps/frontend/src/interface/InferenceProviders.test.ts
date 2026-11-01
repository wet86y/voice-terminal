// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import FixedProviderUsage from './FixedProviderUsage.vue'
import FixedProviderSettings from './FixedProviderSettings.vue'
import type { LocalProviderSettings } from './local-components'

function model(provider: 'deepseek' | 'minimax', usage?: LocalProviderSettings['usage']): LocalProviderSettings {
  return { provider, configured: true, save: vi.fn(async () => ({ configured: true })), usage }
}

it('父组件重建配置与更新运行状态不自动刷新余额，手动刷新仍有效', async () => {
  const query = vi.fn().mockResolvedValue({ balance_infos: [{ currency: 'CNY', total_balance: '20.87' }] })
  const wrapper = mount(FixedProviderUsage, { props: { model: model('deepseek', query), refreshKey: 0 } })
  await flushPromises()
  expect(query).toHaveBeenCalledTimes(1)
  for (let revision = 0; revision < 20; revision++) {
    await wrapper.setProps({ model: { ...model('deepseek', () => query()), disabled: revision % 2 === 0 } })
    await flushPromises()
  }
  expect(query).toHaveBeenCalledTimes(1)
  expect(wrapper.text()).toContain('20.87')
  await wrapper.get('button').trigger('click'); await flushPromises()
  expect(query).toHaveBeenCalledTimes(2)
  wrapper.unmount()
})

it('供应方切换清除旧余额并拒绝迟到回包覆盖 MiniMax 显示', async () => {
  let resolveOld!: (value: Record<string, unknown>) => void
  const old = new Promise<Record<string, unknown>>(resolve => { resolveOld = resolve })
  const wrapper = mount(FixedProviderUsage, { props: { model: model('deepseek', () => old) } })
  await wrapper.setProps({ model: model('minimax', async () => ({ billing_mode: 'pay_as_you_go' })) })
  await flushPromises()
  expect(wrapper.text()).toContain('余额和账单请在 MiniMax 控制台查看')
  resolveOld({ balance_infos: [{ currency: 'CNY', total_balance: '999' }] })
  await flushPromises()
  expect(wrapper.text()).not.toContain('999')
  expect(wrapper.attributes('data-provider')).toBe('minimax')
  wrapper.unmount()
})

it('共享密钥刷新使两处 MiniMax 用量同时重新查询，旧查询不能覆盖新凭据结果', async () => {
  let resolveOld!: (value: Record<string, unknown>) => void
  const old = new Promise<Record<string, unknown>>(resolve => { resolveOld = resolve })
  const query = vi.fn().mockReturnValueOnce(old).mockResolvedValue({ billing_mode: 'pay_as_you_go', balance: 12, currency: 'CNY' })
  const first = mount(FixedProviderUsage, { props: { model: model('minimax', query), refreshKey: 0 } })
  const second = mount(FixedProviderUsage, { props: { model: model('minimax', query), refreshKey: 0 } })
  await first.setProps({ refreshKey: 1 }); await second.setProps({ refreshKey: 1 })
  await flushPromises()
  resolveOld({ billing_mode: 'pay_as_you_go', balance: 999, currency: 'CNY' })
  await flushPromises()
  for (const wrapper of [first, second]) { expect(wrapper.text()).not.toContain('999'); expect(wrapper.text()).toContain('12'); wrapper.unmount() }
  expect(query).toHaveBeenCalledTimes(4)
})

it('密钥草稿不跨供应方携带，保存后清空且使用对应供应方回调', async () => {
  const deepseek = model('deepseek'); const minimax = model('minimax')
  const wrapper = mount(FixedProviderSettings, { props: { model: deepseek } })
  await wrapper.get('input').setValue('synthetic-deepseek-key')
  await wrapper.setProps({ model: minimax })
  expect(wrapper.get<HTMLInputElement>('input').element.value).toBe('')
  await wrapper.get('input').setValue('synthetic-minimax-key')
  await wrapper.get('button').trigger('click'); await flushPromises()
  expect(minimax.save).toHaveBeenCalledWith('synthetic-minimax-key')
  expect(deepseek.save).not.toHaveBeenCalled()
  expect(wrapper.get<HTMLInputElement>('input').element.value).toBe('')
  expect(wrapper.text()).toContain('主语音推理、在线语音合成共用')
  wrapper.unmount()
})

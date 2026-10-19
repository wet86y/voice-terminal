// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ManagedResourceProgress from './ManagedResourceProgress.vue'
import type { ManagedResourceJob } from './local-components'

describe('资源进度按阶段独立计数', () => {
  const job: ManagedResourceJob = { id: 'download', resourceId: 'model', label: '模型', state: 'running',
    phase: '下载固定资源', progressKind: 'download', canCancel: true,
    downloadCompletedBytes: 20, downloadTotalBytes: 100, verificationCompletedBytes: 90, verificationTotalBytes: 100 }

  it('下载与校验共用一个条形，切换阶段时仅取对应计数', async () => {
    const wrapper = mount(ManagedResourceProgress, { props: { job, label: '模型' } })
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('20')
    expect(wrapper.text()).toContain('下载：20 B / 100 B · 20%')
    await wrapper.setProps({ job: { ...job, phase: '校验下载文件', progressKind: 'verify' } })
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('90')
    expect(wrapper.text()).toContain('校验：90 B / 100 B · 90%')
    expect(wrapper.text()).not.toContain('110')
  })

  it('等待和安装阶段不沿用上一阶段百分比', () => {
    const wrapper = mount(ManagedResourceProgress, { props: { job: { ...job, progressKind: 'waiting', phase: '等待文件释放' }, label: '模型' } })
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
    expect(wrapper.text()).not.toContain('%')
  })

  it('兼容没有进度类型的旧任务记录，仍只显示一条', () => {
    const wrapper = mount(ManagedResourceProgress, { props: { job: { ...job, progressKind: undefined, phase: '批量校验进度：校验下载文件' }, label: '模型' } })
    expect(wrapper.findAll('[role="progressbar"]')).toHaveLength(1)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('90')
  })

  it('恢复期间持续显示当前轮次，下载计数从本轮重新计算', () => {
    const wrapper = mount(ManagedResourceProgress, { props: { job: { ...job, attempt: 2, downloadCompletedBytes: 0 }, label: '模型' } })
    expect(wrapper.get('[role="status"]').text()).toContain('第 2 / 3 轮')
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('0')
  })
})

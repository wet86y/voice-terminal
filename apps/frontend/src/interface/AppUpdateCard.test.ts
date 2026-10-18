// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AppUpdateCard from './AppUpdateCard.vue'
import type { AppUpdateStatus } from './app-update'

const status: AppUpdateStatus = {
  state: 'available', current_version: '0.9.0', target_version: '0.9.2',
  release_notes: '改进安装与更新体验\n• 保留已有用户设置和组件。', fields: {},
}

describe('AppUpdateCard（应用更新卡片）', () => {
  it('下载时保持取消操作可用，并展示进度和独立发行说明', async () => {
    const wrapper = mount(AppUpdateCard, { props: { status: {
      ...status, state: 'downloading', can_cancel: true, completed_bytes: 25, total_bytes: 100,
    } } })
    expect(wrapper.get('button').element.disabled).toBe(false)
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('25')
    expect(wrapper.get('.update-release-notes').text()).toContain('保留已有用户设置')
    expect(wrapper.get('.update-summary').text()).not.toContain('保留已有用户设置')
    await wrapper.setProps({ status: { ...status, state: 'downloading', can_cancel: false } })
    expect(wrapper.get('button').element.disabled).toBe(true)
  })

  it.each(['available', 'failed', 'cancelled'] as const)('阶段 %s 有目标版本时可以开始或重试下载', async state => {
    const wrapper = mount(AppUpdateCard, { props: { status: { ...status, state } } })
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('download')).toHaveLength(1)
  })

  it('检查失败没有目标版本时重新检查，交接期间禁止重复操作', async () => {
    const wrapper = mount(AppUpdateCard, { props: { status: {
      ...status, state: 'failed', target_version: null, error: '发行信息暂时不可用',
    } } })
    expect(wrapper.get('[role="alert"]').text()).toBe('发行信息暂时不可用')
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('check')).toHaveLength(1)
    await wrapper.setProps({ status: { ...status, state: 'handing_off' } })
    expect(wrapper.get('button').element.disabled).toBe(true)
  })

  it('未收到字节总量时展示等待进度，并遵守工作区禁用状态', () => {
    const wrapper = mount(AppUpdateCard, { props: { status: {
      ...status, state: 'downloading', can_cancel: true,
    }, disabled: true } })
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
    expect(wrapper.get('button').element.disabled).toBe(true)
  })
})

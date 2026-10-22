// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AppUpdateCard from './AppUpdateCard.vue'
import type { AppUpdateStatus } from './app-update'

const status: AppUpdateStatus = {
  state: 'available', current_version: '0.9.0', target_version: '0.9.3',
  release_notes: '改进安装与更新体验\n• 保留已有用户设置和组件。', fields: {},
}

describe('AppUpdateCard（应用更新卡片）', () => {
  it('完整性入口位于更新右侧，缺失时只请求下载当前版本校验程序，准备后才可启动校验', async () => {
    const wrapper = mount(AppUpdateCard, { props: { status, integrity: { state: 'idle', current_version: '0.9.0', installer_ready: false, fields: {} } } })
    expect(wrapper.get('.update-actions').findAll('button').map(button => button.text())).toEqual(['下载并安装', '下载校验程序'])
    expect(wrapper.get('.integrity-detail').text()).toContain('不会自动获取最新版')
    await wrapper.get('.integrity-action').trigger('click')
    expect(wrapper.emitted('downloadIntegrity')).toHaveLength(1)
    expect(wrapper.emitted('download')).toBeUndefined()
    expect(wrapper.emitted('checkIntegrity')).toBeUndefined()
    await wrapper.setProps({ integrity: { state: 'current', current_version: '0.9.0', installer_ready: true, fields: {} } })
    expect(wrapper.get('.integrity-action').text()).toBe('校验完整性')
    expect(wrapper.get('.integrity-detail').text()).toContain('将关闭程序')
    await wrapper.get('.integrity-action').trigger('click')
    expect(wrapper.emitted('checkIntegrity')).toHaveLength(1)
  })
  it('下载校验程序可取消，同时禁止触发更新', async () => {
    const wrapper = mount(AppUpdateCard, { props: { status, integrity: { state: 'downloading', current_version: '0.9.0', can_cancel: true, fields: {} } } })
    expect(wrapper.get<HTMLButtonElement>('.update-action').element.disabled).toBe(true)
    await wrapper.get('.integrity-action').trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })
  it('显示作者并通过独立操作打开项目仓库，不触发更新下载', async () => {
    const wrapper = mount(AppUpdateCard, { props: { status } })
    expect(wrapper.get('.update-about').text()).toContain('作者 wet86y')
    await wrapper.get('.update-repository').trigger('click')
    expect(wrapper.emitted('repository')).toHaveLength(1)
    expect(wrapper.emitted('download')).toBeUndefined()
  })
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

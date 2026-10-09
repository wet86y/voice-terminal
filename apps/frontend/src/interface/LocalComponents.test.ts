// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import LocalComponents from './LocalComponents.vue'
import type { LocalComponentsModel } from './local-components'

function model(): LocalComponentsModel {
  return {
    status: '运行正常', state: 'ready', sampledAt: '当前', resources: [], resourceActionsAvailable: true,
    rows: [{
      id: 'voice-system', name: 'Voice Server（语音服务）', state: 'ready', status: '运行中', available: true,
      runtime: '本地系统服务', description: '完整语音运行环境', facts: [],
      resource: { id: 'voice-server', label: 'Voice Server（语音服务）', state: 'installed', status: '已配置' },
    }],
  }
}

describe('LocalComponents（本地组件）运行环境与导入入口', () => {
  it('全部安装就绪后隐藏批量导入，并保持语音服务详情入口', async () => {
    const wrapper = mount(LocalComponents, { props: { model: model() } })
    expect(wrapper.find('.resource-batch-import').exists()).toBe(false)
    expect(wrapper.find('.local-install-actions').exists()).toBe(false)
    await wrapper.get('[data-component="voice-system"] .local-component-action').trigger('click')
    expect(wrapper.get('.local-managed-resource').text()).toContain('已配置')
    wrapper.unmount()
  })

  it('语音依赖不完整时在语音服务条目下载或导入，并在完成后隐藏批量入口', async () => {
    const value = model()
    value.rows[0].resource!.state = 'incomplete'
    const wrapper = mount(LocalComponents, { props: { model: value } })
    expect(wrapper.find('.resource-batch-import').exists()).toBe(true)
    await wrapper.get('button[aria-label="Voice Server（语音服务）：下载"]').trigger('click')
    await wrapper.get('button[aria-label="Voice Server（语音服务）：导入"]').trigger('click')
    expect(wrapper.emitted('resourceDownload')).toEqual([['voice-server']])
    expect(wrapper.emitted('resourceImport')).toEqual([['voice-server']])
    const complete = model()
    await wrapper.setProps({ model: complete })
    expect(wrapper.find('.resource-batch-import').exists()).toBe(false)
    wrapper.unmount()
  })

  it('单个模型缺失时允许批量导入，进程故障本身不会重新显示批量入口', async () => {
    const value = model()
    value.rows.push({ ...value.rows[0], id: 'paraformer', name: 'Paraformer（语音识别）', state: 'failed', status: '不可用', available: false,
      resource: { id: 'paraformer-streaming', label: 'Paraformer（语音识别）', state: 'missing', status: '缺失' } })
    const wrapper = mount(LocalComponents, { props: { model: value } })
    await wrapper.get('.resource-batch-import').trigger('click')
    expect(wrapper.emitted('resourceImportAll')).toEqual([[]])
    await wrapper.setProps({ model: { ...value, rows: value.rows.map(row => ({ ...row, resource: { ...row.resource!, state: 'installed' } })) } })
    expect(wrapper.find('.resource-batch-import').exists()).toBe(false)
    wrapper.unmount()
  })
})

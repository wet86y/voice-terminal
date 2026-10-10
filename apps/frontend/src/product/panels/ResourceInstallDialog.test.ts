// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { useWorkspaceResources } from '../useWorkspaceResources'
import type { ResourceInstallPlan, ResourceStatus } from '../desktop-port'
import ResourceInstallDialog from './ResourceInstallDialog.vue'

function controller(plan: ResourceInstallPlan, resources: ResourceStatus[] = []) {
  return {
    pendingDownloadPlan: ref(plan), pendingImport: ref(null), resources: ref(resources), confirmDownload: vi.fn(), confirmImport: vi.fn(),
  } as unknown as ReturnType<typeof useWorkspaceResources>
}

describe('ResourceInstallDialog（资源安装确认）模型与共享依赖入口', () => {
  it('模型任务只说明模型文件，语音依赖由语音服务入口配置', async () => {
    const model = controller({ resource_id: 'fsmn-vad', title: 'FSMN-VAD（语音活动检测）', model_bytes: 1730986, environment: null })
    const wrapper = mount(ResourceInstallDialog, { props: { controller: model } })
    expect(wrapper.text()).toContain('本次仅下载模型文件')
    expect(wrapper.text()).toContain('Voice Server（语音服务）中配置')
    expect(wrapper.text()).not.toContain('一次检测并配置')
    const button = wrapper.get('.install-plan-actions button')
    expect(button.text()).toBe('开始下载')
    await button.trigger('click')
    expect(model.confirmDownload).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('语音服务任务继续统一准备解释器和全部语音依赖', () => {
    const model = controller({ resource_id: 'voice-server', title: 'Voice Server（语音服务）', model_bytes: 0,
      environment: { group: 'all', already_ready: false, total_bytes: 300, wheels: [], system_candidates: [] } })
    const wrapper = mount(ResourceInstallDialog, { props: { controller: model } })
    expect(wrapper.text()).toContain('全部语音依赖')
    expect(wrapper.get('.install-plan-actions button').text()).toBe('开始下载并配置')
    wrapper.unmount()
  })

  it('轻量语音检测计划说明只补齐对应依赖，不暴露内部依赖组名称', () => {
    const model = controller({ resource_id: 'fsmn-vad', title: 'FSMN-VAD（语音活动检测）', model_bytes: 1730986,
      environment: { group: 'onnx-vad', already_ready: false, total_bytes: 300, wheels: [], system_candidates: [] } })
    const wrapper = mount(ResourceInstallDialog, { props: { controller: model } })
    expect(wrapper.text()).toContain('本次将补齐轻量语音检测所需运行依赖')
    expect(wrapper.text()).not.toContain('全部语音依赖')
    expect(wrapper.text()).not.toContain('onnx-vad')
    expect(wrapper.get('.install-plan-actions button').text()).toBe('开始下载并补齐依赖')
    wrapper.unmount()
  })

  it('已就绪的轻量语音检测运行依赖显示复用说明', () => {
    const model = controller({ resource_id: 'fsmn-vad', title: 'FSMN-VAD（语音活动检测）', model_bytes: 1730986,
      environment: { group: 'onnx-vad', already_ready: true, total_bytes: 0, wheels: [], system_candidates: [] } })
    const wrapper = mount(ResourceInstallDialog, { props: { controller: model } })
    expect(wrapper.text()).toContain('轻量语音检测所需运行依赖已就绪')
    expect(wrapper.text()).not.toContain('现用完整运行环境')
    wrapper.unmount()
  })

  it('已安装模型更新沿用资源下载计划，但将确认文案标为更新', () => {
    const model = controller({ resource_id: 'fsmn-vad', title: 'FSMN-VAD（语音活动检测）', model_bytes: 1730986, environment: null }, [
      { resource_id: 'fsmn-vad', label: 'FSMN-VAD（语音活动检测）', installation_state: 'installed',
        update_available: true, installed_version: '2.0', version: '2.1' },
    ])
    const wrapper = mount(ResourceInstallDialog, { props: { controller: model } })
    expect(wrapper.get('#install-plan-title').text()).toContain('准备更新 FSMN-VAD')
    expect(wrapper.text()).toContain('仅更新模型文件')
    expect(wrapper.get('.install-plan-actions button').text()).toBe('开始更新')
    wrapper.unmount()
  })

  it('不完整的代理组件带有更新标记时仍显示更新说明和更新按钮', () => {
    const agent = controller({ resource_id: 'dsh', title: 'DeepSeek Harness 官方桌面客户端', model_bytes: 289313640, environment: null }, [
      { resource_id: 'dsh', label: 'DeepSeek Harness 官方桌面客户端', installation_state: 'incomplete',
        update_available: true, installed_version: '0.2.0-rc.1', version: '0.2.0-rc.2' },
    ])
    const wrapper = mount(ResourceInstallDialog, { props: { controller: agent } })
    expect(wrapper.get('#install-plan-title').text()).toContain('准备更新 DeepSeek Harness')
    expect(wrapper.text()).toContain('本次将更新代理执行组件文件')
    expect(wrapper.get('.install-plan-actions button').text()).toBe('开始更新')
    wrapper.unmount()
  })
})

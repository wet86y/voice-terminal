// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { useWorkspaceResources } from '../useWorkspaceResources'
import type { ResourceInstallPlan } from '../desktop-port'
import ResourceInstallDialog from './ResourceInstallDialog.vue'

function controller(plan: ResourceInstallPlan) {
  return {
    pendingDownloadPlan: ref(plan), pendingImport: ref(null), confirmDownload: vi.fn(), confirmImport: vi.fn(),
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
})

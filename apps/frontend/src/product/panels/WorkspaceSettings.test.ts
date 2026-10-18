// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { useWorkspaceResources } from '../useWorkspaceResources'
import WorkspaceSettings from './WorkspaceSettings.vue'

function controller(workspace: NonNullable<ReturnType<typeof useWorkspaceResources>['workspace']['value']>, canManage: boolean) {
  return {
    canManage: ref(canManage),
    workspace: ref(workspace),
    activeWorkspaceJob: computed(() => undefined),
    latestWorkspaceJob: computed(() => undefined),
    loading: ref(false),
    workspaceError: ref(''),
    eventError: ref(''),
    workspaceOperationStarting: ref(false),
    appUpdate: ref({ state: 'idle', current_version: '0.9.2' }),
    checkAppUpdate: vi.fn(),
    downloadAppUpdate: vi.fn(),
    cancelAppUpdate: vi.fn(),
    chooseWorkspace: vi.fn(),
    linkWorkspace: vi.fn(),
    migrateWorkspace: vi.fn(),
    cancelJob: vi.fn(),
  } as unknown as ReturnType<typeof useWorkspaceResources>
}

describe('WorkspaceSettings（工作区设置）统一正式路径', () => {
  it('本地正式程序展示工作区根与数据目录', () => {
    const wrapper = mount(WorkspaceSettings, {
      props: {
        controller: controller({
          root: 'D:\\项目开发\\语音前端\\.local',
          data_dir: 'D:\\项目开发\\语音前端\\.local\\data',
          version: '0.8.0',
          can_manage: false,
        }, false),
      },
    })

    const paths = wrapper.findAll('.workspace-paths > div')
    expect(paths).toHaveLength(2)
    expect(paths[0].find('dt').text()).toBe('工作区根目录')
    expect(paths[0].find('dd').text()).toBe('D:\\项目开发\\语音前端\\.local')
    expect(paths[1].find('dt').text()).toBe('数据目录')
    expect(paths[1].find('dd').text()).toBe('D:\\项目开发\\语音前端\\.local\\data')
    expect(wrapper.text()).toContain('正式程序')
    expect(wrapper.find('.workspace-actions').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('选择工作区位置')
    wrapper.unmount()
  })

  it('用户正式程序保留相同工作区管理操作', () => {
    const wrapper = mount(WorkspaceSettings, {
      props: {
        controller: controller({
          root: 'D:\\测试\\语音工作区',
          data_dir: 'D:\\测试\\语音工作区\\data',
          version: '0.8.0',
          can_manage: true,
        }, true),
      },
    })

    const paths = wrapper.findAll('.workspace-paths > div')
    expect(paths).toHaveLength(2)
    expect(paths[0].find('dt').text()).toBe('工作区根目录')
    expect(paths[0].find('dd').text()).toBe('D:\\测试\\语音工作区')
    expect(paths[1].find('dt').text()).toBe('数据目录')
    expect(paths[1].find('dd').text()).toBe('D:\\测试\\语音工作区\\data')
    expect(wrapper.text()).not.toContain('开发运行固定使用项目的 .local')
    expect(wrapper.text()).toContain('选择工作区位置')
    wrapper.unmount()
  })
})

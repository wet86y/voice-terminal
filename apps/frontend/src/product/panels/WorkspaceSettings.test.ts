// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
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
    integrity: ref({ state: 'idle', operation: 'integrity', installer_ready: false, fields: {} }),
    downloadIntegrity: vi.fn(), checkIntegrity: vi.fn(),
    appUpdate: ref({ state: 'idle', current_version: '0.9.3' }),
    checkAppUpdate: vi.fn(),
    downloadAppUpdate: vi.fn(),
    cancelAppUpdate: vi.fn(),
    openProjectRepository: vi.fn(),
    chooseWorkspace: vi.fn(),
    linkWorkspace: vi.fn(),
    migrateWorkspace: vi.fn(),
    cancelJob: vi.fn(),
  } as unknown as ReturnType<typeof useWorkspaceResources>
}

describe('WorkspaceSettings（关于页面）', () => {
  it('更新位于首栏，工作区只展示根目录，没有管理权限时隐藏迁移操作', () => {
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
    expect(wrapper.get('h2').text()).toBe('关于')
    const cards = wrapper.findAll('.workspace-settings > section')
    expect(cards[0].classes()).toContain('app-update-card')
    expect(cards[1].classes()).toContain('workspace-card')
    expect(paths).toHaveLength(1)
    expect(paths[0].find('dt').text()).toBe('工作区根目录')
    expect(paths[0].find('dd').text()).toBe('D:\\项目开发\\语音前端\\.local')
    expect(wrapper.get('.workspace-card').text()).not.toMatch(/数据目录|应用版本|正式程序/)
    expect(wrapper.find('.workspace-migrate-action').exists()).toBe(false)
    wrapper.unmount()
  })

  it('工作区卡片提供唯一的迁移按钮，项目仓库按钮连接桌面操作', async () => {
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
    expect(paths).toHaveLength(1)
    expect(paths[0].find('dt').text()).toBe('工作区根目录')
    expect(paths[0].find('dd').text()).toBe('D:\\测试\\语音工作区')
    expect(wrapper.get('.workspace-migrate-action').text()).toBe('迁移工作区')
    expect(wrapper.get('.workspace-card').findAll('button')).toHaveLength(1)
    expect(wrapper.text()).not.toMatch(/链接并切换|选择工作区位置|数据目录/)
    await wrapper.get('.update-repository').trigger('click')
    expect(wrapper.props('controller').openProjectRepository).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('选择目录后先确认源与目标，确认后仅调用迁移', async () => {
    const actions = controller({ root: 'D:\\旧工作区', data_dir: 'D:\\旧工作区\\data', version: '0.9.3', can_manage: true }, true)
    vi.mocked(actions.chooseWorkspace).mockResolvedValue({ root: 'E:\\新工作区' })
    const wrapper = mount(WorkspaceSettings, { props: { controller: actions } })

    await wrapper.get('.workspace-migrate-action').trigger('click')
    await flushPromises()
    expect(actions.chooseWorkspace).toHaveBeenCalledOnce()
    expect(actions.migrateWorkspace).not.toHaveBeenCalled()
    const dialog = wrapper.get('[role="dialog"]')
    expect(dialog.text()).toContain('D:\\旧工作区')
    expect(dialog.text()).toContain('E:\\新工作区')
    expect(dialog.text()).toContain('只有复制和校验成功后才会切换，并清理旧位置。')
    await dialog.get('.workspace-confirm-button').trigger('click')
    expect(actions.migrateWorkspace).toHaveBeenCalledExactlyOnceWith('E:\\新工作区')
    expect(actions.linkWorkspace).not.toHaveBeenCalled()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('取消选目录或返回确认弹窗均不迁移，重新选择时使用新目标', async () => {
    const actions = controller({ root: 'D:\\旧工作区', data_dir: 'D:\\旧工作区\\data', version: '0.9.3', can_manage: true }, true)
    vi.mocked(actions.chooseWorkspace).mockResolvedValueOnce(null)
      .mockResolvedValueOnce({ root: 'E:\\第一次选择' })
      .mockResolvedValueOnce({ root: 'F:\\重新选择' })
    const wrapper = mount(WorkspaceSettings, { props: { controller: actions } })

    await wrapper.get('.workspace-migrate-action').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    await wrapper.get('.workspace-migrate-action').trigger('click')
    await flushPromises()
    await wrapper.get('[role="dialog"] .outline-button').trigger('click')
    expect(actions.migrateWorkspace).not.toHaveBeenCalled()
    await wrapper.get('.workspace-migrate-action').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="dialog"]').text()).not.toContain('E:\\第一次选择')
    await wrapper.get('.workspace-confirm-button').trigger('click')
    expect(actions.migrateWorkspace).toHaveBeenCalledExactlyOnceWith('F:\\重新选择')
    expect(actions.linkWorkspace).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('目录选择尚未结束时锁定迁移按钮，避免重复打开选择器', async () => {
    const actions = controller({ root: 'D:\\工作区', data_dir: 'D:\\工作区\\data', version: '0.9.3', can_manage: true }, true)
    let finishChoice!: (choice: null) => void
    vi.mocked(actions.chooseWorkspace).mockImplementation(() => new Promise(resolve => { finishChoice = resolve }))
    const wrapper = mount(WorkspaceSettings, { props: { controller: actions } })

    await wrapper.get('.workspace-migrate-action').trigger('click')
    expect(wrapper.get('.workspace-migrate-action').text()).toBe('正在选择…')
    expect(wrapper.get('.workspace-migrate-action').attributes('disabled')).toBeDefined()
    await wrapper.get('.workspace-migrate-action').trigger('click')
    expect(actions.chooseWorkspace).toHaveBeenCalledOnce()
    finishChoice(null)
    await flushPromises()
    expect(wrapper.get('.workspace-migrate-action').text()).toBe('迁移工作区')
    expect(wrapper.get('.workspace-migrate-action').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })
})

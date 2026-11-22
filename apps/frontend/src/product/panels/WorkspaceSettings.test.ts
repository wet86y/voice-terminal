// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { useWorkspaceResources } from '../useWorkspaceResources'
import WorkspaceSettings from './WorkspaceSettings.vue'
import * as desktop from '../desktop-port'

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
    cleanupWorkspace: vi.fn(),
    cancelJob: vi.fn(),
  } as unknown as ReturnType<typeof useWorkspaceResources>
}

describe('WorkspaceSettings（关于页面）', () => {
  it('外部调试使用底部滑块，保存失败时恢复原开关状态', async () => {
    const save = vi.fn().mockRejectedValueOnce(new Error('无法保存调试授权')).mockResolvedValueOnce(undefined)
    const port = vi.spyOn(desktop, 'desktopPort').mockReturnValue({ available: true,
      workspaceInfo: async () => ({ allow_external_debug: true }), setAgentDebugAllowed: save } as never)
    const wrapper = mount(WorkspaceSettings, { props: { controller: controller({
      root: 'D:\\测试工作区', data_dir: 'D:\\测试工作区\\data', version: '1.0.0', can_manage: true }, true) } })
    try {
      await flushPromises()
      expect(wrapper.findAll('.workspace-settings > section').at(-1)!.classes()).toContain('debug-access-card')
      const control = wrapper.get<HTMLInputElement>('input[role="switch"][aria-label="允许外部调试"]')
      expect(control.element.checked).toBe(true)
      await control.setValue(false)
      await flushPromises()
      expect(save).toHaveBeenCalledWith(false)
      expect(control.element.checked).toBe(true)
      expect(wrapper.get('[role="alert"]').text()).toContain('无法保存调试授权')
      await control.setValue(false)
      await flushPromises()
      expect(control.element.checked).toBe(false)
    } finally { wrapper.unmount(); port.mockRestore() }
  })
  it('更新位于首栏，工作区只展示根目录，没有管理权限时隐藏迁移操作', () => {
    const wrapper = mount(WorkspaceSettings, {
      props: {
        controller: controller({
          root: 'D:\\ExampleWorkspace\\VoiceTerminal\\.local',
          data_dir: 'D:\\ExampleWorkspace\\VoiceTerminal\\.local\\data',
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
    expect(paths[0].find('dd').text()).toBe('D:\\ExampleWorkspace\\VoiceTerminal\\.local')
    expect(wrapper.get('.workspace-card').text()).not.toMatch(/数据目录|应用版本|正式程序/)
    expect(wrapper.find('.workspace-migrate-action').exists()).toBe(false)
    expect(wrapper.find('.workspace-cleanup-action').exists()).toBe(false)
    wrapper.unmount()
  })

  it('工作区卡片的清理位于迁移左侧，项目仓库按钮连接桌面操作', async () => {
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
    expect(wrapper.get('.workspace-card').findAll('button').map(button => button.text())).toEqual(['清理', '迁移工作区'])
    expect(wrapper.text()).not.toMatch(/链接并切换|选择工作区位置|数据目录/)
    await wrapper.get('.update-repository').trigger('click')
    expect(wrapper.props('controller').openProjectRepository).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('展开本机清理区域，默认不选中，且仅提交勾选项', async () => {
    const actions = controller({ root: '远端路径', data_dir: '', version: '1.1.0', can_manage: false }, false)
    const plan = { root: '本机路径', token: 'scan-1', total_bytes: 7, entries: [{ path: 'tmp/resource-downloads/example', bytes: 1024, reason: '可重新下载' }], removed_bytes: 0, removed_count: 0, skipped: [], components: [] }
    const scan = vi.fn().mockResolvedValue(plan)
    const clean = vi.fn().mockResolvedValue({ ...plan, removed_count: 1, removed_bytes: 7 })
    const port = vi.spyOn(desktop, 'desktopPort').mockReturnValue({ available: true, workspaceInfo: async () => ({root:'本机路径'}), storageScan: scan, storageCleanup: clean } as never)
    const wrapper = mount(WorkspaceSettings, { props: { controller: actions }, attachTo: document.body })
    try {
      await flushPromises()
      await wrapper.get('.workspace-cleanup-action').trigger('click')
      await flushPromises()
      expect(scan).toHaveBeenCalledExactlyOnceWith()
      expect(wrapper.get('[aria-label="本机空间管理"]').text()).toContain('本机路径')
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(wrapper.text()).toContain('本机暂无已安装的可选组件。')
      expect(wrapper.get('.storage-entry').text()).toContain('1 KB')
      expect(wrapper.get('.storage-entry').text()).not.toMatch(/字节|KiB|MiB|GiB/)
      expect(wrapper.get<HTMLButtonElement>('.workspace-confirm-button').element.disabled).toBe(true)
      await wrapper.get('input[type="checkbox"]').setValue(true)
      await wrapper.get('.workspace-confirm-button').trigger('click')
      await flushPromises()
      expect(clean).toHaveBeenCalledExactlyOnceWith('scan-1', ['tmp/resource-downloads/example'])
      expect(actions.cleanupWorkspace).not.toHaveBeenCalled()
      expect(scan).toHaveBeenCalledTimes(2)
      expect(wrapper.text()).toContain('已清理 1 项')
    } finally { wrapper.unmount(); port.mockRestore() }
  })

  it('远程模式下页内确认本机卸载，通过本机维护入口查询进度并刷新', async () => {
    const actions = Object.assign(controller({ root:'远端路径',data_dir:'',version:'1.1.0',can_manage:false },false),{remoteMode:ref(true)})
    actions.workspaceError.value = '远程服务端状态不可用，本机资源操作已停用。'
    const resource = 'tts-aniflive-fixture'
    const plan = {root:'本机路径',token:'scan',entries:[],total_bytes:0,removed_bytes:0,removed_count:0,skipped:[],components:[{resource_id:resource,label:'测试模型',bytes:64,installed:true},{resource_id:'tts-aniflive-missing',label:'未安装的模型',bytes:32,installed:false}]}
    const scan = vi.fn().mockResolvedValueOnce(plan).mockResolvedValue({...plan,components:plan.components.map(item=>({...item,installed:false}))})
    const preview = vi.fn().mockResolvedValue({resource_id:resource,token:'uninstall',message:'保留音色，移除全部模型版本。',paths:['models/aniflive-tts/packages/fixture']})
    const start = vi.fn().mockResolvedValue({job_id:'reset-uninstall-'+resource})
    const query = vi.fn().mockResolvedValue({job:{job_id:'reset-uninstall-'+resource,resource_id:resource,state:'complete',phase:'完成',error:null}})
    const legacyJobs = vi.fn().mockRejectedValue(new Error('远程模式禁止访问旧任务接口'))
    const port = vi.spyOn(desktop,'desktopPort').mockReturnValue({available:true,workspaceInfo:async()=>({root:'本机路径'}),storageScan:scan,uninstallPlan:preview,uninstallResource:start,uninstallJobStatus:query,resourceJobSnapshots:legacyJobs} as never)
    const wrapper = mount(WorkspaceSettings,{props:{controller:actions}})
    try {
      await flushPromises()
      expect(wrapper.text()).not.toContain('本机资源操作已停用')
      await wrapper.get('.workspace-cleanup-action').trigger('click');await flushPromises()
      expect(wrapper.findAll('.storage-component')).toHaveLength(1)
      expect(wrapper.text()).not.toContain('未安装的模型')
      await wrapper.get('.storage-component .outline-button').trigger('click');await flushPromises()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(wrapper.get('[aria-label="确认本机组件卸载"]').text()).toContain('保留音色')
      await wrapper.get('[aria-label="确认本机组件卸载"] .save-button').trigger('click');await flushPromises()
      expect(start).toHaveBeenCalledExactlyOnceWith(resource,'uninstall')
      expect(query).toHaveBeenCalledExactlyOnceWith(resource,'reset-uninstall-'+resource)
      expect(legacyJobs).not.toHaveBeenCalled()
      expect(scan).toHaveBeenCalledTimes(2)
      expect(wrapper.text()).toContain('卸载完成，用户资料已保留。')
      expect(wrapper.findAll('.storage-component')).toHaveLength(0)
      expect(wrapper.text()).toContain('本机暂无已安装的可选组件。')
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    } finally {wrapper.unmount();port.mockRestore()}
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

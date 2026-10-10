// @vitest-environment jsdom
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as desktop from '../product/desktop-port'
import type { ResourceJob, ResourceStatus } from '../product/desktop-port'
import ManagementApp from './ManagementApp.vue'

const row = (id: string, label: string, ready = false): ResourceStatus => ({
  resource_id: id, label, installation_state: ready ? 'installed' : 'missing',
  files_installed: ready, runtime_ready: ready, can_start: ready,
})

describe('ManagementApp（组件管理）启动校验与并行配置', () => {
  let wrapper: VueWrapper | undefined
  let inventory: ResourceStatus[]
  let jobs: ResourceJob[]
  let configured: { deepseek: boolean; minimax: boolean } // configured（固定接口配置状态）。
  let resourceEvent: ((event: { payload: ResourceJob }) => void) | undefined
  const start = vi.fn()
  const download = vi.fn()
  const resourcePlan = vi.fn()
  const inventoryRead = vi.fn()
  const environment = vi.fn()
  const saveCredentials = vi.fn()

  beforeEach(() => {
    inventory = [row('voice-server', 'Voice Server（语音服务）'), row('paraformer-streaming', 'Paraformer（流式识别）'), row('sensevoice-small', 'SenseVoice（精修模型）')]
    jobs = []
    configured = { deepseek: true, minimax: true }
    saveCredentials.mockReset().mockImplementation(async (request: { deepseek_api_key: string; minimax_api_key: string }) => {
      configured = { deepseek: configured.deepseek || Boolean(request.deepseek_api_key.trim()), minimax: configured.minimax || Boolean(request.minimax_api_key.trim()) }
      return { providers: { deepseek: { configured: configured.deepseek }, minimax: { configured: configured.minimax } }, vault_available: true }
    })
    start.mockReset().mockResolvedValue(undefined)
    download.mockReset().mockImplementation(async (id: string) => ({ job_id: `download-${id}` }))
    resourcePlan.mockReset().mockImplementation(async (id: string) => ({ resource_id: id,
      title: inventory.find(item => item.resource_id === id)?.label || id, model_bytes: 100,
      environment: id === 'voice-server'
        ? { group: 'all', already_ready: false, total_bytes: 300, wheels: [], system_candidates: [] }
        : null }))
    inventoryRead.mockReset().mockImplementation(async () => inventory)
    environment.mockReset().mockImplementation(async () => ({ interpreter_ready: false, service_ready: false, python: '', groups: {}, jobs }))
    vi.spyOn(desktop, 'desktopPort').mockReturnValue({
      available: true,
      workspaceInfo: async () => ({ root: 'D:/测试/测试123', data_dir: 'D:/测试/测试123/data', version: '0.9.0', can_manage: true }),
      resourceInventory: inventoryRead,
      environmentStatus: environment,
      resourceInstallPlan: resourcePlan,
      downloadResource: download,
      startVoiceService: start,
      fixedCredentials: async () => ({ providers: { deepseek: { configured: configured.deepseek }, minimax: { configured: configured.minimax } }, vault_available: true }),
      saveFixedCredentials: saveCredentials,
      listen: async (name: string, handler: typeof resourceEvent) => {
        if (name === desktop.RESOURCE_PROGRESS_EVENT) resourceEvent = handler
        return () => undefined
      },
      openLogs: vi.fn(), exit: vi.fn(),
    } as unknown as ReturnType<typeof desktop.desktopPort>)
  })

  afterEach(() => { wrapper?.unmount(); wrapper = undefined; resourceEvent = undefined; vi.restoreAllMocks() })

  it('组件不完整时保持管理页，返回操作也不能启动服务', async () => {
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.get('.management-start').text()).toContain('请先下载或导入缺失组件')
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    await wrapper.get('button[aria-label="返回工作台"]').trigger('click')
    expect(wrapper.text()).toContain('全部组件完备后再启动')
    expect(start).not.toHaveBeenCalled()
  })

  it('全部组件就绪后由用户手动启动', async () => {
    inventory = inventory.map(item => ({ ...item, installation_state: 'installed', files_installed: true, runtime_ready: true, can_start: true }))
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(start).not.toHaveBeenCalled()
    const button = wrapper.get('.start-program')
    expect(button.attributes('disabled')).toBeUndefined()
    await button.trigger('click')
    await flushPromises()
    expect(start).toHaveBeenCalledTimes(1)
  })

  it('待更新模型显示卡片更新操作并阻止启动，校验通过并刷新清单后才可启动', async () => {
    inventory = [
      row('voice-server', 'Voice Server（语音服务）', true),
      row('paraformer-streaming', 'Paraformer（流式识别）', true),
      { ...row('sensevoice-small', 'SenseVoice（精修模型）', true), update_available: true,
        installed_version: '1.0.0', version: '1.1.0', can_start: true },
    ]
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.management-start').text()).toContain('有组件需更新并通过校验后才能启动语音服务')
    const card = wrapper.get('[data-component="sensevoice-small"]')
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="SenseVoice（精修模型）：更新"]').text()).toBe('更新')
    expect(card.get('button[aria-label="SenseVoice（精修模型）：导入"]').text()).toBe('导入')
    expect(card.get('button[aria-label="SenseVoice（精修模型）：跳转官方来源"]').text()).toBe('跳转')
    await card.get('button[aria-label="SenseVoice（精修模型）：更新"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('#install-plan-title').text()).toContain('准备更新 SenseVoice')
    expect(wrapper.get('.install-plan-actions button').text()).toBe('开始更新')
    await wrapper.get('.install-plan-actions button').trigger('click')
    await flushPromises()
    expect(download).toHaveBeenCalledWith('sensevoice-small')
    expect(wrapper.get('[data-job="download-sensevoice-small"]').text()).toContain('正在准备下载')
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()

    inventory = inventory.map(item => item.resource_id === 'sensevoice-small'
      ? { ...item, update_available: false, can_start: true, installed_version: '1.1.0' }
      : item)
    resourceEvent?.({ payload: { job_id: 'download-sensevoice-small', resource_id: 'sensevoice-small', state: 'complete', phase: '更新校验通过', can_cancel: false } })
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeUndefined()
    await wrapper.get('.start-program').trigger('click')
    await flushPromises()
    expect(start).toHaveBeenCalledOnce()
    wrapper.unmount()
    wrapper = undefined
  })

  it('首次缺失模型仍显示下载并通过固定资源下载计划安装当前目标版本', async () => {
    inventory = [row('voice-server', 'Voice Server（语音服务）', true),
      { ...row('paraformer-streaming', 'Paraformer（流式识别）'), version: '2.0.0' },
      row('sensevoice-small', 'SenseVoice（精修模型）', true)]
    wrapper = mount(ManagementApp)
    await flushPromises()
    const card = wrapper.get('[data-component="paraformer-streaming"]')
    const button = card.get('button[aria-label="Paraformer（流式识别）：下载"]')
    expect(button.text()).toBe('下载')
    await button.trigger('click')
    await flushPromises()
    expect(wrapper.get('#install-plan-title').text()).toContain('准备下载 Paraformer')
    expect(wrapper.text()).toContain('本次下载将安装当前目标模型版本：2.0.0')
    await wrapper.get('.install-plan-actions button').trigger('click')
    await flushPromises()
    expect(download).toHaveBeenCalledWith('paraformer-streaming')
    wrapper.unmount()
    wrapper = undefined
  })

  it('模型更新校验失败仍阻止服务启动并保留重试更新操作', async () => {
    inventory = [row('voice-server', 'Voice Server（语音服务）', true),
      row('paraformer-streaming', 'Paraformer（流式识别）', true),
      { ...row('sensevoice-small', 'SenseVoice（精修模型）', true), update_available: true,
        installed_version: '1.0.0', version: '1.1.0', can_start: false }]
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    resourceEvent?.({ payload: { job_id: 'failed-update', resource_id: 'sensevoice-small', state: 'failed',
      phase: '更新校验失败', error: '更新校验失败', can_cancel: false } })
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.management-start').text()).toContain('有组件需更新并通过校验后才能启动语音服务')
    const card = wrapper.get('[data-component="sensevoice-small"]')
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="SenseVoice（精修模型）：更新"]').text()).toBe('重试更新')
    expect(start).not.toHaveBeenCalled()
    wrapper.unmount()
    wrapper = undefined
  })

  it('语音运行环境旧依赖锁显示更新入口，弹窗说明依赖更新且不称模型', async () => {
    inventory = [
      { ...row('voice-server', 'Voice Server（语音服务）', true), update_available: true,
        version: '3.12.10', can_start: true },
      row('paraformer-streaming', 'Paraformer（流式识别）', true),
      row('sensevoice-small', 'SenseVoice（精修模型）', true),
    ]
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    const card = wrapper.get('[data-component="voice-system"]')
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="Voice Server（语音服务）：更新"]').text()).toBe('更新')
    expect(card.get('button[aria-label="Voice Server（语音服务）：导入"]').text()).toBe('导入')
    expect(card.get('button[aria-label="Voice Server（语音服务）：跳转官方来源"]').text()).toBe('跳转')
    await card.get('button[aria-label="Voice Server（语音服务）：更新"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('#install-plan-title').text()).toBe('准备更新 Voice Server（语音服务）')
    expect(wrapper.text()).toContain('更新语音运行环境')
    expect(wrapper.text()).not.toContain('更新模型文件')
    expect(wrapper.text()).not.toContain('目标模型版本')
    expect(wrapper.get('.install-plan-actions button').text()).toBe('开始更新并配置')
    await wrapper.get('.install-plan-actions button').trigger('click')
    await flushPromises()
    expect(resourcePlan).toHaveBeenCalledWith('voice-server')
    expect(download).toHaveBeenCalledWith('voice-server')
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    wrapper.unmount()
    wrapper = undefined
  })

  it('恢复正在下载的任务，同时允许启动两项不同模型的下载', async () => {
    jobs = [{ job_id: 'environment-all', resource_id: 'voice-server', state: 'running', phase: '正在下载语音依赖', completed_bytes: 50, total_bytes: 100, can_cancel: true },
      { job_id: 'old-failure', resource_id: 'sensevoice-small', state: 'failed', phase: '失败', error: '旧下载失败' }]
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.get('[data-job="environment-all"]').text()).toContain('50%')
    expect(wrapper.text()).not.toContain('旧下载失败')
    for (const name of ['Paraformer（流式识别）', 'SenseVoice（精修模型）']) {
      const button = wrapper.get(`button[aria-label="${name}：下载"]`)
      expect(button.attributes('disabled')).toBeUndefined()
      await button.trigger('click')
      await flushPromises()
      await wrapper.get('.install-plan-actions button').trigger('click')
      await flushPromises()
    }
    expect(download.mock.calls.map(call => call[0])).toEqual(['paraformer-streaming', 'sensevoice-small'])
    expect(wrapper.findAll('[data-job]')).toHaveLength(3)
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    expect(environment).toHaveBeenCalledTimes(1)
    expect(start).not.toHaveBeenCalled()
  })

  it('任务快照晚到时不能覆盖先收到的完成事件', async () => {
    let resolve: (value: unknown) => void = () => undefined
    environment.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    wrapper = mount(ManagementApp)
    await flushPromises()
    resourceEvent?.({ payload: { job_id: 'stream-download', resource_id: 'paraformer-streaming', state: 'complete', phase: '完成', can_cancel: false } })
    resolve({ interpreter_ready: false, service_ready: false, python: '', groups: {}, jobs: [
      { job_id: 'stream-download', resource_id: 'paraformer-streaming', state: 'running', phase: '过期下载进度', completed_bytes: 30, total_bytes: 100 },
    ] })
    await flushPromises()
    expect(wrapper.find('[data-job="stream-download"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('过期下载进度')
  })

  it('组件和后端未就绪时可以在下载期间保存接口密钥，留空保留已有值', async () => {
    configured = { deepseek: false, minimax: true }
    jobs = [{ job_id: 'model-download', resource_id: 'paraformer-streaming', state: 'running', phase: '正在下载模型', completed_bytes: 40, total_bytes: 100 }]
    wrapper = mount(ManagementApp)
    await flushPromises()
    await wrapper.get('button[aria-label="模型管理"]').trigger('click')
    expect(wrapper.text()).toContain('组件未就绪时也可以提前填写')
    const inputs = wrapper.findAll<HTMLInputElement>('.fixed-model-credentials input')
    expect(inputs[0].attributes('disabled')).toBeUndefined()
    await inputs[0].setValue('test-language-key')
    await wrapper.get('.fixed-model-credentials').trigger('submit')
    await flushPromises()
    expect(saveCredentials).toHaveBeenCalledWith({ deepseek_api_key: 'test-language-key', minimax_api_key: '' })
    expect(inputs[0].element.value).toBe('')
    expect(wrapper.findAll('.entry-status.configured')).toHaveLength(2)
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    expect(start).not.toHaveBeenCalled()
    await wrapper.get('button[aria-label="本地组件"]').trigger('click')
    expect(wrapper.get('[data-job="model-download"]').text()).toContain('40%')
  })

  it('组件完备但缺少密钥时进入模型管理，保存完整后可以手动启动', async () => {
    inventory = inventory.map(item => ({ ...item, can_start: true }))
    configured = { deepseek: false, minimax: false }
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.find('.fixed-model-credentials').exists()).toBe(true)
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    await wrapper.get('button[aria-label="返回工作台"]').trigger('click')
    expect(wrapper.text()).toContain('请先填写 DeepSeek')
    const inputs = wrapper.findAll<HTMLInputElement>('.fixed-model-credentials input')
    await inputs[0].setValue('test-language-key')
    await wrapper.get('.fixed-model-credentials').trigger('submit')
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    await inputs[1].setValue('test-speech-key')
    await wrapper.get('.fixed-model-credentials').trigger('submit')
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeUndefined()
    await wrapper.get('.start-program').trigger('click')
    expect(start).toHaveBeenCalledTimes(1)
  })

  it('密钥保存失败时保留草稿并显示错误，不启动后端', async () => {
    configured = { deepseek: false, minimax: false }
    saveCredentials.mockRejectedValueOnce(new Error('系统凭据存储不可用'))
    wrapper = mount(ManagementApp)
    await flushPromises()
    await wrapper.get('button[aria-label="模型管理"]').trigger('click')
    await wrapper.get('.fixed-model-credentials input').setValue('test-draft-key')
    await wrapper.get('.fixed-model-credentials').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('系统凭据存储不可用')
    expect(wrapper.get<HTMLInputElement>('.fixed-model-credentials input').element.value).toBe('test-draft-key')
    expect(start).not.toHaveBeenCalled()
  })
})

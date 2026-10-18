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

  async function expandCategory(category: string) {
    const toggle = wrapper!.get(`[data-category="${category}"] .local-category-toggle`)
    if (toggle.attributes('aria-expanded') !== 'true') await toggle.trigger('click')
  }

  async function expandComponent(id: string, category: string) {
    await expandCategory(category)
    const card = wrapper!.get(`[data-component="${id}"]`)
    const toggle = card.find('.local-component-action')
    if (toggle.exists() && toggle.attributes('aria-expanded') !== 'true') await toggle.trigger('click')
    return card
  }

  beforeEach(() => {
    inventory = [row('voice-server', 'Voice Server（语音服务）'), row('paraformer-streaming', 'Paraformer（流式识别）'), row('sensevoice-small', 'SenseVoice（精修模型）')]
    jobs = []
    configured = { deepseek: true, minimax: true }
    saveCredentials.mockReset().mockImplementation(async (request: { deepseek_api_key: string; minimax_api_key: string }) => {
      configured = { deepseek: configured.deepseek || Boolean(request.deepseek_api_key?.trim()), minimax: configured.minimax || Boolean(request.minimax_api_key?.trim()) }
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
      exportLogs: vi.fn(), exit: vi.fn(),
    } as unknown as ReturnType<typeof desktop.desktopPort>)
  })

  afterEach(() => { wrapper?.unmount(); wrapper = undefined; resourceEvent = undefined; vi.restoreAllMocks(); vi.useRealTimers() })

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

  it('语音运行依赖分为三个子卡片，可选依赖默认不勾选且不阻塞启动', async () => {
    inventory = inventory.map(item => ({ ...item, installation_state: 'installed', files_installed: true, runtime_ready: true, can_start: true }))
    inventory[0]!.runtime_components = [
      { ...row('voice-runtime-cpu', 'CPU（中央处理器）运行依赖', true), required: true },
      { ...row('tts-audiocpp-runtime', 'audio.cpp（语音合成引擎）运行依赖'), required: false },
      { ...row('voice-runtime-gpu', 'GPU（图形处理器）运行依赖'), required: false },
    ]
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeUndefined()
    const voice = await expandComponent('voice-system', 'voice-server')
    expect(voice.findAll('[data-runtime-dependency]')).toHaveLength(3)
    await wrapper.get('[data-category="voice-server"] .category-download').trigger('click')
    await flushPromises()
    const options = wrapper.findAll('.install-bundle-choice input')
    expect(options).toHaveLength(2)
    expect(options.every(option => !(option.element as HTMLInputElement).checked)).toBe(true)
    expect(wrapper.get('.install-plan-actions button').attributes('disabled')).toBeDefined()
    const gpu = wrapper.get('.install-bundle-choice input').element as HTMLInputElement
    // 先选择任一可选依赖，再按其资源编号启动现有安装任务。
    await options[1]!.setValue(true)
    await wrapper.get('.install-plan-actions button').trigger('click')
    await flushPromises()
    expect(download).toHaveBeenCalledWith('voice-runtime-gpu')
    expect(download).not.toHaveBeenCalledWith('voice-runtime-cpu')
    expect(gpu.type).toBe('checkbox')
  })

  it('可选 CAM++（声纹模型）缺失或待更新不禁用启动按钮', async () => {
    inventory = inventory.map(item => ({ ...item, installation_state: 'installed', files_installed: true, runtime_ready: true, can_start: true }))
    inventory.push({ ...row('campplus-zh-cn-common', 'CAM++（可选声纹模型）'), required: false, update_available: true })
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeUndefined()
    const campplus = await expandComponent('campplus', 'extensions')
    expect(campplus.get('.local-state').text()).toBe('需更新')
    await wrapper.get('.start-program').trigger('click')
    expect(start).toHaveBeenCalledOnce()
  })

  it('首次下载完成赶上旧清单请求时，再读最新状态并可直接启动加载画面', async () => {
    const previous = inventory.map(item => ({ ...item }))
    let resolveInventory: (rows: ResourceStatus[]) => void = () => undefined
    inventoryRead.mockImplementationOnce(() => new Promise(resolve => { resolveInventory = resolve }))
    wrapper = mount(ManagementApp)
    await flushPromises()
    inventory = inventory.map(item => ({ ...item, installation_state: 'installed', files_installed: true, runtime_ready: true, can_start: true }))
    resourceEvent?.({ payload: { job_id: 'first-install', resource_id: 'voice-server', state: 'complete', phase: '就绪', created_at: 100, revision: 3, installed_resources: ['voice-server'] } })
    resolveInventory(previous)
    await flushPromises()
    expect(inventoryRead).toHaveBeenCalledTimes(2)
    const button = wrapper.get('.start-program')
    expect(button.attributes('disabled')).toBeUndefined()
    let finishStart: () => void = () => undefined
    start.mockImplementationOnce(() => new Promise<void>(resolve => { finishStart = resolve }))
    await button.trigger('click')
    expect(wrapper.get('[aria-label="应用启动画面"]').text()).toContain('正在准备启动并校验本地组件')
    expect(wrapper.get('.startup-progress').attributes('data-indeterminate')).toBe('true')
    await button.trigger('click')
    expect(start).toHaveBeenCalledOnce()
    finishStart()
    await flushPromises()
  })

  it('丢失完成事件时两秒补查终态和组件就绪，卸载后停止计时', async () => {
    vi.useFakeTimers()
    jobs = [{ job_id: 'first-install', resource_id: 'voice-server', state: 'running', phase: '下载中', created_at: 100, revision: 2 }]
    const snapshots = vi.fn(async () => jobs)
    Object.assign(desktop.desktopPort(), { resourceJobSnapshots: snapshots })
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    inventory = inventory.map(item => ({ ...item, installation_state: 'installed', files_installed: true, runtime_ready: true, can_start: true }))
    jobs = [{ ...jobs[0], state: 'complete', phase: '下载就绪', revision: 3, installed_resources: ['voice-server'] }]
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(snapshots).toHaveBeenCalledTimes(2)
    expect(wrapper.get('.start-program').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-job="first-install"]').exists()).toBe(false)
    await wrapper.get('.start-program').trigger('click')
    expect(start).toHaveBeenCalledOnce()
    wrapper.unmount(); wrapper = undefined
    const count = snapshots.mock.calls.length
    await vi.advanceTimersByTimeAsync(20000)
    expect(snapshots).toHaveBeenCalledTimes(count)
  })

  it('共享环境启用后立即刷新对应卡片，后续模型下载仍阻止整体启动', async () => {
    wrapper = mount(ManagementApp)
    await flushPromises()
    resourceEvent?.({ payload: { job_id: 'shared-install', resource_id: 'paraformer-streaming', state: 'running', phase: '配置共享环境', created_at: 100, revision: 2 } })
    expect(inventoryRead).toHaveBeenCalledOnce()
    inventory = [ { ...row('voice-server', 'Voice Server（语音服务）', true), can_start: false }, ...inventory.slice(1) ]
    resourceEvent?.({ payload: { job_id: 'shared-install', resource_id: 'paraformer-streaming', state: 'running', phase: '下载模型文件', created_at: 100, revision: 3, installed_resources: ['voice-server'] } })
    await flushPromises()
    expect(inventoryRead).toHaveBeenCalledTimes(2)
    await expandCategory('voice-server')
    expect(wrapper.get('[data-component="voice-system"] .local-state').text()).toBe('已就绪')
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
  })

  it('旧修订事件和上一轮下载不会覆盖完成状态，新一轮仍可恢复活动进度', async () => {
    inventory = inventory.map(item => ({ ...item, can_start: true }))
    wrapper = mount(ManagementApp)
    await flushPromises()
    const send = (state: string, created_at: number, revision: number) => resourceEvent?.({ payload: {
      job_id: 'same-download', resource_id: 'voice-server', state, phase: state === 'complete' ? '就绪' : '下载中', created_at, revision,
    } })
    send('complete', 100, 4)
    send('running', 100, 3)
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeUndefined()
    send('running', 200, 1)
    send('complete', 100, 10)
    await flushPromises()
    const voice = await expandComponent('voice-system', 'voice-server')
    expect(voice.get('[data-job="same-download"]').text()).toContain('下载中')
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    send('complete', 200, 2)
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeUndefined()
  })

  it('空闲时十秒自动刷新，刷新请求串行且页面聚焦立即补查', async () => {
    vi.useFakeTimers()
    const snapshots = vi.fn(async () => jobs)
    Object.assign(desktop.desktopPort(), { resourceJobSnapshots: snapshots })
    wrapper = mount(ManagementApp)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(9999)
    expect(inventoryRead).toHaveBeenCalledOnce()
    inventory = inventory.map(item => ({ ...item, can_start: true }))
    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    expect(inventoryRead).toHaveBeenCalledTimes(2)
    expect(wrapper.get('.start-program').attributes('disabled')).toBeUndefined()
    window.dispatchEvent(new Event('focus'))
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()
    expect(inventoryRead).toHaveBeenCalledTimes(3)
  })

  it('其他组件持续发进度时仍按两秒补查，不推迟丢失终态的恢复', async () => {
    vi.useFakeTimers()
    jobs = [ { job_id: 'environment', resource_id: 'voice-server', state: 'running', phase: '下载环境', created_at: 100, revision: 1 },
      { job_id: 'model', resource_id: 'paraformer-streaming', state: 'running', phase: '下载模型', created_at: 100, revision: 1 } ]
    const snapshots = vi.fn(async () => jobs)
    Object.assign(desktop.desktopPort(), { resourceJobSnapshots: snapshots })
    wrapper = mount(ManagementApp)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    resourceEvent?.({ payload: { ...jobs[1], revision: 2 } })
    jobs = [ { ...jobs[0], state: 'complete', revision: 2 }, { ...jobs[1], revision: 2 } ]
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(snapshots).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-job="environment"]').exists()).toBe(false)
    const voice = await expandComponent('voice-system', 'voice-server')
    const model = await expandComponent('paraformer', 'speech')
    expect(voice.find('[data-job="environment"]').exists()).toBe(false)
    expect(model.find('[data-job="model"]').exists()).toBe(true)
  })

  it('磁盘清单读取超过两秒时，持续进度不叠加补查或不断丢弃当前结果', async () => {
    vi.useFakeTimers()
    jobs = [{ job_id: 'slow-disk', resource_id: 'paraformer-streaming', state: 'running', phase: '下载模型', created_at: 100, revision: 1 }]
    const snapshots = vi.fn(async () => jobs)
    Object.assign(desktop.desktopPort(), { resourceJobSnapshots: snapshots })
    wrapper = mount(ManagementApp)
    await flushPromises()
    let finishRead!: (rows: ResourceStatus[]) => void
    inventoryRead.mockImplementationOnce(() => new Promise<ResourceStatus[]>(resolve => { finishRead = resolve }))
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    for (let revision = 2; revision <= 4; revision += 1) {
      await vi.advanceTimersByTimeAsync(1000)
      resourceEvent?.({ payload: { ...jobs[0], revision } })
    }
    expect(snapshots).toHaveBeenCalledTimes(2)
    expect(inventoryRead).toHaveBeenCalledTimes(2)
    inventory = inventory.map(item => ({ ...item, can_start: true, files_installed: true, runtime_ready: true, installation_state: 'installed' }))
    finishRead(inventory)
    await flushPromises()
    expect(inventoryRead).toHaveBeenCalledTimes(2)
    await expandCategory('voice-server')
    expect(wrapper.get('[data-component="voice-system"] .local-state').text()).toBe('已就绪')
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
    const card = await expandComponent('sensevoice', 'speech')
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="SenseVoice（精修模型）：更新"]').text()).toBe('更新')
    expect(card.get('button[aria-label="SenseVoice（精修模型）：导入"]').text()).toBe('导入')
    expect(card.get('button[aria-label="SenseVoice（精修模型）：跳转官方来源"]').text()).toBe('跳转来源')
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
    const card = await expandComponent('paraformer', 'speech')
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
    const card = await expandComponent('sensevoice', 'speech')
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="SenseVoice（精修模型）：重试更新"]').text()).toBe('重试更新')
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
    const card = await expandComponent('voice-system', 'voice-server')
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="Voice Server（语音服务）：更新"]').text()).toBe('更新')
    expect(card.get('button[aria-label="Voice Server（语音服务）：导入"]').text()).toBe('导入')
    expect(card.get('button[aria-label="Voice Server（语音服务）：跳转官方来源"]').text()).toBe('跳转来源')
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
    const voice = await expandComponent('voice-system', 'voice-server')
    expect(voice.get('[data-job="environment-all"]').text()).toContain('50%')
    expect(wrapper.text()).not.toContain('旧下载失败')
    for (const name of ['Paraformer（流式识别）', 'SenseVoice（精修模型）']) {
      const id = name.startsWith('Paraformer') ? 'paraformer' : 'sensevoice'
      const card = await expandComponent(id, 'speech')
      const button = card.get(`button[aria-label="${name}：下载"]`)
      expect(button.attributes('disabled')).toBeUndefined()
      await button.trigger('click')
      await flushPromises()
      await wrapper.get('.install-plan-actions button').trigger('click')
      await flushPromises()
    }
    expect(download.mock.calls.map(call => call[0])).toEqual(['paraformer-streaming', 'sensevoice-small'])
    expect(wrapper.findAll('[data-job]')).toHaveLength(3)
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    expect(environment).toHaveBeenCalledTimes(3) // 初始恢复和两次启动任务均补查；正式宿主使用轻量任务接口。
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

  it('组件未就绪时仍可在推理组件详情保存 DeepSeek 密钥', async () => {
    configured = { deepseek: false, minimax: true }
    jobs = [{ job_id: 'model-download', resource_id: 'paraformer-streaming', state: 'running', phase: '正在下载模型', completed_bytes: 40, total_bytes: 100 }]
    wrapper = mount(ManagementApp)
    await flushPromises()
    await wrapper.get('[data-category="inference"] .local-category-toggle').trigger('click')
    await wrapper.get('[data-component="deepseek"] .local-component-action').trigger('click')
    const input = wrapper.get<HTMLInputElement>('[data-provider="deepseek"] .provider-key-settings input')
    expect(input.attributes('disabled')).toBeUndefined()
    await input.setValue('test-language-key')
    await wrapper.get('[data-provider="deepseek"] .save-button').trigger('click')
    await flushPromises()
    expect(saveCredentials).toHaveBeenCalledWith({ deepseek_api_key: 'test-language-key' })
    expect(input.element.value).toBe('')
    expect(wrapper.find('[data-provider="deepseek"] .provider-key-state').text()).toBe('已配置')
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    expect(start).not.toHaveBeenCalled()
    const model = await expandComponent('paraformer', 'speech')
    expect(model.get('[data-job="model-download"]').text()).toContain('40%')
  })

  it('组件完备但缺少密钥时可在 DeepSeek 与 MiniMax 详情配置后启动', async () => {
    inventory = inventory.map(item => ({ ...item, can_start: true }))
    configured = { deepseek: false, minimax: false }
    wrapper = mount(ManagementApp)
    await flushPromises()
    expect(wrapper.find('[data-provider="deepseek"]').exists()).toBe(false)
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    await wrapper.get('button[aria-label="返回工作台"]').trigger('click')
    expect(wrapper.text()).toContain('请配置 DeepSeek 接口密钥')
    await wrapper.get('[data-category="inference"] .local-category-toggle').trigger('click')
    await wrapper.get('[data-component="deepseek"] .local-component-action').trigger('click')
    const deepseek = wrapper.get<HTMLInputElement>('[data-provider="deepseek"] .provider-key-settings input')
    await deepseek.setValue('test-language-key')
    await wrapper.get('[data-provider="deepseek"] .save-button').trigger('click')
    await flushPromises()
    expect(wrapper.get('.start-program').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-category="tts"] .local-category-toggle').trigger('click')
    await wrapper.get('[data-component="minimax"] .local-component-action').trigger('click')
    const minimax = wrapper.get<HTMLInputElement>('[data-provider="minimax"] .provider-key-settings input')
    await minimax.setValue('test-speech-key')
    await wrapper.get('[data-provider="minimax"] .save-button').trigger('click')
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
    await wrapper.get('[data-category="inference"] .local-category-toggle').trigger('click')
    await wrapper.get('[data-component="deepseek"] .local-component-action').trigger('click')
    await wrapper.get('[data-provider="deepseek"] .provider-key-settings input').setValue('test-draft-key')
    await wrapper.get('[data-provider="deepseek"] .save-button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('系统凭据存储不可用')
    expect(wrapper.get<HTMLInputElement>('[data-provider="deepseek"] .provider-key-settings input').element.value).toBe('test-draft-key')
    expect(start).not.toHaveBeenCalled()
  })
})

// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createSSRApp, h, ref } from 'vue'
import { renderToString } from 'vue/server-renderer'
import ProductApp from './ProductApp.vue'
import UiDemoApp from '../ui-demo/UiDemoApp.vue'
import Workbench from '../interface/Workbench.vue'
import UiDialog from '../interface/UiDialog.vue'
import { settingsSections } from '../interface/settings'
import { productApi, ProductApiError } from './api'
import { useProductAdapter } from './useProductAdapter'
import { useProductControls } from './useProductControls'
import * as desktopBridge from './desktop-port'
import ComputeSettings from './panels/ComputeSettings.vue'
import ModelSettings from './panels/ModelSettings.vue'
import WakeTasksSettings from './panels/WakeTasksSettings.vue'
import SpeakerSettings from './panels/SpeakerSettings.vue'
import { provideProductContext } from './product-context'
import { useWorkspaceResources } from './useWorkspaceResources'
import type { WorkbenchModel } from '../interface/workbench'
import type { ApplicationSnapshot } from './application-host'
import { DEFAULT_REGISTRY, DEFAULT_SETTINGS } from './normalization'
import { APPLICATION_EXIT_EVENT } from '../runtime/application-motion'
const application = {
  managed: ref(false), snapshot: ref<ApplicationSnapshot | null>(null), businessReady: ref(false), overlay: ref(false),
  error: ref(''), now: ref(0), retryingAll: ref(false), connected: ref(false), inspecting: ref(false),
  start: vi.fn(), stop: vi.fn(), inspect: vi.fn(), refresh: vi.fn(), exit: vi.fn(), retry: vi.fn(), retryFailed: vi.fn(),
}

vi.mock('./application-host', () => ({
  applicationStateLabel: (state: string) => state,
  useApplicationHost: () => application,
}))
const mounted: ReturnType<typeof mount>[] = []
function render(component: Parameters<typeof mount>[0], options?: Parameters<typeof mount>[1]) {
  const wrapper = mount(component, options); mounted.push(wrapper); return wrapper
}
function button(wrapper: ReturnType<typeof mount>, label: string) {
  const result = wrapper.findAll('button').find(item => item.text().includes(label))
  if (!result) throw new Error(`页面缺少操作：${label}`)
  return result
}
async function adapter() {
  let value!: ReturnType<typeof useProductAdapter>
  await renderToString(createSSRApp({ setup() { value = useProductAdapter(); return () => h('div') } }))
  return value
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function liveAdapter() {
  let value!: ReturnType<typeof useProductAdapter>
  const wrapper = render({ setup() { value = useProductAdapter(); return () => h('div') } })
  return { value, wrapper }
}
function liveControls() {
  let value!: ReturnType<typeof useProductControls>
  const wrapper = render({ setup() { value = useProductControls(); return () => h('div') } })
  return { value, wrapper }
}
function allowBusinessReady(value: { voice: ReturnType<typeof useProductAdapter>['voice'] }) {
  vi.spyOn(productApi, 'snapshot').mockResolvedValue({
    settings: structuredClone(DEFAULT_SETTINGS), models: structuredClone(DEFAULT_REGISTRY),
  } as never)
  vi.spyOn(productApi, 'speakerStatus').mockResolvedValue({})
  vi.spyOn(productApi, 'speakerProfile').mockResolvedValue({})
  vi.spyOn(productApi, 'ambientContext').mockResolvedValue({})
  vi.spyOn(productApi, 'computeStatus').mockResolvedValue({} as never)
  vi.spyOn(productApi, 'wakeTasks').mockResolvedValue({ tasks: [] } as never)
  // 伪造已连接的测试适配器，避免对本机服务发起 WebSocket（网页套接字）请求。
  value.voice.connection.value = 'connected'
  application.businessReady.value = true
}
beforeEach(() => {
  vi.spyOn(productApi, 'stopSpeakerCapture').mockResolvedValue({ stopped: true })
  history.replaceState({}, '', '/'); vi.useFakeTimers()
  window.__VOICE_APPLICATION__ = { managed: true, origin: 'http://127.0.0.1:42123',
    voice_api_url: 'http://127.0.0.1:43876', voice_ws_url: 'ws://127.0.0.1:43876/ws' }
  application.managed.value = true; application.snapshot.value = null; application.businessReady.value = false
  application.overlay.value = false; application.inspect.mockClear()
  application.inspecting.value = false
})
afterEach(() => {
  mounted.splice(0).forEach(wrapper => wrapper.unmount())
  Reflect.deleteProperty(window, '__VOICE_APPLICATION__')
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks()
})
describe('正式界面的真实控件交互', () => {
  it('声纹阈值只保存页面草稿，越过旧界限后保存按钮仍可用并提交放宽量', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(SpeakerSettings) } })
    allowBusinessReady(value)
    const save = vi.spyOn(productApi, 'saveSpeaker').mockImplementation(async submitted => ({ settings: { speaker: submitted } } as never))
    await flushPromises()
    const sliders = wrapper.findAll('.speaker-thresholds input[type="range"]')
    await sliders[0]!.setValue('0.30')
    await sliders[1]!.setValue('0.20')
    await flushPromises()
    expect(save).not.toHaveBeenCalled()
    expect(button(wrapper, '保存声纹阈值').attributes('disabled')).toBeUndefined()
    await button(wrapper, '保存声纹阈值').trigger('click')
    await flushPromises()
    expect(save).toHaveBeenCalledOnce()
    expect(save.mock.calls[0]![0]).toMatchObject({ confirm_threshold: 0.30, continuity_relaxation: 0.20 })
    expect((save.mock.calls[0]![0] as { continuity_threshold: number }).continuity_threshold).toBeCloseTo(0.10)
    expect(value.settings.speaker.confirm_threshold).toBe(0.30)
    expect(value.speakerForm.continuity_relaxation).toBe(0.20)
  })

  it('DSH（代理执行宿主）轨迹保留所有当前工具和输出，授权按钮携带原请求与运行序号', async () => {
    const { value } = liveControls()
    value.voice.connection.value = 'connected'
    value.voice.shadowProduct.value.backend = 'dsh'
    value.voice.shadowProduct.value.activities = [
      { run_seq: 3, item_id: 'tool-a', stream: 'tool', phase: 'completed', timestamp: '', tool_name: 'bash', text: 'echo demo' },
      { run_seq: 3, item_id: 'job-a', stream: 'output', phase: 'delta', timestamp: '', tool_name: '命令输出', text: 'demo\n' },
      { run_seq: 3, item_id: 'approval-a', stream: 'approval', phase: 'started', timestamp: '', approval_id: 'request-a', text: '需要执行目录写入', tool_name: 'bash' },
    ]
    const decide = vi.spyOn(productApi, 'decideDshApproval').mockResolvedValue({ result: 'accepted' })
    const wrapper = render(Workbench, { props: { model: value.workbenchModel.value }, attrs: {
      onApproval: (id: string, runSeq: number, approved: boolean) => value.decideDshApproval(id, runSeq, approved),
    } })
    expect(value.workbenchModel.value.shadow.trace).toHaveLength(3)
    expect(wrapper.get('.trace-permission-content').text()).toContain('需要执行目录写入')
    await button(wrapper, '同意').trigger('click')
    await flushPromises()
    expect(decide).toHaveBeenCalledWith({ approval_id: 'request-a', run_seq: 3, decision: 'allowed-once' })
    await value.decideDshApproval('old-request', 2, false)
    expect(decide).toHaveBeenCalledOnce()
  })

  it('主人打断开关直接保存，待机时显示已启用并保留未保存的阈值', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(SpeakerSettings) } })
    allowBusinessReady(value)
    vi.spyOn(productApi, 'speakerProfile').mockResolvedValue({ available: true, templates: [] })
    vi.spyOn(productApi, 'ambientContext').mockResolvedValue({})
    vi.spyOn(productApi, 'speakerStatus').mockResolvedValue({ registered: true, owner_only_barge_in_selected: true, owner_only_barge_in_ready: true,
      owner_only_barge_in_effective: false, owner_only_barge_in_reason: '已启用，等待播放期间验证回声保护' })
    value.speakerForm.confirm_threshold = 0.85
    const saved = { ...value.settings.speaker, owner_only_barge_in: true }
    const save = vi.spyOn(productApi, 'saveSpeaker').mockResolvedValue({ settings: { speaker: saved } } as never)
    await flushPromises()
    await wrapper.get('input[role="switch"]').setValue(true)
    await flushPromises()
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith(saved)
    expect(value.settings.speaker.owner_only_barge_in).toBe(true)
    expect(value.speakerForm.confirm_threshold).toBe(0.85)
    expect(wrapper.get('.speaker-barge-status strong').text()).toBe('主人打断已启用')
    expect(wrapper.get('.speaker-barge-status').text()).not.toContain('不可用')
  })

  it('主人打断自动保存失败恢复开关，并显示服务端原因', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(SpeakerSettings) } })
    allowBusinessReady(value)
    vi.spyOn(productApi, 'speakerProfile').mockResolvedValue({ available: true, templates: [] })
    vi.spyOn(productApi, 'saveSpeaker').mockRejectedValue(new Error('声纹工作进程启动失败'))
    await flushPromises()
    await wrapper.get('input[role="switch"]').setValue(true)
    await flushPromises()
    expect(value.speakerForm.owner_only_barge_in).toBe(false)
    expect(value.settings.speaker.owner_only_barge_in).toBe(false)
    expect(value.lastError.value).toContain('声纹工作进程启动失败')
    expect(wrapper.get('input[role="switch"]').element).toHaveProperty('checked', false)
  })

  it('声纹运行模式选择直接应用，无需再次点击页面底部保存', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(SpeakerSettings) } })
    allowBusinessReady(value)
    const save = vi.spyOn(productApi, 'saveSpeaker').mockResolvedValue({ settings: { speaker: { ...DEFAULT_SETTINGS.speaker, mode: 'observe' } } } as never)
    await flushPromises()
    await wrapper.get('select').setValue('observe')
    await flushPromises()
    expect(save).toHaveBeenCalledTimes(1)
    expect(save.mock.calls[0]![0]).toMatchObject({ mode: 'observe', owner_only_barge_in: false })
    expect(value.settings.speaker.mode).toBe('observe')
  })

  it('保存开关前的迟到声纹状态不能覆盖保存后的就绪结果', async () => {
    const { value } = liveAdapter()
    allowBusinessReady(value)
    await flushPromises()
    const stale = deferred<Record<string, unknown>>()
    vi.mocked(productApi.speakerStatus).mockReturnValueOnce(stale.promise)
      .mockResolvedValue({ owner_only_barge_in_selected: true, owner_only_barge_in_ready: true })
    const oldRefresh = value.refreshSpeaker()
    vi.spyOn(productApi, 'saveSpeaker').mockResolvedValue({ settings: { speaker: { ...DEFAULT_SETTINGS.speaker, owner_only_barge_in: true } } } as never)
    value.speakerForm.owner_only_barge_in = true
    await value.saveSpeakerControls()
    stale.resolve({ owner_only_barge_in_selected: false, owner_only_barge_in_ready: false })
    await oldRefresh
    expect(value.speakerStatus.value.owner_only_barge_in_ready).toBe(true)
    expect(value.settings.speaker.owner_only_barge_in).toBe(true)
  })

  it('加载页只展示当前组件，跳过后用一秒动画进入主页且继续等待完整启动', async () => {
    application.overlay.value = true
    application.snapshot.value = { state: 'starting', startup_complete: false, core_available: false,
      total_components: 9, completed_components: 3,
      components: [{ id: 'asr', name: 'ASR（语音识别）', state: 'starting', phase: '加载语音识别模型' },
        { id: 'dsh', name: 'DSH（代理执行宿主）', state: 'waiting', phase: '等待启动' }] } as ApplicationSnapshot
    application.inspect.mockImplementationOnce(() => { application.inspecting.value = true; application.overlay.value = false })
    const wrapper = render(ProductApp)
    const loading = wrapper.get('.application-startup')
    expect(loading.text()).toContain('应用启动中')
    expect(loading.text()).toContain('正在启动语音识别')
    expect(loading.find('ul').exists()).toBe(false)
    expect(loading.text()).not.toContain('已耗时')
    await loading.get('button').trigger('click')
    expect(wrapper.get('.application-presentation').attributes('data-motion')).toBe('entering')
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.find('.application-startup').exists()).toBe(false)
    expect(wrapper.get('.workbench').attributes('style') ?? '').not.toContain('display: none')
    expect(wrapper.get('.voice-session-action').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.settings-page').attributes('style')).toContain('display: none')
    await wrapper.get('.compact-settings-button').trigger('click')
    await button(wrapper, '本地组件').trigger('click')
    expect(wrapper.get('.local-components').text()).toContain('本地组件')
    expect(wrapper.get('[data-component="paraformer"]').text()).toContain('加载语音识别模型')
    expect(wrapper.get('[data-component="dsh"]').text()).toContain('等待启动')
    expect(application.snapshot.value.startup_complete).toBe(false)
  })

  it('首屏已跳过时进入主页，完整启动后不重复播放入场动画', async () => {
    application.inspecting.value = true
    application.snapshot.value = { state: 'starting', startup_complete: false, components: [] } as unknown as ApplicationSnapshot
    const wrapper = render(ProductApp)
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.get('.workbench').attributes('style') ?? '').not.toContain('display: none')
    expect(wrapper.get('.voice-session-action').attributes('disabled')).toBeDefined()
    application.snapshot.value = { ...application.snapshot.value, state: 'ready', startup_complete: true }
    await flushPromises()
    expect(wrapper.get('.application-presentation').attributes('data-motion')).toBe('shown')
  })

  it('原生退出通知立即显示退出滚动条，旧错误不遮挡退出画面', async () => {
    application.error.value = '上一轮连接错误'
    const wrapper = render(ProductApp)
    await vi.advanceTimersByTimeAsync(1000)
    window.dispatchEvent(new Event(APPLICATION_EXIT_EVENT))
    await flushPromises()
    const screen = wrapper.get('.application-startup')
    expect(screen.classes()).toContain('is-exiting')
    expect(screen.text()).toContain('正在退出应用')
    expect(screen.text()).not.toContain('上一轮连接错误')
    expect(screen.get('.startup-progress').attributes('data-indeterminate')).toBe('true')
    expect(screen.get('.startup-progress').attributes('aria-label')).toBe('应用退出进度')
    expect(screen.get('.startup-progress').attributes('aria-valuenow')).toBeUndefined()
    expect(screen.findAll('button')).toHaveLength(0)
    application.error.value = ''
  })

  it('桌面下载期间后台服务停止不会触发应用退出动画或阻断组件操作', async () => {
    vi.spyOn(desktopBridge, 'desktopPort').mockReturnValue({
      available: true,
      workspaceInfo: async () => ({ can_manage: true }),
      resourceInventory: async () => [],
      environmentStatus: async () => ({ jobs: [{ job_id: 'dl-streaming', resource_id: 'paraformer-streaming',
        state: 'running', phase: '正在下载模型文件', completed_bytes: 350098353, total_bytes: 889112166, can_cancel: true }] }),
      listen: async () => () => undefined,
    } as unknown as ReturnType<typeof desktopBridge.desktopPort>)
    const wrapper = render(ProductApp)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    application.snapshot.value = { state: 'stopping', stopping: true, components: [] } as unknown as ApplicationSnapshot
    application.overlay.value = true
    await flushPromises()
    expect(wrapper.get('.application-presentation').attributes('data-motion')).toBe('shown')
    expect(wrapper.get('.application-presentation').attributes('inert')).toBeUndefined()
    expect(wrapper.find('.application-startup').exists()).toBe(false)
    await wrapper.get('.compact-settings-button').trigger('click')
    expect(wrapper.get('.settings-page').attributes('style') ?? '').not.toContain('display: none')
    window.dispatchEvent(new Event(APPLICATION_EXIT_EVENT))
    await flushPromises()
    expect(wrapper.get('.application-presentation').attributes('data-motion')).toBe('exiting')
  })

  it('语音接口已连接但代理仍启动时，设置可访问，语音开始、重置和模式切换仍受保护', async () => {
    const { value } = liveControls()
    allowBusinessReady(value)
    application.snapshot.value = { state: 'starting', startup_complete: false, core_available: true,
      components: [{ id: 'voice', name: '语音服务', state: 'ready' }, { id: 'dsh', name: '代理服务', state: 'starting' }] } as ApplicationSnapshot
    const start = vi.spyOn(value.voice, 'toggleListening')
    const reset = vi.spyOn(value.voice, 'resetSession')
    const mode = vi.spyOn(value.voice, 'setReasoningMode')
    const enrollment = vi.spyOn(productApi, 'startSpeakerEnrollment')
    const speakerTest = vi.spyOn(productApi, 'captureSpeakerTest')
    expect(value.workbenchModel.value.disabled).toBe(true)
    expect(value.workbenchModel.value.action).toBe('组件启动中')
    value.runtimeAction(); value.resetSession(); value.setReasoningMode('deep')
    expect(start).not.toHaveBeenCalled(); expect(reset).not.toHaveBeenCalled(); expect(mode).not.toHaveBeenCalled()
    await value.beginSpeakerRecording(0, '第一段'); await value.beginSpeakerTest()
    expect(enrollment).not.toHaveBeenCalled(); expect(speakerTest).not.toHaveBeenCalled()
    value.showApplicationSystem()
    expect(value.activeSettingsTab.value).toBe('compute')
    application.snapshot.value = { ...application.snapshot.value, startup_complete: true, state: 'ready' }
    await flushPromises()
    expect(value.workbenchModel.value.action).toBe('开始语音会话')
  })
  it('固定供应商密钥只提交输入字段，失败保留草稿，成功清空输入', async () => {
    vi.spyOn(productApi, 'fixedCredentials').mockResolvedValue({ providers: { deepseek: { configured: true }, minimax: { configured: true } }, vault_available: true })
    const save = vi.spyOn(productApi, 'saveFixedCredentials').mockRejectedValueOnce(new Error('保存失败'))
    const wrapper = render({ setup() { const value = useProductControls(); provideProductContext(value); return () => h(ModelSettings) } })
    await flushPromises()
    expect(wrapper.findAll('select')).toHaveLength(0)
    expect(wrapper.findAll('input')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('模型池')
    await wrapper.get('input').setValue('test-deepseek-key')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.get('input').element).toHaveProperty('value', 'test-deepseek-key')
    save.mockResolvedValue({ providers: { deepseek: { configured: true }, minimax: { configured: true } }, vault_available: true })
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(save).toHaveBeenLastCalledWith({ deepseek_api_key: 'test-deepseek-key', minimax_api_key: '' })
    expect(wrapper.get('input').element).toHaveProperty('value', '')
  })
  it('唤醒任务按分类切换，归档与取消记录均可删除且不混入待执行列表', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(WakeTasksSettings) } })
    const tasks = [{ task_id: 'pending-1', status: 'pending', text: '待执行测试', has_reference: false }, { task_id: 'archive-1', status: 'archived', text: '归档测试', has_reference: false }, { task_id: 'cancel-1', status: 'cancelled', text: '取消测试', has_reference: false }]
    value.wakeTasks.value = tasks as never
    await flushPromises()
    expect(wrapper.findAll('.wake-task-list article')).toHaveLength(1)
    await wrapper.get('.wake-task-tabs button:last-child').trigger('click')
    expect(wrapper.findAll('.wake-task-list article')).toHaveLength(2)
    const remove = vi.spyOn(productApi, 'deleteWakeTask').mockResolvedValue({ deleted: true })
    vi.spyOn(productApi, 'wakeTasks').mockResolvedValue({ tasks: tasks.filter(task => task.task_id !== 'archive-1') } as never)
    await wrapper.get('.wake-task-list .danger-action').trigger('click'); await flushPromises()
    expect(remove).toHaveBeenCalledWith('archive-1')
    expect(wrapper.findAll('.wake-task-list article')).toHaveLength(1)
    expect(wrapper.get('.wake-task-list').text()).toContain('取消测试')
  })
  it.each([false, true])('归档删除没有成功回执或请求失败时，保留原记录并显示错误（请求失败：%s）', async failed => {
    const value = await adapter()
    const task = { task_id: 'failed-delete', status: 'archived', text: '保留归档', has_reference: false } as const
    value.wakeTasks.value = [task] as never
    const remove = vi.spyOn(productApi, 'deleteWakeTask')
    if (failed) remove.mockRejectedValue(new Error('插件存储失败'))
    else remove.mockResolvedValue({ deleted: false } as never)
    await value.removeWakeTask(task as never)
    expect(value.wakeTasks.value).toHaveLength(1)
    expect(value.notice.value).not.toContain('已删除')
    expect(value.lastError.value).toContain(failed ? '插件存储失败' : '归档删除未成功')
  })
  it('待执行任务调用取消接口并保留归档记录，不能直接走归档删除', async () => {
    const value = await adapter()
    const task = { task_id: 'cancel-pending', status: 'pending', text: '取消测试', has_reference: false } as const
    value.wakeTasks.value = [task] as never
    const cancel = vi.spyOn(productApi, 'cancelWakeTask').mockResolvedValue({ ...task, status: 'cancelled' } as never)
    const remove = vi.spyOn(productApi, 'deleteWakeTask')
    const list = vi.spyOn(productApi, 'wakeTasks').mockResolvedValue({ tasks: [{ ...task, status: 'cancelled' }] } as never)
    application.snapshot.value = { components: [{ id: 'dsh', name: '代理服务', state: 'ready' }] } as unknown as ApplicationSnapshot
    application.businessReady.value = true
    await value.removeWakeTask(task as never)
    expect(cancel).toHaveBeenCalledWith('cancel-pending')
    expect(remove).not.toHaveBeenCalled()
    expect(list).toHaveBeenCalledOnce()
    expect(value.wakeTasks.value[0]?.status).toBe('cancelled')
    expect(value.notice.value).toContain('保存在归档中')
  })
  it('本地组件不把整卡已用显存当应用占用，并区分设备草稿、待生效配置和实际 CPU（中央处理器）', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { hardware: { system_memory: { total_bytes: 16 * 1024 ** 3, commit_limit_bytes: null }, gpu: { available: true, total_bytes: 8 * 1024 ** 3, free_bytes: 2 * 1024 ** 3 } }, models: { paraformer: { service: 'READY', device: 'cpu', requested_device: 'cpu', gpu_available: true, runtime: 'PyTorch（推理框架）', resource_status: { pid: 12, rss_bytes: 1024 ** 3, private_commit_bytes: 2 * 1024 ** 3 } } } } as never
    application.snapshot.value = { core_available: true, total_memory_rss_bytes: null, total_memory_private_commit_bytes: null, components: [{ id: 'asr', state: 'ready', pid: 12 }] } as unknown as ApplicationSnapshot
    value.settings.compute.devices.paraformer = 'cuda'
    value.computeSavedDevices.value.paraformer = 'cpu'
    await flushPromises()
    expect(wrapper.find('[data-resource="vram"]').exists()).toBe(false)
    expect(wrapper.find('[data-resource="commit"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('未提供')
    const row = wrapper.get('[data-component="paraformer"]')
    expect(row.find('[data-metric="vram"]').exists()).toBe(false)
    expect(row.get('[data-metric="memory"]').text()).toContain('1 GB / 2 GB')
    await row.get('button.local-component-action').trigger('click')
    expect(row.text()).toContain('草稿尚未保存')
    expect(row.text()).not.toContain('已保存配置下次启动生效')
    value.computeSavedDevices.value.paraformer = 'cuda'
    await flushPromises()
    expect(row.text()).not.toContain('草稿尚未保存')
    expect(row.text()).toContain('已保存配置下次启动生效')
    expect(row.text()).toContain('实际运行：CPU（中央处理器）')
  })
  it('运行中的组件页把随包更新显示为必需更新操作，并复用既有下载计划', async () => {
    const inventoryRead = vi.fn(async () => [{ resource_id: 'paraformer-streaming', label: 'Paraformer（流式识别）',
      installation_state: 'installed' as const, files_installed: true, runtime_ready: true, can_start: true,
      update_available: true, installed_version: '1.0.0', version: '1.1.0' }])
    const resourcePlan = vi.fn(async (id: string) => ({ resource_id: id, title: 'Paraformer（流式识别）', model_bytes: 100 }))
    const download = vi.fn(async (id: string) => ({ job_id: `download-${id}` }))
    vi.spyOn(desktopBridge, 'desktopPort').mockReturnValue({
      available: true,
      resourceInventory: inventoryRead,
      workspaceInfo: async () => ({ root: 'D:/测试', data_dir: 'D:/测试/data', version: '1.0', can_manage: true }),
      environmentStatus: async () => ({ jobs: [] }),
      resourceInstallPlan: resourcePlan,
      downloadResource: download,
      listen: async () => () => undefined,
    } as unknown as ReturnType<typeof desktopBridge.desktopPort>)
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() {
      value = useProductControls()
      provideProductContext(value)
      const resourceController = useWorkspaceResources()
      return () => h(ComputeSettings, { resourceController })
    } })
    value.computeStatus.value = { models: { paraformer: { service: 'READY', device: 'cpu', requested_device: 'cpu', gpu_available: false } } } as never
    application.snapshot.value = { startup_complete: true, components: [{ id: 'asr', state: 'ready', pid: 12 }] } as unknown as ApplicationSnapshot
    await flushPromises()
    const card = wrapper.get('[data-component="paraformer"]')
    expect(card.get('.local-state').text()).toBe('需更新')
    expect(card.get('button[aria-label="Paraformer（流式语音识别）：更新"]').text()).toBe('更新')
    expect(card.get('button[aria-label="Paraformer（流式语音识别）：导入"]').text()).toBe('导入')
    expect(card.get('button[aria-label="Paraformer（流式语音识别）：跳转官方来源"]').text()).toBe('跳转')
    expect(card.find('.local-component-action').exists()).toBe(false)
    await card.get('button[aria-label="Paraformer（流式语音识别）：更新"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('#install-plan-title').text()).toContain('准备更新 Paraformer')
    await wrapper.get('.install-plan-actions button').trigger('click')
    await flushPromises()
    expect(resourcePlan).toHaveBeenCalledWith('paraformer-streaming')
    expect(download).toHaveBeenCalledWith('paraformer-streaming')
  })
  it('显存摘要使用去重进程计量，未知指标不显示，缺失后端不能选择显卡，刷新重新检测环境', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { hardware: { system_memory: {}, gpu: { total_bytes: 8 * 1024 ** 3 } }, models: {
      paraformer: { service: 'READY', device: 'cuda', requested_device: 'cuda', gpu_available: true, runtime: 'PyTorch（推理框架）', resource_status: { gpu_dedicated_bytes: 512 * 1024 ** 2 } },
      wespeaker: { service: 'READY', device: 'cpu', requested_device: 'cpu', gpu_available: false, gpu_unavailable_reason: '显卡运行库缺失' },
    } } as never
    application.snapshot.value = { total_gpu_dedicated_bytes: 768 * 1024 ** 2, components: [
      { id: 'setup', name: '环境与配置检查', state: 'ready', memory_rss_bytes: null, memory_private_commit_bytes: null },
    ] } as unknown as ApplicationSnapshot
    await flushPromises()
    expect(wrapper.get('[data-resource="vram"]').text()).toBe('显存768 MB/ 8 GB')
    expect(wrapper.get('[data-component="paraformer"] [data-metric="vram"]').text()).toContain('512 MB')
    expect(wrapper.find('[data-component="setup"]').exists()).toBe(false)
    const system = wrapper.get('[data-component="voice-system"]')
    expect(system.find('[data-metric="memory"]').exists()).toBe(false)
    await system.get('.local-component-action').trigger('click')
    expect(system.text()).toContain('环境与配置检查已通过')
    const row = wrapper.get('[data-component="wespeaker"]')
    await row.get('button.local-component-action').trigger('click')
    expect(row.findAll('option').map(option => option.attributes('value'))).toEqual(['auto', 'cpu'])
    expect(row.text()).toContain('显卡运行库缺失')
    value.settings.compute.devices.wespeaker = 'cuda'
    await flushPromises()
    expect(row.get('option[value="cuda"]').attributes('disabled')).toBeDefined()
    expect(row.get('.save-button').attributes('disabled')).toBeDefined()
    const probe = vi.spyOn(productApi, 'computeStatus').mockResolvedValue({} as never)
    application.businessReady.value = true
    await wrapper.get('.local-refresh').trigger('click'); await flushPromises()
    expect(probe).toHaveBeenCalledWith(true)
  })
  it('系统组件合并，同次进程明细覆盖旧模型采样，共享模型不重复展示内存', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    const mb = 1024 ** 2
    value.computeStatus.value = { hardware: { system_memory: {}, gpu: {} }, models: Object.fromEntries(
      ['paraformer', 'sensevoice', 'voice_vad', 'kws', 'smart_turn', 'wespeaker'].map(id => [id, {
        service: 'READY', device: 'cpu', requested_device: 'cpu', gpu_available: false,
        resource_status: { rss_bytes: 999 * mb, private_commit_bytes: 9999 * mb },
      }])) } as never
    const processes = [ ['asr', 100], ['analysis', 200], ['voice', 300], ['speaker', 80], ['dsh', 40], ['desktop', 60], ['host', 50], ['voice', 30] ] as const
    application.snapshot.value = {
      total_memory_rss_bytes: 860 * mb, total_memory_private_commit_bytes: 8600 * mb,
      resource_processes: processes.map(([component_id, size], index) => ({ component_id, pid: index + 1, created_at: 10,
        memory_rss_bytes: size * mb, memory_private_commit_bytes: size * 10 * mb, gpu_dedicated_bytes: 0 })),
      components: ['asr', 'analysis', 'voice', 'speaker', 'dsh', 'desktop', 'host', 'web', 'setup'].map(id => ({ id, name: id, state: 'ready', phase: '已就绪' })),
    } as unknown as ApplicationSnapshot
    await flushPromises()
    expect(wrapper.find('.voice-environment').exists()).toBe(false)
    expect(wrapper.findAll('.local-component-card[data-component]')).toHaveLength(8)
    expect(wrapper.findAll('[data-metric="memory"]')).toHaveLength(5)
    expect(wrapper.get('[data-component="paraformer"] [data-metric="memory"]').text()).toContain('100 MB / 1000 MB')
    expect(wrapper.get('[data-component="sensevoice"] [data-metric="memory"]').text()).toContain('200 MB')
    expect(wrapper.get('[data-component="voice-system"] [data-metric="memory"]').text()).toContain('440 MB')
    for (const id of ['voice_vad', 'kws', 'smart_turn']) expect(wrapper.find(`[data-component="${id}"] [data-metric="memory"]`).exists()).toBe(false)
    for (const id of ['voice', 'host', 'desktop', 'web', 'setup']) expect(wrapper.find(`[data-component="${id}"]`).exists()).toBe(false)
    application.snapshot.value!.resource_processes![0]!.memory_private_commit_bytes = null
    await flushPromises()
    expect(wrapper.get('[data-component="paraformer"] [data-metric="memory"]').text()).toBe('常驻内存100 MB')
  })
  it.each(['failed', 'blocked', 'stopped'] as const)('所属进程为 %s（不可用）时，旧模型就绪采样不能继续显示运行中', async state => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: { wespeaker: { service: 'READY', device: 'cpu', requested_device: 'cpu', runtime: 'ONNX Runtime（推理运行时）', gpu_available: false } } } as never
    application.snapshot.value = { core_available: true, components: [{ id: 'speaker', state, retry_allowed: state !== 'stopped', error: '' }] } as unknown as ApplicationSnapshot
    await flushPromises()
    const row = wrapper.get('[data-component="wespeaker"]')
    expect(row.get('.local-component-identity > .local-state').text()).toBe('不可用')
    expect(row.classes()).toContain('failed')
    expect(row.get('.local-error').text()).not.toBe('')
    expect(row.text()).not.toContain('运行中')
    if (state !== 'stopped') {
      await row.get('.retry-action').trigger('click')
      expect(application.retry).toHaveBeenCalledWith('speaker')
    }
  })
  it('模型就绪采样与组件故障冲突时显示宿主原因，恢复期间和完成后同步更新文案与按钮', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: { wespeaker: { service: 'READY', device: 'cpu', requested_device: 'cpu', gpu_available: false } } } as never
    application.snapshot.value = { core_available: true, components: [{ id: 'speaker', state: 'failed', retry_allowed: true, error: '声纹连接已断开' }] } as unknown as ApplicationSnapshot
    await flushPromises()
    const row = wrapper.get('[data-component="wespeaker"]')
    expect(row.get('.local-error').text()).toBe('声纹连接已断开')
    application.snapshot.value!.components[0]!.state = 'starting'
    await flushPromises()
    expect(row.get('.local-state').text()).toBe('启动中')
    expect(row.get('.local-component-action').attributes('disabled')).toBeDefined()
    expect(row.find('.retry-action').exists()).toBe(false)
    application.snapshot.value!.components[0]!.state = 'ready'
    await flushPromises()
    expect(row.get('.local-state').text()).toBe('运行中')
    expect(row.classes()).toContain('ready')
    expect(row.find('.retry-action').exists()).toBe(false)
    expect(row.find('.local-error').exists()).toBe(false)
  })
  it('Voice（语音服务）进程仍运行但核心能力未就绪时，VAD（语音活动检测）/KWS（关键词唤醒）按自身状态呈现', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    const modelStatuses = {
      voice_vad: { service: 'READY', device: 'cpu', runtime: 'PyTorch（推理框架）' },
      kws: { service: 'READY', device: 'cpu', runtime: 'Sherpa-ONNX（唤醒引擎）' },
    }
    value.computeStatus.value = { models: modelStatuses } as never
    application.snapshot.value = { startup_complete: true, voice_api_ready: true, components: [
      { id: 'voice', name: '语音服务', state: 'failed', pid: 4321, retry_allowed: true,
        error: '核心能力未就绪：LLM（大语言模型）、TTS（语音合成）', external: false },
    ] } as unknown as ApplicationSnapshot
    await flushPromises()

    for (const id of ['voice_vad', 'kws']) {
      const row = wrapper.get(`[data-component="${id}"]`)
      expect(row.classes()).toContain('ready')
      expect(row.get('.local-state').text()).toBe('运行中')
      expect(row.find('.retry-action').exists()).toBe(false)
    }

    value.computeStatus.value = { models: {
      ...modelStatuses,
      kws: { service: 'DISABLED', device: null },
    } } as never
    await flushPromises()
    const kws = wrapper.get('[data-component="kws"]')
    expect(kws.classes()).toContain('disabled')
    expect(kws.get('.local-state').text()).toBe('已停用')
    expect(kws.find('.retry-action').exists()).toBe(false)
  })
  it.each(['RUNNING', 'STREAMING', 'CONNECTED'])('将 %s（正常工作状态）的 KWS（关键词唤醒）呈现为可用', async (service) => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: { kws: { service, device: 'cpu' } } } as never
    application.snapshot.value = { startup_complete: false, components: [
      { id: 'voice', name: '语音服务', state: 'ready', pid: 4321, retry_allowed: false },
    ] } as unknown as ApplicationSnapshot
    await flushPromises()
    const kws = wrapper.get('[data-component="kws"]')
    expect(kws.classes()).toContain('ready')
    expect(kws.get('.local-state').text()).toBe('运行中')
    expect(kws.find('.local-error').exists()).toBe(false)
    expect(kws.find('.retry-action').exists()).toBe(false)
    application.snapshot.value!.startup_complete = true
    await flushPromises()
    expect(kws.classes()).toContain('ready')
    expect(kws.get('.local-state').text()).toBe('运行中')
  })
  it('Voice（语音服务）模型自身错误仍可重试，宿主确认所属进程退出时拒绝沿用 READY（就绪）快照', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: {
      voice_vad: { service: 'READY', device: 'cpu' },
      kws: { service: 'ERROR', load_error: 'KWS（关键词唤醒）模型加载失败' },
    } } as never
    application.snapshot.value = { startup_complete: true, voice_api_ready: true, components: [
      { id: 'voice', name: '语音服务', state: 'failed', pid: 4321, retry_allowed: true,
        error: '核心能力未就绪：LLM（大语言模型）、TTS（语音合成）', external: false },
    ] } as unknown as ApplicationSnapshot
    await flushPromises()

    const kws = wrapper.get('[data-component="kws"]')
    expect(kws.classes()).toContain('failed')
    expect(kws.get('.local-state').text()).toBe('运行异常')
    expect(kws.get('.local-error').text()).toBe('KWS（关键词唤醒）模型加载失败')
    await kws.get('.retry-action').trigger('click')
    expect(application.retry).toHaveBeenCalledWith('voice')

    application.snapshot.value!.components[0]!.state = 'stopped'
    application.snapshot.value!.components[0]!.pid = null
    await flushPromises()
    for (const id of ['voice_vad', 'kws']) {
      const row = wrapper.get(`[data-component="${id}"]`)
      expect(row.classes()).toContain('failed')
      expect(row.get('.local-state').text()).toBe('不可用')
      expect(row.get('.local-error').text()).not.toBe('')
      expect(row.text()).not.toContain('运行中')
    }
  })
  it('外部 Voice（语音服务）组件即使没有 pid（进程编号）也不因缺少 pid 被误判为退出', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: { kws: { service: 'READY', device: 'cpu' } } } as never
    application.snapshot.value = { startup_complete: true, components: [
      { id: 'voice', name: '语音服务', state: 'external', pid: null, external: true },
    ] } as unknown as ApplicationSnapshot
    await flushPromises()
    const kws = wrapper.get('[data-component="kws"]')
    expect(kws.classes()).toContain('ready')
    expect(kws.get('.local-state').text()).toBe('运行中')
  })
  it('模型运行故障单独显示原因，加载回退诊断不覆盖运行异常', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: { wespeaker: { service: 'ERROR', device: 'cpu', requested_device: 'cpu', gpu_available: false, runtime_error: '声纹推理请求超时（2500 毫秒）', load_error: 'CUDA（图形处理器）加载失败，已回退' } } } as never
    application.snapshot.value = { core_available: true, components: [{ id: 'speaker', state: 'ready' }] } as unknown as ApplicationSnapshot
    await flushPromises()
    const row = wrapper.get('[data-component="wespeaker"]')
    expect(row.get('.local-state').text()).toBe('运行异常')
    expect(row.get('.local-error').text()).toBe('声纹推理请求超时（2500 毫秒）')
    expect(row.text()).not.toContain('运行中')
    application.snapshot.value!.components[0]!.state = 'failed'
    await flushPromises()
    expect(row.get('.local-state').text()).toBe('不可用')
    expect(row.get('.local-error').text()).toBe('声纹推理请求超时（2500 毫秒）')
  })
  it.each(['waiting', 'starting'])('启动阶段的 %s（加载过程）覆盖临时模型错误，所有组件采用正常加载样式', async state => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: Object.fromEntries(
      ['paraformer', 'sensevoice', 'voice_vad', 'kws', 'smart_turn', 'wespeaker'].map((id, index) => [id, {
        service: ['ERROR', 'UNAVAILABLE', 'DISABLED', 'UNKNOWN'][index % 4], load_error: '组件尚未就绪', runtime_error: '旧采样错误',
      }])) } as never
    application.snapshot.value = { state: 'starting', startup_complete: false, components: [
      ...['asr', 'analysis', 'voice', 'speaker', 'dsh'].map(id => ({ id, name: id, state, phase: '正在加载组件', error: '旧错误' })),
      { id: 'host', name: '应用宿主', state: 'ready' },
    ] } as unknown as ApplicationSnapshot
    await flushPromises()
    expect(wrapper.find('.voice-environment').exists()).toBe(false)
    expect(wrapper.findAll('.local-component-card[data-component]')).toHaveLength(8)
    expect(wrapper.get('.local-overview .local-state').text()).toBe('组件启动中')
    // 模型启动状态与独立原生环境管理卡片的接口错误分别核对。
    expect(wrapper.find('.local-component-list .local-error').exists()).toBe(false)
    expect(wrapper.find('.retry-action').exists()).toBe(false)
    for (const row of wrapper.findAll('.local-component-card[data-component]')) {
      expect(row.classes()).toContain('loading')
      expect(row.get('.local-state').text()).toBe('启动中')
      expect(row.get('.local-component-action').text()).toBe('启动中')
      expect(row.get('.local-component-action').attributes('disabled')).toBeDefined()
      expect(row.get('.local-component-phase').text()).toBe('正在加载组件')
    }
  })
  it.each(['wespeaker', 'dsh', 'voice-system'])('%s（本地组件）点击重试立即进入加载状态，后台接管后持续加载并保留真正失败结果', async id => {
    const owner = id === 'wespeaker' ? 'speaker' : id === 'voice-system' ? 'voice' : 'dsh'
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: { wespeaker: { service: 'READY', device: 'cpu' } } } as never
    application.snapshot.value = { startup_complete: true, components: [
      { id: owner, name: owner, state: 'failed', phase: '旧启动失败', retry_allowed: true, error: '旧错误' },
    ] } as unknown as ApplicationSnapshot
    await flushPromises()
    const row = wrapper.get(`[data-component="${id}"]`)
    const requested = deferred<void>()
    application.retry.mockReturnValueOnce(requested.promise)
    await row.get('.local-component-action').trigger('click')
    expect(application.retry).toHaveBeenCalledWith(owner)
    expect(row.classes()).toContain('loading')
    expect(row.classes()).not.toContain('failed')
    expect(row.get('.local-state').text()).toBe('启动中')
    expect(row.get('.local-component-phase').text()).toBe('正在重新启动组件')
    expect(row.find('.local-error').exists()).toBe(false)
    expect(row.find('.retry-action').exists()).toBe(false)
    expect(row.get('.local-component-action').attributes('disabled')).toBeDefined()
    application.snapshot.value!.components[0]!.retrying = true
    application.snapshot.value!.components[0]!.phase = '等待恢复操作'
    requested.resolve(); await flushPromises()
    expect(row.classes()).toContain('loading')
    expect(row.get('.local-state').text()).toBe('启动中')
    expect(row.find('.local-error').exists()).toBe(false)
    application.snapshot.value!.components[0]!.retrying = false
    application.snapshot.value!.components[0]!.error = '本次启动失败'
    await flushPromises()
    expect(row.classes()).toContain('failed')
    expect(row.get('.local-error').text()).toContain('本次启动失败')
    expect(row.get('.retry-action').text()).toBe('重试')
  })
  it('所属进程已就绪但完整启动尚未结束时，旧的未就绪采样不能显示错误；明确失败和停用仍正常显示', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { models: {
      paraformer: { service: 'ERROR', load_error: '旧未就绪错误' },
      sensevoice: { service: 'UNAVAILABLE', load_error: '尚未连接' },
      wespeaker: { service: 'DISABLED' },
    } } as never
    application.snapshot.value = { startup_complete: false, components: [
      { id: 'asr', state: 'ready', phase: '健康检查通过' },
      { id: 'analysis', state: 'failed', error: '分析模型缺失' },
      { id: 'speaker', state: 'disabled' },
    ] } as unknown as ApplicationSnapshot
    await flushPromises()
    expect(wrapper.get('[data-component="paraformer"] .local-state').text()).toBe('启动中')
    expect(wrapper.find('[data-component="paraformer"] .local-error').exists()).toBe(false)
    expect(wrapper.get('[data-component="sensevoice"] .local-error').text()).toBe('分析模型缺失')
    expect(wrapper.get('[data-component="wespeaker"] .local-state').text()).toBe('已停用')
    application.snapshot.value!.startup_complete = true
    await flushPromises()
    expect(wrapper.get('[data-component="paraformer"] .local-error').text()).toBe('旧未就绪错误')
  })
  it('组件完成启动立即重新采样，启动前的迟到错误响应不能覆盖新的就绪结果', async () => {
    const { value } = liveControls()
    allowBusinessReady(value)
    application.snapshot.value = { startup_complete: false, components: [{ id: 'asr', state: 'starting', pid: 12 }] } as unknown as ApplicationSnapshot
    await flushPromises()
    const pending = deferred<Awaited<ReturnType<typeof productApi.computeStatus>>>()
    const probe = vi.mocked(productApi.computeStatus)
    probe.mockReturnValueOnce(pending.promise).mockResolvedValue({ models: { paraformer: { service: 'READY', device: 'cpu' } } } as never)
    const stale = value.refreshComputeStatus()
    application.snapshot.value = { startup_complete: true, components: [{ id: 'asr', state: 'ready', pid: 12 }] } as unknown as ApplicationSnapshot
    await flushPromises()
    expect(value.computeStatus.value?.models.paraformer?.service).toBe('READY')
    pending.resolve({ models: { paraformer: { service: 'ERROR', load_error: '启动时尚未就绪' } } } as never)
    await stale; await flushPromises()
    expect(value.computeStatus.value?.models.paraformer?.service).toBe('READY')
    const count = probe.mock.calls.length
    application.snapshot.value!.components[0]!.phase = '采样说明更新'
    await flushPromises()
    expect(probe).toHaveBeenCalledTimes(count)
  })
  it('实际播放在输入结束判定期间驱动语音主体，完成、打断和错误仍及时更新', async () => {
    const { value } = liveControls()
    allowBusinessReady(value)
    application.snapshot.value = { core_available: true, components: [] } as unknown as ApplicationSnapshot
    value.voice.listening.value = true
    value.voice.state.value = 'FINALIZING'
    value.voice.playbackQueue.value = [{ speech_id: 'actual-output', conversation_id: 'actual-session', state: 'playing', text: '真实播报' }] as never
    value.voice.audioLevels.value = [0.1, 0.2, 0.3, 0.2, 0.1]
    expect(value.workbenchModel.value.state).toBe('speaking')
    expect(value.workbenchModel.value.audioLevels).toEqual([0.1, 0.2, 0.3, 0.2, 0.1])
    value.voice.state.value = 'INTERRUPTING'
    expect(value.workbenchModel.value.state).toBe('listening')
    value.voice.state.value = 'ERROR'
    expect(value.workbenchModel.value.state).toBe('error')
    value.voice.state.value = 'FINALIZING'
    value.voice.playbackQueue.value = []
    expect(value.workbenchModel.value.state).toBe('listening')
    expect(value.workbenchModel.value.audioLevels).toEqual([0, 0, 0, 0, 0])
  })
  it('用户识别条目不显示播报交付字段，助手消息保留真实已听内容', () => {
    const { value } = liveControls()
    value.voice.connection.value = 'connected'
    value.voice.conversationEntries.value = [
      { seq: 1, kind: 'user_turn', conversation_id: 'actual-session', turn_id: 'turn-1', text: '用户原话', at: '2026-10-06T12:00:00Z', status: 'completed', delivery: { status: 'pending' } },
      { seq: 2, kind: 'assistant_speech', conversation_id: 'actual-session', turn_id: 'turn-1', speech_id: 'speech-1', text: '助手回复', at: '2026-10-06T12:00:01Z', status: 'completed', delivery: { status: 'interrupted', heard_text: '助手' } },
    ] as never
    expect(value.workbenchModel.value.conversation[0]?.delivered).toBe('')
    expect(value.workbenchModel.value.conversation[1]?.delivered).toContain('助手')
  })
  it('三种手动任务只提交说明和排期，失败保留创建草稿', async () => {
    const { value } = liveAdapter()
    allowBusinessReady(value)
    const create = vi.spyOn(productApi, 'createWakeTask').mockResolvedValue({} as never)
    for (const kind of ['at', 'daily', 'weekly'] as const) {
      value.addWakeTask()
      expect(Date.parse(value.wakeTaskCreateDraft.when)).toBeGreaterThan(Date.now())
      value.wakeTaskCreateDraft.ruleKind = kind
      value.wakeTaskCreateDraft.time = '09:15'
      value.wakeTaskCreateDraft.text = ' 手动检查 '
      value.wakeTaskCreateDraft.weekdays = '1,3'
      await value.createWakeTask()
      const submitted = create.mock.calls.at(-1)![0]
      expect(Object.keys(submitted).sort()).toEqual([kind, 'text'].sort())
      expect(submitted.text).toBe('手动检查')
      if (kind === 'daily') expect(submitted.daily?.time).toBe('09:15:00')
      if (kind === 'weekly') expect(submitted.weekly?.time).toBe('09:15:00')
      if (kind === 'weekly') expect(submitted.weekly?.weekdays).toEqual([1, 3])
      expect(value.addingWakeTask.value).toBe(false)
    }
    create.mockRejectedValueOnce(new Error('创建失败'))
    value.addWakeTask(); value.wakeTaskCreateDraft.text = '保留草稿'
    await value.createWakeTask()
    expect(value.addingWakeTask.value).toBe(true)
    expect(value.wakeTaskCreateDraft.text).toBe('保留草稿')
    expect(value.lastError.value).toBe('创建失败')
  })
  it('三段服务端录音完成后立即允许保存，失败保留片段并可重试，成功显示已注册模板', async () => {
    vi.spyOn(productApi, 'startSpeakerEnrollment').mockResolvedValue({
      enrollment_id: 'complete-session', required_segments: 3,
      prompts: [{ text: '第一句' }, { text: '第二句' }, { text: '第三句' }],
    })
    const capture = vi.spyOn(productApi, 'captureSpeakerEnrollmentSegment')
    const commit = vi.spyOn(productApi, 'commitSpeakerEnrollment')
      .mockRejectedValueOnce(new Error('保存暂时失败'))
      .mockResolvedValue({ enrollment: { min_similarity: 0.8 } })
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(SpeakerSettings) } })
    allowBusinessReady(value)
    await flushPromises()
    await button(wrapper, '开始录制').trigger('click')
    expect(wrapper.find('input[aria-label="自定义模板标签"]').exists()).toBe(false)
    const save = button(wrapper, '保存声纹')
    for (let index = 0; index < 3; index += 1) {
      const response = deferred<Record<string, unknown>>()
      capture.mockReturnValueOnce(response.promise)
      const row = wrapper.get(`[data-segment="${index}"]`)
      await row.get('button').trigger('click')
      await flushPromises()
      expect(save.attributes('disabled')).toBeDefined()
      response.resolve({ quality: { speech_ms: 6000 } })
      await flushPromises()
      expect(row.get('.speaker-segment-state').text()).toBe('已完成')
      expect(capture).toHaveBeenLastCalledWith('complete-session', expect.objectContaining({ index, label: `第 ${index + 1} 段` }), expect.any(AbortSignal))
    }
    expect(value.speakerEnrollmentComplete.value).toBe(true)
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click'); await flushPromises()
    expect(commit).toHaveBeenCalledWith('complete-session')
    expect(value.speakerEnrollment.value?.segments).toEqual([0, 1, 2])
    expect(value.lastError.value).toBe('保存暂时失败')
    expect(save.attributes('disabled')).toBeUndefined()
    const templates = [0, 1, 2].map(index => ({ template_id: `template-${index}`, label: `第 ${index + 1} 段`, include_in_centroid: true }))
    vi.mocked(productApi.speakerProfile).mockResolvedValue({ available: true, templates })
    vi.mocked(productApi.speakerStatus).mockResolvedValue({ registered: true, service: 'READY', worker_connected: true })
    await save.trigger('click'); await flushPromises()
    expect(commit).toHaveBeenCalledTimes(2)
    expect(value.speakerEnrollment.value).toBeNull()
    expect(wrapper.findAll('.speaker-template-row')).toHaveLength(3)
    expect(value.notice.value).toContain('声纹资料已更新')
  })
  it('服务器提前停止只在五秒后提交，收起保留完成进度并拒绝迟到响应', async () => {
    vi.spyOn(productApi, 'startSpeakerEnrollment').mockResolvedValue({ enrollment_id: 'stop-session' })
    const response = deferred<Record<string, unknown>>()
    vi.spyOn(productApi, 'captureSpeakerEnrollmentSegment').mockReturnValue(response.promise)
    const { value } = liveControls()
    allowBusinessReady(value)
    const request = value.beginSpeakerRecording(0, '第一段')
    await flushPromises()
    await value.stopSpeakerRecording()
    expect(productApi.stopSpeakerCapture).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(5000)
    await value.stopSpeakerRecording()
    expect(productApi.stopSpeakerCapture).toHaveBeenCalledWith('stop-session', expect.any(String), false)
    value.speakerEnrollment.value!.segments.push(1)
    value.cancelActiveSpeakerCapture()
    response.resolve({ quality: { speech_ms: 5000 } }); await request
    expect(value.speakerEnrollment.value!.segments).toEqual([1])
    expect(value.speakerRecordingSegment.value).toBeNull()
    expect(value.speakerEnrollmentComplete.value).toBe(false)
  })
  it('将固定间隔任务改为单次时清除间隔，每周任务保存所选星期与原有秒数', async () => {
    const { value } = liveAdapter()
    allowBusinessReady(value)
    const update = vi.spyOn(productApi, 'updateWakeTask').mockResolvedValue({} as never)
    value.editWakeTask({ task_id: 'task-existing', target: 'voice_llm', status: 'pending', when: '2026-10-12T18:00:00+08:00', text: '原有任务', repeat: '1d', rule_kind: 'every' } as never)
    value.wakeTaskDraft.ruleKind = 'at'
    await value.saveWakeTask()
    expect(update).toHaveBeenLastCalledWith('task-existing', expect.objectContaining({ repeat: '' }))
    value.editWakeTask({ task_id: 'task-weekly', target: 'voice_llm', status: 'pending', when: '2026-10-12T18:00:00+08:00', text: '每周检查', repeat: null, rule_kind: 'weekly', rule: { time: '18:00:30', weekdays: [1, 3], timeZone: 'Asia/Hong_Kong' } } as never)
    await value.saveWakeTask()
    expect(update).toHaveBeenLastCalledWith('task-weekly', expect.objectContaining({ change: { weekly: { time: '18:00:30', time_zone: 'Asia/Hong_Kong', weekdays: [1, 3] } } }))
  })

  it('桌面控制台取得登录地址后由原生 open_console（打开控制台）处理', async () => {
    const launch = deferred<{ url: string }>()
    const openConsole = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(desktopBridge, 'desktopPort').mockReturnValue({ available: true,
      focus: vi.fn(), openLogs: vi.fn(), openConsole, exit: vi.fn() })
    const openTab = vi.spyOn(window, 'open').mockReturnValue(null)
    vi.spyOn(productApi, 'launchDsh').mockReturnValue(launch.promise)
    const value = await adapter()
    value.dsh.value = { ...value.dsh.value, state: 'ready', ui_origin: 'http://127.0.0.1:3081' }
    const request = value.openDshConsole()
    expect(openConsole).not.toHaveBeenCalled()
    expect(openTab).not.toHaveBeenCalled()
    const url = 'http://127.0.0.1:3081/?token=launch%2Btoken%2Fvalue%3D'
    launch.resolve({ url }); await request
    expect(openConsole).toHaveBeenCalledOnce()
    expect(openConsole).toHaveBeenCalledWith(url)
    expect(value.lastError.value).toBe('')
  })

  it('原生打开控制台失败时显示错误，入口仍可重试', async () => {
    const openConsole = vi.fn().mockRejectedValueOnce(new Error('无法打开系统默认浏览器。'))
      .mockResolvedValueOnce(undefined)
    vi.spyOn(desktopBridge, 'desktopPort').mockReturnValue({ available: true,
      focus: vi.fn(), openLogs: vi.fn(), openConsole, exit: vi.fn() })
    const openTab = vi.spyOn(window, 'open').mockReturnValue(null)
    vi.spyOn(productApi, 'launchDsh').mockResolvedValue({ url: 'http://127.0.0.1:3081/?token=value' })
    const value = await adapter()
    const ready = { ...value.dsh.value, state: 'ready' as const, ui_origin: 'http://127.0.0.1:3081' }
    vi.spyOn(productApi, 'dshStatus').mockResolvedValue(ready)
    value.dsh.value = ready
    await value.openDshConsole()
    expect(value.lastError.value).toBe('无法打开系统默认浏览器。')
    await value.openDshConsole()
    expect(openConsole).toHaveBeenCalledTimes(2)
    expect(openTab).not.toHaveBeenCalled()
    expect(value.dsh.value.state).toBe('ready')
  })

  it('原生命令桥不可用时不请求控制台地址或打开浏览器窗口', async () => {
    vi.spyOn(desktopBridge, 'desktopPort').mockReturnValue({ available: false,
      focus: vi.fn(), openLogs: vi.fn(), openConsole: vi.fn(), exit: vi.fn() })
    const openTab = vi.spyOn(window, 'open').mockReturnValue(null)
    const launch = vi.spyOn(productApi, 'launchDsh')
    const value = await adapter()
    await value.openDshConsole()
    expect(value.lastError.value).toContain('只能由 Voice Terminal 桌面应用打开')
    expect(launch).not.toHaveBeenCalled()
    expect(openTab).not.toHaveBeenCalled()
  })

  it('宿主晚于页面就绪时更新控制台状态，迟到的启动快照不能覆盖它', async () => {
    application.managed.value = true; application.businessReady.value = true
    const response = deferred<Awaited<ReturnType<typeof productApi.snapshot>>>()
    vi.spyOn(productApi, 'snapshot').mockReturnValue(response.promise)
    vi.spyOn(productApi, 'speakerStatus').mockResolvedValue({})
    vi.spyOn(productApi, 'speakerProfile').mockResolvedValue({})
    vi.spyOn(productApi, 'ambientContext').mockResolvedValue({})
    vi.spyOn(productApi, 'computeStatus').mockResolvedValue({} as never)
    vi.spyOn(productApi, 'wakeTasks').mockResolvedValue({ tasks: [] } as never)
    const ready = { enabled:true, state:'ready' as const, version:'0.1.7', profile:'web' as const,
      ui_origin:'http://127.0.0.1:3081', message:'宿主已就绪' }
    const status = vi.spyOn(productApi, 'dshStatus').mockResolvedValue(ready)
    const { value } = liveAdapter()
    application.snapshot.value = { components:[{id:'dsh',state:'ready'}] } as ApplicationSnapshot
    await flushPromises()
    expect(status).toHaveBeenCalledOnce()
    expect(value.dsh.value).toEqual(ready)
    response.resolve({ settings:structuredClone(DEFAULT_SETTINGS), models:structuredClone(DEFAULT_REGISTRY),
      dsh:{...ready,state:'unavailable'} } as never)
    await flushPromises()
    expect(value.dsh.value).toEqual(ready)
    application.snapshot.value = { components:[{id:'dsh',state:'retrying'}] } as ApplicationSnapshot
    await flushPromises()
    expect(status).toHaveBeenCalledTimes(2)
  })

  it('迟到的首个快照保留已经编辑的表单与已完成的模型扫描', async () => {
    application.businessReady.value = true
    const response = deferred<Awaited<ReturnType<typeof productApi.snapshot>>>()
    vi.spyOn(productApi, 'snapshot').mockReturnValue(response.promise)
    vi.spyOn(productApi, 'speakerStatus').mockResolvedValue({})
    vi.spyOn(productApi, 'speakerProfile').mockResolvedValue({})
    vi.spyOn(productApi, 'ambientContext').mockResolvedValue({})
    vi.spyOn(productApi, 'computeStatus').mockResolvedValue({} as never)
    vi.spyOn(productApi, 'wakeTasks').mockResolvedValue({ tasks: [] } as never)
    vi.spyOn(productApi, 'scanModels').mockResolvedValue({ ...structuredClone(DEFAULT_REGISTRY),
      models: [{ id:'scanned-model', capability:'llm', name:'已扫描模型' }] } as never)
    const value = await adapter()
    const request = value.loadSnapshot()
    value.wakeForm.keywords = ['保留这个草稿']; value.silenceSeconds.value = 45
    value.quietHoursForm.start = '22:00'; value.speakerForm.confirm_threshold = 0.8
    value.personaDocuments.user = '新资料'; value.settings.compute.devices.paraformer = 'cpu'
    await value.scanModels(); value.modelAssignments.voice = 'scanned-model'
    response.resolve({ settings:structuredClone(DEFAULT_SETTINGS), models:structuredClone(DEFAULT_REGISTRY) } as never)
    await request
    expect(value.wakeForm.keywords).toEqual(['保留这个草稿']); expect(value.silenceSeconds.value).toBe(45)
    expect(value.quietHoursForm.start).toBe('22:00'); expect(value.speakerForm.confirm_threshold).toBe(0.8)
    expect(value.personaDocuments.user).toBe('新资料'); expect(value.settings.compute.devices.paraformer).toBe('cpu')
    expect(value.registry.value.models.map(row=>row.id)).toEqual(['scanned-model'])
    expect(value.modelAssignments.voice).toBe('scanned-model')
  })
  it('接口可用但核心语音未就绪时，工作台显示诊断并禁止启动语音', async () => {
    application.managed.value = true; application.businessReady.value = true
    application.snapshot.value = { core_available:false, components:[{id:'voice',error:'在线模型未配置'}] } as ApplicationSnapshot
    let value!: ReturnType<typeof useProductControls>
    await renderToString(createSSRApp({setup(){value=useProductControls();return()=>h('div')}}))
    value.voice.connection.value = 'connected'
    const start = vi.spyOn(value.voice, 'toggleListening')
    expect(value.workbenchModel.value.state).toBe('error')
    expect(value.workbenchModel.value.description).toBe('在线模型未配置')
    expect(value.workbenchModel.value.action).toBe('查看本地组件')
    value.runtimeAction()
    expect(value.activeSettingsTab.value).toBe('compute'); expect(start).not.toHaveBeenCalled()
  })
  it('合并后的导航都有页面，切换后保留根层持有的唤醒词草稿', async () => {
    const wrapper = render(ProductApp)
    await button(wrapper, '设置').trigger('click')
    await wrapper.find('.wake-keyword-row input').setValue('你好，新助手')
    for (const item of settingsSections) {
      await button(wrapper, item.label).trigger('click')
      expect(wrapper.find('.settings-panel').text().length).toBeGreaterThan(10)
    }
    await button(wrapper, '唤醒与静默').trigger('click')
    expect((wrapper.find('.wake-keyword-row input').element as HTMLInputElement).value).toBe('你好，新助手')
  })
  it('合并页的三种保存分别使用原有业务接口', async () => {
    const wake = vi.spyOn(productApi, 'saveWake').mockResolvedValue({ message: '已保存' } as never)
    const silence = vi.spyOn(productApi, 'saveSilence').mockResolvedValue({ message: '已保存' } as never)
    const quiet = vi.spyOn(productApi, 'saveQuietHours').mockResolvedValue({ message: '已保存' } as never)
    const wrapper = render(ProductApp)
    await button(wrapper, '设置').trigger('click')
    await wrapper.find('.wake-keyword-row input').setValue('你好助手')
    await button(wrapper, '保存唤醒词设置').trigger('click'); await flushPromises()
    await button(wrapper, '保存最终静默设置').trigger('click'); await flushPromises()
    await button(wrapper, '保存被动静默设置').trigger('click'); await flushPromises()
    expect(wake).toHaveBeenCalledWith(expect.objectContaining({ keywords: ['你好助手'] }))
    expect(silence).toHaveBeenCalledTimes(1); expect(quiet).toHaveBeenCalledTimes(1)
  })
  it('预览后改变草稿，确认仍提交已预览的同一组分配', async () => {
    const apply = vi.spyOn(productApi, 'applyAssignments').mockResolvedValue({ impact: [], applied: true } as never)
    const value = await adapter()
    value.modelAssignments.voice = 'previewed'; await value.requestModelApply()
    value.modelAssignments.voice = 'new-draft'; await value.confirmModelApply()
    expect(apply.mock.calls.map(call => [call[0].voice_llm_model_id, call[1]])).toEqual([['previewed', false], ['previewed', true]])
    expect(value.modelAssignments.voice).toBe('new-draft')
  })
  it('采集不可用允许回退，服务硬错误保留错误', async () => {
    const capture = vi.spyOn(productApi, 'captureSpeakerTest'); const value = await adapter()
    capture.mockRejectedValueOnce(new ProductApiError('设备未配置', 503, { code: 'SPEAKER_CAPTURE_UNAVAILABLE' }))
    expect(await value.captureSpeakerTest(6)).toBe(false)
    capture.mockRejectedValueOnce(new ProductApiError('录音质量不合格', 422, { code: 'SPEAKER_QUALITY_LOW' }))
    expect(await value.captureSpeakerTest(6)).toBe(null); expect(value.lastError.value).toBe('录音质量不合格')
  })
  it('注册请求迟于页面释放返回时，清理服务端会话而不复活界面', async () => {
    const response = deferred<Record<string, unknown>>()
    vi.spyOn(productApi, 'startSpeakerEnrollment').mockReturnValue(response.promise)
    const cancel = vi.spyOn(productApi, 'cancelSpeakerEnrollment').mockResolvedValue({})
    const { value, wrapper } = liveAdapter()
    allowBusinessReady(value)
    const request = value.startSpeakerEnrollment()
    wrapper.unmount()
    response.resolve({ enrollment_id: 'late-session', required_segments: 3 })
    await request
    expect(cancel).toHaveBeenCalledWith('late-session')
    expect(value.speakerEnrollment.value).toBeNull()
    expect(value.notice.value).toBe('')
  })
  it('取消注册立即中止服务端采集与计时，不向浏览器申请麦克风', async () => {
    vi.spyOn(productApi, 'startSpeakerEnrollment').mockResolvedValue({ enrollment_id: 'recording-session' })
    const cancel = vi.spyOn(productApi, 'cancelSpeakerEnrollment').mockResolvedValue({})
    let signal!: AbortSignal
    vi.spyOn(productApi, 'captureSpeakerEnrollmentSegment').mockImplementation((_id, _body, cancellation) => {
      signal = cancellation!
      return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('采集取消', 'AbortError')), { once: true }))
    })
    const { value } = liveControls()
    allowBusinessReady(value)
    const request = value.beginSpeakerRecording(0, '第一段')
    await flushPromises()
    expect(value.speakerServerCapture.value).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(value.speakerRecordingSeconds.value).toBe(1)
    await value.cancelSpeakerEnrollment({ notify: false }); await request
    expect(signal.aborted).toBe(true)
    expect(cancel).toHaveBeenCalledWith('recording-session')
    expect(value.speakerRecordingSegment.value).toBeNull()
    expect(value.speakerServerCapture.value).toBe(false)
    expect(value.lastError.value).toBe('')
    await vi.advanceTimersByTimeAsync(30000)
    expect(value.speakerRecordingSeconds.value).toBe(0)
    expect(value.lastError.value).toBe('')
  })
  it('组件开始重试时中止服务端录音并保留设置草稿', async () => {
    vi.spyOn(productApi, 'startSpeakerEnrollment').mockResolvedValue({ enrollment_id:'retry-session' })
    const cancel = vi.spyOn(productApi, 'cancelSpeakerEnrollment').mockResolvedValue({})
    let signal!: AbortSignal
    vi.spyOn(productApi, 'captureSpeakerEnrollmentSegment').mockImplementation((_id,_body,cancellation) => {
      signal=cancellation!
      return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('采集取消','AbortError')),{once:true}))
    })
    const { value }=liveControls()
    allowBusinessReady(value)
    value.wakeForm.keywords=['未保存的草稿']
    const recording=value.beginSpeakerRecording(0,'第一段'); await flushPromises()
    application.managed.value=true
    application.snapshot.value={components:[{id:'speaker',retrying:true,state:'starting'}]} as ApplicationSnapshot
    await recording; await flushPromises()
    expect(signal.aborted).toBe(true); expect(cancel).toHaveBeenCalledWith('retry-session')
    expect(value.speakerEnrollment.value).toBeNull(); expect(value.speakerRecordingSeconds.value).toBe(0)
    expect(value.wakeForm.keywords).toEqual(['未保存的草稿'])
    await vi.advanceTimersByTimeAsync(30000); expect(value.lastError.value).toBe('')
  })
  it('麦克风授权迟于组件重试返回时，立即释放设备而不开始录音', async () => {
    vi.spyOn(productApi, 'startSpeakerEnrollment').mockResolvedValue({ enrollment_id:'pending-microphone' })
    vi.spyOn(productApi, 'captureSpeakerEnrollmentSegment').mockRejectedValue(new ProductApiError('服务端采集不可用',503,{code:'SPEAKER_CAPTURE_UNAVAILABLE'}))
    vi.spyOn(productApi, 'cancelSpeakerEnrollment').mockResolvedValue({})
    const permission=deferred<MediaStream>()
    const previous=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
    Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:vi.fn(()=>permission.promise)}})
    const stop=vi.fn()
    try {
      const { value }=liveControls()
    allowBusinessReady(value)
      const recording=value.beginSpeakerRecording(0,'第一段'); await flushPromises()
      application.managed.value=true
      application.snapshot.value={components:[{id:'voice',retrying:true,state:'starting'}]} as ApplicationSnapshot
      permission.resolve({getTracks:()=>[{stop}]} as unknown as MediaStream)
      await recording
      expect(stop).toHaveBeenCalledTimes(1)
      expect(value.speakerRecordingSegment.value).toBeNull(); expect(value.speakerEnrollment.value).toBeNull()
    } finally {
      if(previous) Object.defineProperty(navigator,'mediaDevices',previous)
      else Reflect.deleteProperty(navigator,'mediaDevices')
    }
  })
  it('保存声纹、计算设备及指令时，迟到响应保留发送后的草稿', async () => {
    const { value } = liveAdapter()
    const speaker = deferred<Awaited<ReturnType<typeof productApi.saveSpeaker>>>()
    vi.spyOn(productApi, 'saveSpeaker').mockReturnValue(speaker.promise)
    value.speakerForm.confirm_threshold = 0.6
    const speakerSave = value.saveSpeaker(); await flushPromises()
    value.speakerForm.confirm_threshold = 0.8
    speaker.resolve({ settings: { speaker: { confirm_threshold: 0.6 } } } as never)
    await speakerSave
    expect(value.settings.speaker.confirm_threshold).toBe(0.6)
    expect(value.speakerForm.confirm_threshold).toBe(0.8)

    const compute = deferred<Awaited<ReturnType<typeof productApi.saveCompute>>>()
    vi.spyOn(productApi, 'saveCompute').mockReturnValue(compute.promise)
    value.settings.compute.devices.paraformer = 'cpu'
    const computeSave = value.saveComputeSettings(); await flushPromises()
    value.settings.compute.devices.paraformer = 'auto'
    compute.resolve({ settings: { compute: { devices: { paraformer: 'cpu' } } } } as never)
    await computeSave
    expect(value.settings.compute.devices.paraformer).toBe('auto')

    const instructions = deferred<Awaited<ReturnType<typeof productApi.saveCustomInstructions>>>()
    vi.spyOn(productApi, 'saveCustomInstructions').mockReturnValue(instructions.promise)
    value.customInstructions.value = [{ id: 'instruction-1', trigger: '查资料', target: 'shadow', instruction: '原指令', enabled: true }]
    const instructionSave = value.saveCustomInstructions(); await flushPromises()
    value.customInstructions.value[0].instruction = '新草稿'
    instructions.resolve({ settings: { custom_instructions: [{ id: 'instruction-1', trigger: '查资料', target: 'shadow', instruction: '原指令', enabled: true }] } } as never)
    await instructionSave
    expect(value.settings.custom_instructions[0].instruction).toBe('原指令')
    expect(value.customInstructions.value[0].instruction).toBe('新草稿')
  })
  it('模型确认在实际应用完成前保持可见，失败后可重试', async () => {
    const response = deferred<Awaited<ReturnType<typeof productApi.applyAssignments>>>()
    const apply = vi.spyOn(productApi, 'applyAssignments')
    apply.mockResolvedValueOnce({ requires_confirmation: true, impact: ['重新加载语音模型'] } as never)
    apply.mockReturnValueOnce(response.promise)
    const { value } = liveControls()
    value.modelAssignments.voice = 'new-model'
    await value.openApplyDialog()
    const request = value.confirmApply(); await flushPromises()
    expect(value.showApplyConfirm.value).toBe(true)
    expect(value.restartState.value).toBe('restarting')
    response.resolve({ applied: false } as never); await request
    expect(value.showApplyConfirm.value).toBe(true)
    apply.mockResolvedValueOnce({ applied: true } as never)
    await value.confirmApply()
    expect(value.showApplyConfirm.value).toBe(false)
  })
})
describe('共享呈现与展示隔离', () => {
  it('展示页七项导航可操作，修改样例不发请求', async () => {
    const request = vi.spyOn(globalThis, 'fetch'); const wrapper = render(UiDemoApp)
    await button(wrapper, '设置').trigger('click')
    for (const item of settingsSections) {
      await button(wrapper, item.label).trigger('click')
      expect(wrapper.find('.settings-content').text().length).toBeGreaterThan(10)
    }
    expect(request).not.toHaveBeenCalled()
  })
  it('工作台保留会话顺序与播放变色，隐藏完成标记和重复交付文本', async () => {
    const model: WorkbenchModel = { state:'offline',title:'离线',description:'等待连接',action:'重试',mode:'fast',modeDisabled:true,latency:'—',voiceModel:'语音模型',shadowModel:'协作模型',ttsModel:'合成模型',conversationHint:'等待',conversation:[{key:'server-2',role:'assistant',text:'第二条',status:'已打断',time:'现在',delivered:'已听到前半句'},{key:'server-3',role:'user',text:'第三条',status:'已完成',time:'现在'}],shadow:{title:'待机',status:'待机',context:'—',cache:'—',trace:[]} }
    const wrapper = render(Workbench, {props:{model}})
    expect(wrapper.findAll('.message').map(item=>item.text())).toEqual([expect.stringContaining('第二条'),expect.stringContaining('第三条')])
    expect(wrapper.text()).not.toContain('已听到前半句')
    expect(wrapper.find('.model-summary').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('合成模型'); expect(wrapper.text()).not.toContain('High（高强度）')
    expect(wrapper.findAll('.mode-options button').every(item=>item.attributes('disabled') !== undefined)).toBe(true)
    const consoleButton = wrapper.get('.shadow-panel-footer .console-button')
    expect(consoleButton.attributes('disabled')).toBeDefined()
    await consoleButton.trigger('click'); expect(wrapper.emitted('console')).toBeUndefined()
    await wrapper.setProps({ consoleEnabled: true })
    await consoleButton.trigger('click'); expect(wrapper.emitted('console')).toHaveLength(1)
    await wrapper.get('.conversation-column .workbench-settings-button').trigger('click')
    expect(wrapper.emitted('settings')).toHaveLength(1)
  })
  it('确认对话框在应用期间不允许 Escape（退出键）关闭', async () => {
    const wrapper = render(UiDialog, {props:{open:true,title:'确认',busy:true}})
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'})); expect(wrapper.emitted('close')).toBeUndefined()
    await wrapper.setProps({busy:false})
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'})); expect(wrapper.emitted('close')).toHaveLength(1)
  })
})

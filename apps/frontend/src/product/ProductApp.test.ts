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
import { provideProductContext } from './product-context'
import type { WorkbenchModel } from '../interface/workbench'
import type { ApplicationSnapshot } from './application-host'
import { DEFAULT_REGISTRY, DEFAULT_SETTINGS } from './normalization'
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
})
afterEach(() => {
  mounted.splice(0).forEach(wrapper => wrapper.unmount())
  Reflect.deleteProperty(window, '__VOICE_APPLICATION__')
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks()
})
describe('正式界面的真实控件交互', () => {
  it('本地组件不把整卡已用显存当应用占用，并区分设备草稿、待生效配置和实际 CPU（中央处理器）', async () => {
    let value!: ReturnType<typeof useProductControls>
    const wrapper = render({ setup() { value = useProductControls(); provideProductContext(value); return () => h(ComputeSettings) } })
    value.computeStatus.value = { hardware: { system_memory: { total_bytes: 16 * 1024 ** 3, commit_limit_bytes: null }, gpu: { available: true, total_bytes: 8 * 1024 ** 3, free_bytes: 2 * 1024 ** 3 } }, models: { paraformer: { service: 'READY', device: 'cpu', requested_device: 'cpu', gpu_available: true, runtime: 'PyTorch（推理框架）', resource_status: { pid: 12, rss_bytes: 1024 ** 3, private_commit_bytes: 2 * 1024 ** 3 } } } } as never
    application.snapshot.value = { core_available: true, total_memory_rss_bytes: null, total_memory_private_commit_bytes: null, components: [{ id: 'asr', state: 'ready', pid: 12 }] } as unknown as ApplicationSnapshot
    value.settings.compute.devices.paraformer = 'cuda'
    value.computeSavedDevices.value.paraformer = 'cpu'
    await flushPromises()
    expect(wrapper.get('[data-resource="vram"]').text()).toContain('未提供')
    expect(wrapper.get('[data-resource="vram"]').text()).not.toContain('6 GB')
    expect(wrapper.get('[data-resource="commit"]').text()).toBe('提交内存未提供/ 未提供')
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
    expect(row.get('.retry-action').attributes('disabled')).toBeDefined()
    application.snapshot.value!.components[0]!.state = 'ready'
    await flushPromises()
    expect(row.get('.local-state').text()).toBe('运行中')
    expect(row.classes()).toContain('ready')
    expect(row.find('.retry-action').exists()).toBe(false)
    expect(row.find('.local-error').exists()).toBe(false)
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
  it('工作台保留会话顺序与交付，移除模型摘要且离线模式停用', async () => {
    const model: WorkbenchModel = { state:'offline',title:'离线',description:'等待连接',action:'重试',mode:'fast',modeDisabled:true,latency:'—',voiceModel:'语音模型',shadowModel:'协作模型',ttsModel:'合成模型',conversationHint:'等待',conversation:[{key:'server-2',role:'assistant',text:'第二条',status:'已打断',time:'现在',delivered:'已听到前半句'},{key:'server-3',role:'user',text:'第三条',status:'已完成',time:'现在'}],shadow:{title:'待机',status:'待机',expanded:false,context:'—',cache:'—',trace:[]} }
    const wrapper = render(Workbench, {props:{model}})
    expect(wrapper.findAll('.message').map(item=>item.text())).toEqual([expect.stringContaining('第二条'),expect.stringContaining('第三条')])
    expect(wrapper.text()).toContain('已听到前半句')
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

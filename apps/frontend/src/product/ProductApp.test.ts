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
    expect(value.workbenchModel.value.action).toBe('查看系统运行')
    value.runtimeAction()
    expect(value.activeSettingsTab.value).toBe('system'); expect(start).not.toHaveBeenCalled()
  })
  it('七项导航都有页面，切换后保留根层持有的唤醒词草稿', async () => {
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
  it('工作台保留顺序、交付与合成模型，离线模式停用', async () => {
    const model: WorkbenchModel = { state:'offline',title:'离线',description:'等待连接',action:'重试',mode:'fast',modeDisabled:true,latency:'—',voiceModel:'语音模型',shadowModel:'协作模型',ttsModel:'合成模型',conversationHint:'等待',conversation:[{key:'server-2',role:'assistant',text:'第二条',status:'已打断',time:'现在',delivered:'已听到前半句'},{key:'server-3',role:'user',text:'第三条',status:'已完成',time:'现在'}],shadow:{title:'待机',status:'待机',expanded:false,context:'—',cache:'—',trace:[]} }
    const wrapper = render(Workbench, {props:{model}})
    expect(wrapper.findAll('.message').map(item=>item.text())).toEqual([expect.stringContaining('第二条'),expect.stringContaining('第三条')])
    expect(wrapper.text()).toContain('已听到前半句'); expect(wrapper.text()).toContain('合成模型'); expect(wrapper.text()).toContain('High（高强度）')
    expect(wrapper.findAll('.mode-options button').every(item=>item.attributes('disabled') !== undefined)).toBe(true)
    await wrapper.find('.model-summary').trigger('click'); expect(wrapper.emitted('models')).toHaveLength(1)
  })
  it('确认对话框在应用期间不允许 Escape（退出键）关闭', async () => {
    const wrapper = render(UiDialog, {props:{open:true,title:'确认',busy:true}})
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'})); expect(wrapper.emitted('close')).toBeUndefined()
    await wrapper.setProps({busy:false})
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'})); expect(wrapper.emitted('close')).toHaveLength(1)
  })
})

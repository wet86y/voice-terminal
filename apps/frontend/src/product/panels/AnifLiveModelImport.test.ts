// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { productApi, type AnifLiveImportScan } from '../api'
import * as bridge from '../desktop-port'
import type { ResourceJob } from '../desktop-port'
import { nativeDeviceApi } from '../native-device-api'
import type { RemoteManagementOperation, RemoteManagementResponse } from '../native-device-api'
import AnifLiveModelImport from './AnifLiveModelImport.vue'

const state = vi.hoisted(() => ({ host: undefined as unknown }))
vi.mock('../application-host', () => ({ useApplicationHost: () => state.host }))
beforeEach(() => { vi.useFakeTimers(); vi.restoreAllMocks() })
afterEach(() => { vi.useRealTimers() })

function remoteResponse(operation: RemoteManagementOperation, body: unknown, status = 200): RemoteManagementResponse {
  return { request_id: 'remote-request', target_host_id: 'server-a', session_kind: 'management',
    session_generation: 7, operation, status, body: body as Record<string, unknown>, fields: {} }
}

function managedJob(jobId: string, resourceId: string, state: string, phase: string): ResourceJob {
  return { job_id: jobId, resource_id: resourceId, state, phase, can_cancel: false }
}

async function scanImport(weights: AnifLiveImportScan['candidates']['weights']) {
  state.host = { recovery: ref({ suspended: false }) }
  vi.spyOn(bridge, 'desktopPort').mockReturnValue({ available: true,
    resourceInventory: async () => [{ resource_id: 'tts-aniflive-runtime', can_start: true }],
    chooseAnifLiveImportFolder: async () => 'D:/模型' } as never)
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockResolvedValue({ jobs: [] })
  vi.spyOn(productApi, 'scanAnifLiveImport').mockResolvedValue({ scan_id: 'scan', source: 'D:/模型', source_type: 'local',
    files: weights, candidates: { weights, audio: [{ file_id: 'apology', path: 'references/apology.wav' },
      { file_id: 'neutral', path: 'references/neutral.wav' }], transcripts: [
      { file_id: 'apology-text', path: 'references/apology.lab' },
      { file_id: 'neutral-text', path: 'references/neutral.txt' }] } })
  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === '选择文件夹')!.trigger('click')
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === '扫描文件')!.trigger('click')
  await flushPromises()
  return wrapper
}

it('唯一权重自动展示，基础录音不按文件顺序选择，转写只展示同名旁注', async () => {
  const wrapper = await scanImport([{ file_id: 'gpt', path: 'weights/voice.ckpt', role: 'gpt' },
    { file_id: 'sovits', path: 'weights/voice.pth', role: 'sovits' },
    { file_id: 'aux', path: 'pretrained_models/model.bin', role: 'unknown' }])
  const remoteOperation = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest')
  expect(wrapper.get('[aria-label="自动识别的语义权重"]').attributes('readonly')).toBeDefined()
  expect((wrapper.get('[aria-label="自动识别的声音权重"]').element as HTMLInputElement).value).toBe('weights/voice.pth')
  const selects = wrapper.findAll('select')
  const audio = selects[0]!
  const transcript = selects[1]!
  expect((audio.element as HTMLSelectElement).value).toBe('')
  expect(transcript.findAll('option')).toHaveLength(1)
  await audio.setValue('neutral')
  expect(transcript.text()).toContain('references/neutral.txt')
  expect(transcript.text()).not.toContain('apology.lab')
  await wrapper.get('textarea').setValue('旧录音转写')
  await audio.setValue('apology')
  expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('')
  await audio.setValue('')
  const start = vi.spyOn(productApi, 'startAnifLiveImport').mockResolvedValue({ task_id: 'task', status: 'pending_reference', phase: '待补参考' })
  vi.spyOn(productApi, 'getAnifLiveImport').mockResolvedValue({ task_id: 'task', status: 'pending_reference', phase: '待补参考' })
  await wrapper.findAll('button').find(button => button.text() === '确认下载并转换')!.trigger('click')
  await flushPromises()
  expect(start).toHaveBeenCalledWith(expect.objectContaining({ selected_gpt: 'gpt', selected_sovits: 'sovits' }))
  expect(start.mock.calls[0]![0].reference_audio).toBeUndefined()
  expect(remoteOperation).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('远程待补参考通过客户端原生选择器上传，再沿用服务端转写操作', async () => {
  state.host = { recovery: ref({ suspended: false }), snapshot: ref({ remote_mode: true }) }
  vi.spyOn(bridge, 'desktopPort').mockReturnValue({ available: true } as never)
  vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue(remoteResponse('resource.jobs', []))
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockResolvedValue({ jobs: [{
    task_id: 'remote-task', status: 'pending_reference', phase: '等待参考录音',
  }] })
  const importAsset = vi.spyOn(nativeDeviceApi, 'remoteTtsAssetImport').mockResolvedValue({
    asset: { asset_id: 'remote-reference', duration_ms: 5680 },
  })
  const upload = vi.spyOn(productApi, 'uploadTtsAsset')
  vi.spyOn(productApi, 'startTtsTranscribe').mockResolvedValue({ operation_id: 'remote-transcribe', model: 'aniflive', kind: 'transcribe', state: 'succeeded',
    result: { text: '服务端识别的参考转写' } })

  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()

  expect(wrapper.findAll('input[type="file"]')).toHaveLength(0)
  const importButton = wrapper.findAll('button').find(button => button.text() === '从客户端导入参考录音')
  expect(importButton).toBeDefined()
  await importButton!.trigger('click')
  await flushPromises()
  expect(importAsset).toHaveBeenCalledWith('aniflive', 'asset')
  expect(upload).not.toHaveBeenCalled()

  await wrapper.findAll('button').find(button => button.text() === '按现有音色库流程识别转写')!.trigger('click')
  await flushPromises()
  expect(productApi.startTtsTranscribe).toHaveBeenCalledWith('aniflive', 'remote-reference')
  expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('服务端识别的参考转写')
  wrapper.unmount()
})

it('导入页卸载后，迟到的转写启动回执只取消原任务且不再轮询', async () => {
  state.host = { recovery: ref({ suspended: false }), snapshot: ref({ remote_mode: true }) }
  vi.spyOn(bridge, 'desktopPort').mockReturnValue({ available: true } as never)
  vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue(remoteResponse('resource.jobs', []))
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockResolvedValue({ jobs: [{
    task_id: 'remote-task', status: 'pending_reference', phase: '等待参考录音',
  }] })
  vi.spyOn(nativeDeviceApi, 'remoteTtsAssetImport').mockResolvedValue({ asset: { asset_id: 'reference', duration_ms: 5000 } } as never)
  let resolveStart!: (value: never) => void
  vi.spyOn(productApi, 'startTtsTranscribe').mockImplementation(() => new Promise(resolve => { resolveStart = resolve }))
  const cancel = vi.spyOn(productApi, 'cancelTtsOperation').mockResolvedValue({} as never)
  const query = vi.spyOn(productApi, 'getTtsOperation')
  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === '从客户端导入参考录音')!.trigger('click')
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === '按现有音色库流程识别转写')!.trigger('click')
  wrapper.unmount()
  resolveStart({ operation_id: 'late-transcribe', state: 'running' } as never)
  await flushPromises()
  expect(cancel).toHaveBeenCalledExactlyOnceWith('late-transcribe')
  expect(query).not.toHaveBeenCalled()
})

it('取消远程参考文件选择时保留待补状态且不启动转写', async () => {
  state.host = { recovery: ref({ suspended: false }), snapshot: ref({ remote_mode: true }) }
  vi.spyOn(bridge, 'desktopPort').mockReturnValue({ available: true } as never)
  vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue(remoteResponse('resource.jobs', []))
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockResolvedValue({ jobs: [{
    task_id: 'remote-task', status: 'pending_reference', phase: '等待参考录音',
  }] })
  const importAsset = vi.spyOn(nativeDeviceApi, 'remoteTtsAssetImport').mockResolvedValue({
    cancelled: true, fields: { cancelled: '用户取消文件选择' },
  })
  const transcribe = vi.spyOn(productApi, 'startTtsTranscribe')

  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()
  const importButton = wrapper.findAll('button').find(button => button.text() === '从客户端导入参考录音')
  expect(importButton).toBeDefined()
  await importButton!.trigger('click')
  await flushPromises()

  expect(importAsset).toHaveBeenCalledWith('aniflive', 'asset')
  expect(transcribe).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('从客户端导入参考录音')
  expect(wrapper.text()).not.toContain('用户取消文件选择')
  wrapper.unmount()
})

it('远程模式禁用客户端本地文件夹并说明来源限制', async () => {
  state.host = { recovery: ref({ suspended: false }), snapshot: ref({ remote_mode: true }) }
  const chooseFolder = vi.fn()
  const local = { available: true, chooseAnifLiveImportFolder: chooseFolder,
    resourceInventory: vi.fn(), downloadResource: vi.fn(), resourceJobSnapshots: vi.fn(), startAnifLiveImport: vi.fn() }
  vi.spyOn(bridge, 'desktopPort').mockReturnValue(local as never)
  vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue(remoteResponse('resource.jobs', []))
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockResolvedValue({ jobs: [] })
  const scan = vi.spyOn(productApi, 'scanAnifLiveImport')

  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()

  const localTab = wrapper.findAll('button').find(button => button.text() === '本地文件夹')!
  expect(localTab.attributes('disabled')).toBeDefined()
  expect(wrapper.find('[aria-label="本地模型文件夹路径"]').exists()).toBe(false)
  expect(wrapper.text()).toContain('本页面暂不提供服务器目录选择')
  expect(chooseFolder).not.toHaveBeenCalled()
  expect(local.resourceInventory).not.toHaveBeenCalled()
  expect(scan).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('服务端 Python 任务接口失败时保留已读到的 ResourceManager 进度', async () => {
  state.host = { recovery: ref({ suspended: false }), snapshot: ref({ remote_mode: true }) }
  const taskId = 'f'.repeat(32)
  const snapshot = managedJob(`aniflive-import-${taskId}`, 'tts-aniflive-import', 'running', '构建推理引擎')
  snapshot.progress_kind = 'build'; snapshot.completed_bytes = 6; snapshot.total_bytes = 10
  const remote = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockResolvedValue(remoteResponse('resource.jobs', [snapshot]))
  vi.spyOn(bridge, 'desktopPort').mockReturnValue({ available: true,
    resourceJobSnapshots: vi.fn(), startAnifLiveImport: vi.fn() } as never)
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockRejectedValue(new Error('服务端 Python 核心暂不可用'))

  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()

  expect(remote).toHaveBeenCalledWith('resource.jobs', {})
  expect(wrapper.text()).toContain('构建推理引擎')
  expect(wrapper.text()).toContain('服务端 Python 核心暂不可用')
  expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('60')
  wrapper.unmount()
})

it('远程公开仓库导入使用服务端框架任务和后台转换调度，不启动客户端任务', async () => {
  state.host = { recovery: ref({ suspended: false }), snapshot: ref({ remote_mode: true }) }
  const local = { available: true, resourceInventory: vi.fn(), downloadResource: vi.fn(),
    resourceJobSnapshots: vi.fn(), startAnifLiveImport: vi.fn() }
  vi.spyOn(bridge, 'desktopPort').mockReturnValue(local as never)
  const taskId = 'd'.repeat(32)
  let frameworkStarted = false
  let frameworkReads = 0
  let serverImportStarted = false
  let serverImportReads = 0
  const remote = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockImplementation(async operation => {
    if (operation === 'resource.inventory') return remoteResponse(operation, [{
      resource_id: 'tts-aniflive-runtime', label: 'AnifLive 推理框架', installation_state: 'missing', can_start: false,
    }])
    if (operation === 'resource.install.start') {
      frameworkStarted = true
      return remoteResponse(operation, { job_id: 'server-framework-job' }, 202)
    }
    if (operation === 'resource.jobs') {
      if (!frameworkStarted) return remoteResponse(operation, [])
      if (!serverImportStarted) {
        frameworkReads += 1
        return remoteResponse(operation, [managedJob('server-framework-job', 'tts-aniflive-runtime',
          frameworkReads === 1 ? 'running' : 'complete', frameworkReads === 1 ? '配置框架' : '框架配置完成')])
      }
      serverImportReads += 1
      if (serverImportReads === 1) return remoteResponse(operation, [])
      if (serverImportReads === 2) return remoteResponse(operation, [managedJob(
        `aniflive-import-${taskId}`, 'tts-aniflive-import', 'running', '构建推理引擎')])
      return remoteResponse(operation, [managedJob(`aniflive-import-${taskId}`, 'tts-aniflive-import', 'complete', '转换完成')])
    }
    throw new Error(`意外的远程操作：${operation}`)
  })
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockResolvedValue({ jobs: [] })
  vi.spyOn(productApi, 'scanAnifLiveImport').mockResolvedValue({ scan_id: 'remote-scan', source: 'user/model',
    source_type: 'huggingface', revision: 'abc123', files: [], candidates: { weights: [
      { file_id: 'gpt', path: 'voice.ckpt', role: 'gpt' }, { file_id: 'sovits', path: 'voice.pth', role: 'sovits' }],
    audio: [], transcripts: [] } })
  const start = vi.spyOn(productApi, 'startAnifLiveImport').mockImplementation(async () => {
    serverImportStarted = true
    return { task_id: taskId, status: 'ready_to_convert', phase: '等待服务端转换' }
  })
  const get = vi.spyOn(productApi, 'getAnifLiveImport')
    .mockResolvedValueOnce({ task_id: taskId, status: 'ready_to_convert', phase: '等待服务端调度' })
    .mockResolvedValueOnce({ task_id: taskId, model_id: 'remote-model', status: 'complete', phase: '转换完成' })

  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()
  await wrapper.get('input[type="url"]').setValue('https://huggingface.co/user/model')
  await wrapper.findAll('button').find(button => button.text() === '扫描文件')!.trigger('click')
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === '确认下载并转换')!.trigger('click')
  await flushPromises()
  expect(remote).toHaveBeenCalledWith('resource.inventory', {})
  expect(remote).toHaveBeenCalledWith('resource.install.start', { resource_id: 'tts-aniflive-runtime' })
  expect(start).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1000)
  await flushPromises()
  await vi.runAllTimersAsync()
  await flushPromises()

  expect(start).toHaveBeenCalledOnce()
  expect(get).toHaveBeenCalledTimes(2)
  expect(remote.mock.calls.some(([operation]) => operation === 'resource.jobs')).toBe(true)
  expect(remote).not.toHaveBeenCalledWith('resource.job.start', expect.anything())
  expect(local.resourceInventory).not.toHaveBeenCalled()
  expect(local.downloadResource).not.toHaveBeenCalled()
  expect(local.resourceJobSnapshots).not.toHaveBeenCalled()
  expect(local.startAnifLiveImport).not.toHaveBeenCalled()
  expect(wrapper.emitted('completed')?.[0]?.[0]).toMatchObject({ task_id: taskId, model_id: 'remote-model' })
  wrapper.unmount()
})

it('本地 ready_to_convert 仍交给本机资源管理器转换', async () => {
  const wrapper = await scanImport([{ file_id: 'gpt', path: 'voice.ckpt', role: 'gpt' },
    { file_id: 'sovits', path: 'voice.pth', role: 'sovits' }])
  const taskId = 'a'.repeat(32)
  let localStarted = false
  const localStart = vi.fn(async () => {
    localStarted = true
    return { job_id: `aniflive-import-${taskId}` }
  })
  const local = bridge.desktopPort()
  Object.assign(local, {
    startAnifLiveImport: localStart,
    resourceJobSnapshots: async () => localStarted
      ? [managedJob(`aniflive-import-${taskId}`, 'tts-aniflive-import', 'complete', '转换完成')] : [],
  })
  const remote = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest')
  vi.spyOn(productApi, 'startAnifLiveImport').mockResolvedValue({ task_id: taskId,
    status: 'ready_to_convert', phase: '等待本机转换' })
  const get = vi.spyOn(productApi, 'getAnifLiveImport')
    .mockResolvedValueOnce({ task_id: taskId, status: 'ready_to_convert', phase: '等待本机转换' })
    .mockResolvedValueOnce({ task_id: taskId, model_id: 'local-model', status: 'complete', phase: '转换完成' })

  await wrapper.findAll('button').find(button => button.text() === '确认下载并转换')!.trigger('click')
  await flushPromises()
  expect(localStart).toHaveBeenCalledWith(taskId)
  expect(remote).not.toHaveBeenCalled()
  await flushPromises()
  expect(get).toHaveBeenCalledTimes(2)
  wrapper.unmount()
})

it('远程失败任务的明确重试只启动服务端资源任务', async () => {
  state.host = { recovery: ref({ suspended: false }), snapshot: ref({ remote_mode: true }) }
  const local = { available: true, startAnifLiveImport: vi.fn(), resourceJobSnapshots: vi.fn() }
  vi.spyOn(bridge, 'desktopPort').mockReturnValue(local as never)
  const taskId = 'e'.repeat(32)
  const remote = vi.spyOn(nativeDeviceApi, 'remoteManagementRequest').mockImplementation(async operation => {
    if (operation === 'resource.jobs') return remoteResponse(operation, [])
    if (operation === 'resource.job.start') return remoteResponse(operation, { job_id: `aniflive-import-${taskId}` }, 202)
    throw new Error(`意外的远程操作：${operation}`)
  })
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockResolvedValue({ jobs: [{ task_id: taskId,
    status: 'failed', phase: '上次转换失败', stage_ready: true }] })
  vi.spyOn(productApi, 'getAnifLiveImport').mockResolvedValue({ task_id: taskId, status: 'failed', phase: '等待重试', stage_ready: true })

  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === '继续导入')!.trigger('click')
  await flushPromises()

  expect(remote).toHaveBeenCalledWith('resource.job.start', { kind: 'aniflive_import', task_id: taskId })
  expect(local.startAnifLiveImport).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('框架配置成功后才启动模型下载，配置失败不启动下载', async () => {
  for (const failed of [false, true]) {
    const wrapper = await scanImport([{ file_id: 'gpt', path: 'voice.ckpt', role: 'gpt' }, { file_id: 'sovits', path: 'voice.pth', role: 'sovits' }])
    const download = vi.fn().mockResolvedValue({ job_id: 'framework' })
    let state = 'running'
    Object.assign(bridge.desktopPort(), {
      resourceInventory: async () => [], downloadResource: download,
      resourceJobSnapshots: async () => [{ job_id: 'framework', state, error: '配置失败' }],
    })
    const start = vi.spyOn(productApi, 'startAnifLiveImport').mockResolvedValue({ task_id: 'task', status: 'pending_reference', phase: '待补参考' })
    vi.spyOn(productApi, 'getAnifLiveImport').mockResolvedValue({ task_id: 'task', status: 'pending_reference', phase: '待补参考' })
    await wrapper.findAll('button').find(button => button.text() === '确认下载并转换')!.trigger('click')
    await flushPromises()
    expect(download).toHaveBeenCalledWith('tts-aniflive-runtime')
    expect(start).not.toHaveBeenCalled()
    state = failed ? 'failed' : 'complete'
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    if (failed) { expect(start).not.toHaveBeenCalled(); expect(wrapper.text()).toContain('配置失败') }
    else expect(start).toHaveBeenCalledOnce()
    wrapper.unmount()
    vi.restoreAllMocks()
  }
})

it('多份同类权重等待明确选择，每个选单只包含对应角色', async () => {
  const wrapper = await scanImport([{ file_id: 'old', path: 'old.ckpt', role: 'gpt' },
    { file_id: 'new', path: 'new.ckpt', role: 'gpt' }, { file_id: 'sovits', path: 'voice.pth', role: 'sovits' }])
  const gpt = wrapper.findAll('select')[0]!
  expect((gpt.element as HTMLSelectElement).value).toBe('')
  expect(gpt.text()).toContain('发现多份语义权重')
  expect(gpt.text()).not.toContain('voice.pth')
  const submit = wrapper.findAll('button').find(button => button.text() === '确认下载并转换')!
  expect(submit.attributes('disabled')).toBeDefined()
  await gpt.setValue('new')
  expect(submit.attributes('disabled')).toBeUndefined()
  wrapper.unmount()
})

it('构建暂停业务期间从原生快照恢复实际进度，恢复后才读取参考登记结果', async () => {
  const recovery = ref({ suspended: true })
  state.host = { recovery }
  const taskId = 'a'.repeat(32)
  let nativeState = 'running'
  const snapshots = vi.fn(async () => [{ job_id: `aniflive-import-${taskId}`, resource_id: 'tts-aniflive-import',
    state: nativeState, phase: '构建推理引擎：已完成 3 / 9', completed_bytes: 9, total_bytes: 15 }])
  vi.spyOn(bridge, 'desktopPort').mockReturnValue({ available: true, resourceJobSnapshots: snapshots } as never)
  const pending = vi.spyOn(productApi, 'pendingAnifLiveImports')
  const get = vi.spyOn(productApi, 'getAnifLiveImport').mockResolvedValue({ task_id: taskId, model_id: 'test-model',
    name: '测试模型', status: 'complete', phase: '参考登记完成', progress: 100,
    reference_registration: { ready: 2, pending: 0, voice_id: 'voice', revision: 1 } })
  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()
  expect(pending).not.toHaveBeenCalled()
  expect(get).not.toHaveBeenCalled()
  expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('60')
  expect(wrapper.text()).toContain('构建推理引擎：已完成 3 / 9')
  await vi.advanceTimersByTimeAsync(2200)
  expect(get).not.toHaveBeenCalled()
  nativeState = 'complete'; recovery.value.suspended = false
  await vi.advanceTimersByTimeAsync(1100)
  await flushPromises()
  expect(wrapper.emitted('completed')?.[0]?.[0]).toMatchObject({ model_id: 'test-model', task_id: taskId })
  expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
  wrapper.unmount()
})

it('未知构建总量使用不确定进度，不能显示伪造的零进度', async () => {
  state.host = { recovery: ref({ suspended: true }) }
  vi.spyOn(bridge, 'desktopPort').mockReturnValue({ available: true, resourceJobSnapshots: async () => [{
    job_id: `aniflive-import-${'b'.repeat(32)}`, resource_id: 'tts-aniflive-import', state: 'running', phase: '等待显卡构建' }] } as never)
  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()
  expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
  expect(wrapper.text()).toContain('等待显卡构建')
  wrapper.unmount()
})

it('原生任务等待参考登记时读取业务接口并完成交接，不持续等待原生终态', async () => {
  state.host = { recovery: ref({ suspended: false }) }
  const taskId = 'c'.repeat(32)
  vi.spyOn(bridge, 'desktopPort').mockReturnValue({ available: true, resourceJobSnapshots: async () => [{
    job_id: `aniflive-import-${taskId}`, resource_id: 'tts-aniflive-import', state: 'running', phase: '正在登记音色参考' }] } as never)
  vi.spyOn(productApi, 'pendingAnifLiveImports').mockResolvedValue({ jobs: [{ task_id: taskId, status: 'converting', phase: '等待参考登记' }] })
  const get = vi.spyOn(productApi, 'getAnifLiveImport').mockResolvedValue({ task_id: taskId,
    model_id: 'test-model', status: 'complete', phase: '参考资料登记完成', progress: 100 })
  const wrapper = mount(AnifLiveModelImport, { props: { open: true } })
  await flushPromises()
  expect(get).toHaveBeenCalledWith(taskId)
  expect(wrapper.emitted('completed')?.[0]?.[0]).toMatchObject({ model_id: 'test-model' })
  wrapper.unmount()
})

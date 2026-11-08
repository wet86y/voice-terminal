// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { productApi, type AnifLiveImportScan } from '../api'
import * as bridge from '../desktop-port'
import AnifLiveModelImport from './AnifLiveModelImport.vue'

const state = vi.hoisted(() => ({ host: undefined as unknown }))
vi.mock('../application-host', () => ({ useApplicationHost: () => state.host }))
beforeEach(() => { vi.useFakeTimers(); vi.restoreAllMocks() })
afterEach(() => { vi.useRealTimers() })

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

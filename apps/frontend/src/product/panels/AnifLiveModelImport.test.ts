// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { productApi } from '../api'
import * as bridge from '../desktop-port'
import AnifLiveModelImport from './AnifLiveModelImport.vue'

const state = vi.hoisted(() => ({ host: undefined as unknown }))
vi.mock('../application-host', () => ({ useApplicationHost: () => state.host }))
beforeEach(() => { vi.useFakeTimers(); vi.restoreAllMocks() })
afterEach(() => { vi.useRealTimers() })

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
  expect(wrapper.get('progress').attributes('value')).toBe('60')
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
  expect(wrapper.get('progress').attributes('value')).toBeUndefined()
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

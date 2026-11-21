// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTtsOperationScope } from './tts-operation'
import type { ProductTtsOperation } from './types'

const operation = (state: ProductTtsOperation['state']): ProductTtsOperation => ({
  operation_id: 'owned-task', kind: 'minimax_preview', model: 'minimax', state,
} as ProductTtsOperation)
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

afterEach(() => { vi.useRealTimers(); delete window.__VOICE_APPLICATION__ })

describe('语音任务沿页面和目标连接生命周期收尾', () => {
  it('卸载先于启动回执时取消同一任务，且不再轮询', async () => {
    const start = deferred<ProductTtsOperation>()
    const client = { getTtsOperation: vi.fn(), cancelTtsOperation: vi.fn().mockResolvedValue(undefined) }
    const scope = createTtsOperationScope(client)
    const result = scope.run(() => start.promise).catch(error => error)
    scope.dispose()
    start.resolve(operation('running'))
    expect((await result).name).toBe('AbortError')
    expect(client.cancelTtsOperation).toHaveBeenCalledExactlyOnceWith('owned-task')
    expect(client.getTtsOperation).not.toHaveBeenCalled()
  })

  it('状态查询在途时卸载会立即取消，迟到成功不会写回页面', async () => {
    vi.useFakeTimers()
    const query = deferred<ProductTtsOperation>()
    const client = { getTtsOperation: vi.fn(() => query.promise), cancelTtsOperation: vi.fn().mockResolvedValue(undefined) }
    const scope = createTtsOperationScope(client)
    const result = scope.run(async () => operation('running'), 300).catch(error => error)
    await vi.advanceTimersByTimeAsync(300)
    expect(client.getTtsOperation).toHaveBeenCalledTimes(1)
    scope.dispose(); scope.dispose()
    expect(client.cancelTtsOperation).toHaveBeenCalledTimes(1)
    query.resolve(operation('succeeded'))
    expect((await result).name).toBe('AbortError')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('正常任务保持查询至终态，并不发出取消', async () => {
    vi.useFakeTimers()
    const client = { getTtsOperation: vi.fn().mockResolvedValue(operation('succeeded')),
      cancelTtsOperation: vi.fn().mockResolvedValue(undefined) }
    const scope = createTtsOperationScope(client)
    const result = scope.run(async () => operation('running'), 300)
    await vi.advanceTimersByTimeAsync(300)
    expect((await result).state).toBe('succeeded')
    scope.dispose()
    expect(client.cancelTtsOperation).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('连接切换后不向新目标发送旧任务编号', async () => {
    const start = deferred<ProductTtsOperation>()
    window.__VOICE_APPLICATION__ = { remote_mode: true, host_id: 'old', endpoint_revision: 1 } as never
    const client = { getTtsOperation: vi.fn(), cancelTtsOperation: vi.fn() }
    const scope = createTtsOperationScope(client)
    const result = scope.run(() => start.promise).catch(error => error)
    window.__VOICE_APPLICATION__ = { remote_mode: true, host_id: 'new', endpoint_revision: 2 } as never
    start.resolve(operation('running'))
    expect((await result).name).toBe('AbortError')
    expect(client.getTtsOperation).not.toHaveBeenCalled()
    expect(client.cancelTtsOperation).not.toHaveBeenCalled()
  })

  it('上传回执迟到时，不能在已卸载页面继续提交转写', async () => {
    const upload = deferred<void>()
    const startTranscribe = vi.fn(async () => operation('running'))
    const scope = createTtsOperationScope({ getTtsOperation: vi.fn(), cancelTtsOperation: vi.fn() })
    const result = scope.run(async assertActive => {
      await upload.promise
      assertActive()
      return startTranscribe()
    }).catch(error => error)
    scope.dispose(); upload.resolve()
    expect((await result).name).toBe('AbortError')
    expect(startTranscribe).not.toHaveBeenCalled()
  })
})

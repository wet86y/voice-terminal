import { describe, expect, it } from 'vitest'
import { workerStatusLabel } from './ttsUi'

describe('Worker（工作进程）状态显示', () => {
  it('covers switching, starting, ready, draining, failure and stopped', () => {
    expect(workerStatusLabel({ state: 'ready' }, true)).toBe('切换中')
    expect(workerStatusLabel({ state: 'starting' }, false)).toBe('启动中')
    expect(workerStatusLabel({ state: 'ready' }, false)).toBe('已就绪')
    expect(workerStatusLabel({ state: 'draining' }, false)).toBe('排空中')
    expect(workerStatusLabel({ state: 'error' }, false)).toBe('失败')
    expect(workerStatusLabel({ state: 'stopped' }, false)).toBe('已停止')
  })
})

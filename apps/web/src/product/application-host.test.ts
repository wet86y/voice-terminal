import { describe, expect, it } from 'vitest'
import { acceptApplicationSnapshot, applicationStateLabel, type ApplicationSnapshot } from './application-host'

describe('独立应用状态投影', () => {
  const snapshot = (instance_id: string, revision: number) => ({ instance_id, revision } as ApplicationSnapshot)
  it('刷新接收快照，丢弃同实例的过期推送', () => {
    expect(acceptApplicationSnapshot(null, snapshot('first', 2))).toBe(true)
    expect(acceptApplicationSnapshot(snapshot('first', 4), snapshot('first', 3))).toBe(false)
    expect(acceptApplicationSnapshot(snapshot('first', 4), snapshot('second', 1))).toBe(true)
  })
  it('失败、依赖阻塞与退出状态明确区分', () => {
    expect(applicationStateLabel('failed')).toBe('需要检查')
    expect(applicationStateLabel('blocked')).toBe('依赖不可用')
    expect(applicationStateLabel('stopped')).toBe('已退出')
  })
})

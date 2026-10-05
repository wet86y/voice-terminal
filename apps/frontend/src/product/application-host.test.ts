import { describe, expect, it } from 'vitest'
import { acceptApplicationSnapshot, applicationStateLabel, applicationApiReady, type ApplicationSnapshot } from './application-host'

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
  it('接口已就绪但未配置凭据时，保留设置访问；接口退出或未启动时拒绝请求', () => {
    const value = { state: 'failed', core_available: false, startup_complete: true, stopping: false,
      voice_api_url: 'http://127.0.0.1:8765', components: [{ id: 'voice', pid: 123, state: 'failed' }] } as ApplicationSnapshot
    expect(applicationApiReady(value)).toBe(true)
    expect(applicationApiReady({ ...value, stopping: true })).toBe(false)
    expect(applicationApiReady({ ...value, voice_api_url: '' })).toBe(false)
    expect(applicationApiReady({ ...value, components: [{ ...value.components[0], pid: null }] })).toBe(false)
    expect(applicationApiReady({ ...value, components: [{ ...value.components[0], state: 'starting' }] })).toBe(false)
  })
})

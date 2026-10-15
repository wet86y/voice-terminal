import { describe, expect, it } from 'vitest'
import {
  acceptApplicationSnapshot, applicationApiReady, applicationCorePhase, applicationOverlay,
  applicationRuntimeFailure, applicationStateLabel, type ApplicationSnapshot,
} from './application-host'

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
  it('轻量接口监听后允许设置访问，模型与完整启动仍保持加载状态', () => {
    const value = { startup_complete: false, core_available: false, stopping: false,
      voice_api_url: 'http://127.0.0.1:8765', voice_api_ready: true,
      components: [{ id: 'voice', pid: 123, state: 'waiting' }] } as ApplicationSnapshot
    expect(applicationApiReady(value)).toBe(true)
    expect(applicationApiReady({ ...value, voice_api_ready: false })).toBe(false)
    expect(applicationApiReady({ ...value, stopping: true })).toBe(false)
    expect(applicationApiReady({ ...value, components: [{ ...value.components[0], pid: null }] })).toBe(false)
    expect(value.startup_complete).toBe(false)
    expect(value.core_available).toBe(false)
  })
  it('运行期的组件故障不再弹出全屏启动画面', () => {
    const running = { startup_complete: true, state: 'failed', core_available: false, stopping: false,
      components: [] } as unknown as ApplicationSnapshot
    expect(applicationOverlay(running)).toBe(false)
    expect(applicationOverlay(running, { inspecting: true })).toBe(false)
    // 启动尚未完成、正在退出与会话失效仍然全屏。
    expect(applicationOverlay({ ...running, startup_complete: false })).toBe(true)
    expect(applicationOverlay({ ...running, startup_complete: false }, { inspecting: true })).toBe(false)
    expect(applicationOverlay({ ...running, state: 'stopping' })).toBe(true)
    expect(applicationOverlay({ ...running, state: 'stopped' })).toBe(true)
    expect(applicationOverlay(running, { sessionInvalid: true })).toBe(true)
    expect(applicationOverlay(null)).toBe(true)
  })
  it('核心能力阶段区分加载中与已失败，运行期失败才显示横幅', () => {
    const base = { startup_complete: true, components: [] } as unknown as ApplicationSnapshot
    expect(applicationCorePhase({ ...base, core_phase: 'starting', core_available: false })).toBe('starting')
    expect(applicationCorePhase({ ...base, core_phase: 'failed', core_available: false })).toBe('failed')
    expect(applicationCorePhase({ ...base, core_phase: 'ready', core_available: true })).toBe('ready')
    // 旧宿主缺少 core_phase 时按 core_available 回落。
    expect(applicationCorePhase({ ...base, core_available: true })).toBe('ready')
    expect(applicationCorePhase({ ...base, core_available: false })).toBe('failed')
    expect(applicationRuntimeFailure({ ...base, core_phase: 'starting', core_available: false })).toBe(false)
    expect(applicationRuntimeFailure({ ...base, core_phase: 'failed', core_available: false })).toBe(true)
    expect(applicationRuntimeFailure({ ...base, core_phase: 'failed', core_available: false, startup_complete: false })).toBe(false)
  })
})

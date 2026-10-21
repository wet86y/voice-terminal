import { describe, expect, it } from 'vitest'
import { componentResources } from './component-resources'
import type { ApplicationSnapshot } from './application-host'
import type { ProductComputeStatus } from './types'

/** fixture（资源采样）：只设置资源分组真正读取的字段。 */
function fixture(footprintPid = 1, footprintCreated = 10) {
  const snapshot = { resource_processes: [
    { component_id: 'voice', pid: 1, created_at: 10, memory_rss_bytes: 1000, memory_private_commit_bytes: 2000, gpu_dedicated_bytes: 30 },
    { component_id: 'asr', pid: 2, created_at: 20, memory_rss_bytes: 500, memory_private_commit_bytes: 800, gpu_dedicated_bytes: 40 },
    { component_id: 'analysis', pid: 3, created_at: 30, memory_rss_bytes: 600, memory_private_commit_bytes: 900, gpu_dedicated_bytes: 50 },
  ] } as unknown as ApplicationSnapshot
  const footprint = (pid: number, created: number, rss: number, commit: number) => ({
    pid, created_at: created, basis: 'load_delta', rss_bytes: rss, private_commit_bytes: commit,
  })
  const compute = { models: {
    kws: { service: 'READY', resource_footprint: footprint(footprintPid, footprintCreated, 200, 400) },
    voice_vad: { service: 'READY', resource_footprint: footprint(1, 10, 300, 500) },
    smart_turn: { service: 'READY', device: 'cuda', resource_footprint: footprint(3, 30, 100, 200) },
  }, auxiliary: {
    hotwords: { service: 'READY', resource_footprint: footprint(1, 10, 100, 200) },
    punctuation: { service: 'READY', resource_footprint: footprint(2, 20, 150, 250) },
  } } as unknown as ProductComputeStatus
  return { snapshot, compute }
}

describe('共享模型的内存归属估算', () => {
  /** totals（分组总量）：检查归属变化前后的常驻、提交内存及显存守恒。 */
  function totals(result: ReturnType<typeof componentResources>) {
    return (['memory_rss_bytes', 'memory_private_commit_bytes', 'gpu_dedicated_bytes'] as const)
      .map(key => [...result.values()].reduce((sum, row) => sum + (row[key] ?? 0), 0))
  }

  it('扣除正确进程的增量，应用总量保持不变且显存不重复分配', () => {
    const { snapshot, compute } = fixture()
    const result = componentResources(snapshot, compute)
    expect(result.get('voice-system')?.memory_rss_bytes).toBe(400)
    expect(result.get('asr')?.memory_rss_bytes).toBe(350)
    expect(result.get('smart_turn')?.memory_rss_bytes).toBe(600)
    expect([...result.values()].reduce((sum, row) => sum + (row.memory_rss_bytes ?? 0), 0)).toBe(2100)
    expect([...result.values()].reduce((sum, row) => sum + (row.memory_private_commit_bytes ?? 0), 0)).toBe(3700)
    expect(result.get('smart_turn')?.gpu_dedicated_bytes).toBe(50)
    expect(result.has('analysis')).toBe(false)
  })

  it.each([null, 'cpu'])('模型设备为 %s 时，分析进程显存保留在语音服务并在设备同步后转归模型', device => {
    const { snapshot, compute } = fixture()
    compute.models.smart_turn!.device = device
    const pending = componentResources(snapshot, compute)
    expect(pending.get('smart_turn')?.memory_rss_bytes).toBe(600)
    expect(pending.get('smart_turn')?.gpu_dedicated_bytes).toBeUndefined()
    expect(pending.get('voice-system')?.gpu_dedicated_bytes).toBe(80)
    expect(totals(pending)).toEqual([2100, 3700, 120])

    compute.models.smart_turn!.device = 'cuda:0'
    const synchronized = componentResources(snapshot, compute)
    expect(synchronized.get('voice-system')?.gpu_dedicated_bytes).toBe(30)
    expect(synchronized.get('smart_turn')?.gpu_dedicated_bytes).toBe(50)
    expect(totals(synchronized)).toEqual([2100, 3700, 120])
  })

  it('状态未返回、模型停用或进程身份过期时保留余量，状态同步后不重复计量', () => {
    const { snapshot, compute } = fixture()
    // 同一受管进程重复出现时仍只计一次。
    snapshot.resource_processes!.push({ ...snapshot.resource_processes![2]! })
    for (const status of [null, { models: {} }, {
      models: { smart_turn: { ...compute.models.smart_turn, service: 'DISABLED' } },
    }, { models: { smart_turn: { ...compute.models.smart_turn, resource_footprint: {
      ...compute.models.smart_turn!.resource_footprint, created_at: 29,
    } } } }]) {
      const pending = componentResources(snapshot, status as ProductComputeStatus | null)
      expect(pending.has('smart_turn')).toBe(false)
      expect(pending.get('voice-system')?.memory_rss_bytes).toBeGreaterThanOrEqual(600)
      expect(totals(pending)).toEqual([2100, 3700, 120])
    }
    const synchronized = componentResources(snapshot, compute)
    expect(synchronized.get('voice-system')?.memory_rss_bytes).toBe(400)
    expect(synchronized.get('smart_turn')?.memory_rss_bytes).toBe(600)
    expect(totals(synchronized)).toEqual([2100, 3700, 120])
  })

  it('无法归属的进程指标缺失时保持未知，已知显存仍保留', () => {
    const { snapshot } = fixture()
    snapshot.resource_processes![2]!.memory_rss_bytes = null
    snapshot.resource_processes![2]!.memory_private_commit_bytes = null
    const pending = componentResources(snapshot, null)
    expect(pending.get('voice-system')?.memory_rss_bytes).toBeNull()
    expect(pending.get('voice-system')?.memory_private_commit_bytes).toBeUndefined()
    expect(pending.get('voice-system')?.gpu_dedicated_bytes).toBe(80)
  })

  it.each(['memory_rss_bytes', 'memory_private_commit_bytes', 'gpu_dedicated_bytes'] as const)(
    '模型身份已匹配且其他余量为零时，缺失的 %s（进程资源指标）仍保持未知', key => {
      const { snapshot, compute } = fixture()
      const analysis = snapshot.resource_processes![2]!
      analysis.memory_rss_bytes = 0
      analysis.memory_private_commit_bytes = 0
      analysis.gpu_dedicated_bytes = 0
      analysis[key] = null
      const pending = componentResources(snapshot, compute)
      expect(pending.get('voice-system')?.[key]).toBe(key === 'memory_rss_bytes' ? null : undefined)
      expect(pending.get('smart_turn')?.[key]).toBe(key === 'memory_rss_bytes' ? null : undefined)
    },
  )

  it('拒绝旧进程及编号复用后的采样', () => {
    for (const [pid, created] of [[9, 10], [1, 11]]) {
      const { snapshot, compute } = fixture(pid, created)
      expect(componentResources(snapshot, compute).has('kws')).toBe(false)
    }
  })

  it('估算大于当前实际占用时以进程余量为上限', () => {
    const { snapshot, compute } = fixture()
    compute.models.kws!.resource_footprint!.rss_bytes = 9000
    const result = componentResources(snapshot, compute)
    expect(result.get('kws')?.memory_rss_bytes).toBe(1000)
    expect(result.get('voice-system')?.memory_rss_bytes).toBe(0)
    expect([...result.values()].reduce((sum, row) => sum + (row.memory_rss_bytes ?? 0), 0)).toBe(2100)
  })
})

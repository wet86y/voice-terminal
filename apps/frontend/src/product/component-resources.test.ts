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
    smart_turn: { service: 'READY', resource_footprint: footprint(3, 30, 100, 200) },
  }, auxiliary: {
    hotwords: { service: 'READY', resource_footprint: footprint(1, 10, 100, 200) },
    punctuation: { service: 'READY', resource_footprint: footprint(2, 20, 150, 250) },
  } } as unknown as ProductComputeStatus
  return { snapshot, compute }
}

describe('共享模型的内存归属估算', () => {
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

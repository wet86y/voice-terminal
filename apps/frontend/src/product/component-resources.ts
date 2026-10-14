import type { ApplicationComponent, ApplicationSnapshot } from './application-host'
import type { ProductLocalModelId, ProductComputeStatus } from './types'

/** modelComponentOwners（模型所属进程）：状态、资源和重试使用同一所有者。 */
export const modelComponentOwners: Record<ProductLocalModelId, string> = {
  paraformer: 'asr', sensevoice: 'analysis', voice_vad: 'voice', kws: 'voice', smart_turn: 'analysis', campplus: 'speaker',
}

/** ComponentResources（组件资源分组）：与宿主总量共用同一采样，按进程身份只计一次。 */
export type ComponentResources = Pick<ApplicationComponent, 'memory_rss_bytes' | 'memory_private_commit_bytes' | 'gpu_dedicated_bytes'>
const separateOwners = new Set(['asr', 'analysis', 'speaker', 'dsh', 'local-tts'])
/** resourceGroup（展示分组）：其余进程统一计入语音系统。 */
export function resourceGroup(owner: string) { return separateOwners.has(owner) ? owner : 'voice-system' }

export function componentResources(snapshot: ApplicationSnapshot | null, compute?: ProductComputeStatus | null): Map<string, ComponentResources> {
  const groups = new Map<string, ComponentResources[]>()
  const processes = snapshot?.resource_processes ?? snapshot?.components.filter(row => row.pid && !row.external)
    .map(row => ({ ...row, component_id: row.id })) ?? []
  const seen = new Set<string>()
  for (const row of processes) {
    const identity = `${row.pid}:${row.created_at}`
    if (seen.has(identity)) continue
    seen.add(identity)
    const group = resourceGroup(row.component_id)
    const residual = { ...row }
    // 归属增量按当前进程身份校验，并以进程当前余量为限；各条目合计始终不超过宿主总量。
    const candidates = [
      ...(['kws', 'voice_vad', 'smart_turn'] as const).map(id => ({ id, status: compute?.models[id] })),
      ...(['hotwords', 'punctuation'] as const).map(id => ({ id, status: compute?.auxiliary?.[id] })),
    ]
    for (const { id, status } of candidates) {
      const footprint = status?.resource_footprint
      if (!footprint || footprint.basis !== 'load_delta' || footprint.pid !== row.pid
        || footprint.created_at !== row.created_at || status?.service === 'DISABLED') continue
      const assigned: ComponentResources = { memory_rss_bytes: null }
      for (const [key, source] of [['memory_rss_bytes', 'rss_bytes'], ['memory_private_commit_bytes', 'private_commit_bytes']] as const) {
        const remaining = residual[key], estimate = footprint[source]
        if (typeof remaining !== 'number' || !Number.isFinite(remaining) || remaining < 0
          || typeof estimate !== 'number' || !Number.isFinite(estimate) || estimate < 0) continue
        assigned[key] = Math.min(remaining, estimate)
        residual[key] = remaining - assigned[key]!
      }
      groups.set(id, [...(groups.get(id) ?? []), assigned])
    }
    groups.set(group, [...(groups.get(group) ?? []), residual])
  }
  const total = (rows: ComponentResources[], key: keyof ComponentResources) =>
    rows.every(row => typeof row[key] === 'number' && Number.isFinite(row[key]) && row[key]! >= 0)
      ? rows.reduce((sum, row) => sum + row[key]!, 0) : undefined
  return new Map([...groups].map(([id, rows]) => [id, {
    memory_rss_bytes: total(rows, 'memory_rss_bytes') ?? null,
    memory_private_commit_bytes: total(rows, 'memory_private_commit_bytes'),
    gpu_dedicated_bytes: total(rows, 'gpu_dedicated_bytes'),
  }]))
}

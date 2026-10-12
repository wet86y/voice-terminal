import type { ApplicationComponent, ApplicationSnapshot } from './application-host'
import type { ProductLocalModelId } from './types'

/** modelComponentOwners（模型所属进程）：状态、资源和重试使用同一所有者。 */
export const modelComponentOwners: Record<ProductLocalModelId, string> = {
  paraformer: 'asr', sensevoice: 'analysis', voice_vad: 'voice', kws: 'voice', smart_turn: 'analysis', campplus: 'speaker',
}

/** ComponentResources（组件资源分组）：与宿主总量共用同一采样，按进程身份只计一次。 */
export type ComponentResources = Pick<ApplicationComponent, 'memory_rss_bytes' | 'memory_private_commit_bytes' | 'gpu_dedicated_bytes'>
const separateOwners = new Set(['asr', 'analysis', 'speaker', 'dsh'])
/** resourceGroup（展示分组）：其余进程统一计入语音系统。 */
export function resourceGroup(owner: string) { return separateOwners.has(owner) ? owner : 'voice-system' }

export function componentResources(snapshot: ApplicationSnapshot | null): Map<string, ComponentResources> {
  const groups = new Map<string, ComponentResources[]>()
  const processes = snapshot?.resource_processes ?? snapshot?.components.filter(row => row.pid && !row.external)
    .map(row => ({ ...row, component_id: row.id })) ?? []
  const seen = new Set<string>()
  for (const row of processes) {
    const identity = `${row.pid}:${row.created_at}`
    if (seen.has(identity)) continue
    seen.add(identity)
    const group = resourceGroup(row.component_id)
    groups.set(group, [...(groups.get(group) ?? []), row])
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

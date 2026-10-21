import type { ApplicationComponent, ApplicationSnapshot } from './application-host'
import type { ProductLocalModelId, ProductComputeStatus } from './types'

/** modelComponentOwners（模型所属进程）：状态、资源和重试使用同一所有者。 */
export const modelComponentOwners: Record<ProductLocalModelId, string> = {
  paraformer: 'asr', emotion: 'analysis', voice_vad: 'voice', kws: 'voice', smart_turn: 'analysis', campplus: 'speaker',
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
    const analysisShares = new Map<string, ComponentResources>()
    // 归属增量按当前进程身份校验，并以进程当前余量为限；各条目合计始终不超过宿主总量。
    const candidates = [
      ...(['kws', 'voice_vad', 'smart_turn', 'emotion'] as const).map(id => ({ id, status: compute?.models?.[id] })),
      ...(['hotwords', 'punctuation', 'acoustic_events'] as const).map(id => ({ id, status: compute?.auxiliary?.[id] })),
    ]
    for (const { id, status } of candidates) {
      const footprint = status?.resource_footprint
      if (!footprint || footprint.basis !== 'load_delta' || footprint.pid !== row.pid
        || footprint.created_at !== row.created_at || status?.service === 'DISABLED') continue
      // analysis（分析进程）的模型卡会承接共享余量；已知进程指标下把未知的模型增量视作零，
      // 让余量继续参与分摊，而不把同一项指标标成未知。
      const assigned: ComponentResources = {
        memory_rss_bytes: group === 'analysis' && typeof row.memory_rss_bytes === 'number' ? 0 : null,
        ...(group === 'analysis' && typeof row.memory_private_commit_bytes === 'number' ? { memory_private_commit_bytes: 0 } : {}),
      }
      for (const [key, source] of [['memory_rss_bytes', 'rss_bytes'], ['memory_private_commit_bytes', 'private_commit_bytes']] as const) {
        const remaining = residual[key], estimate = footprint[source]
        if (typeof remaining !== 'number' || !Number.isFinite(remaining) || remaining < 0
          || typeof estimate !== 'number' || !Number.isFinite(estimate) || estimate < 0) continue
        assigned[key] = Math.min(remaining, estimate)
        residual[key] = remaining - assigned[key]!
      }
      if (group === 'analysis') analysisShares.set(id, assigned)
      else groups.set(id, [...(groups.get(id) ?? []), assigned])
    }
    if (group === 'analysis') {
      // 同进程的模型没有独立进程计数器；常驻与提交余量按已加载模型均分，显存只归属报告 CUDA 的模型。
      // 这是可追溯的共享成本归属，避免为共享运行环境额外创建一张模型卡。
      // id（模型卡标识）与 status（该模型的运行状态及进程身份）。
      const analysisCandidates = [
        { id: 'emotion', status: compute?.models?.emotion },
        { id: 'smart_turn', status: compute?.models?.smart_turn },
        { id: 'acoustic_events', status: compute?.auxiliary?.acoustic_events },
      ] as const
      const matchesProcess = (status: typeof analysisCandidates[number]['status'], process: typeof row) => {
        if (!status || status.service === 'DISABLED') return false
        const footprint = status.resource_footprint
        const sample = status.resource_status
        return footprint?.pid === process.pid && footprint.created_at === process.created_at
          || sample?.pid === process.pid && sample.created_at === process.created_at
      }
      const loaded = analysisCandidates.filter(candidate => matchesProcess(candidate.status, row))
      const addShare = (id: string, key: keyof ComponentResources, amount: number) => {
        const share = analysisShares.get(id) ?? {
          memory_rss_bytes: typeof row.memory_rss_bytes === 'number' ? 0 : null,
          ...(typeof row.memory_private_commit_bytes === 'number' ? { memory_private_commit_bytes: 0 } : {}),
        }
        share[key] = typeof share[key] === 'number' ? share[key]! + amount : amount
        analysisShares.set(id, share)
      }
      const distribute = (key: keyof ComponentResources, owners: readonly { id: string }[]) => {
        const value = residual[key]
        if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0 || !owners.length) return
        const each = Math.floor(value / owners.length)
        const remainder = value - each * owners.length
        owners.forEach((owner, index) => addShare(owner.id, key, each + (index < remainder ? 1 : 0)))
        residual[key] = 0
      }
      distribute('memory_rss_bytes', loaded)
      distribute('memory_private_commit_bytes', loaded)
      const gpuOwners = loaded.filter(candidate => candidate.status?.device?.startsWith('cuda'))
      distribute('gpu_dedicated_bytes', gpuOwners)
      for (const [id, share] of analysisShares) groups.set(id, [...(groups.get(id) ?? []), share])
      // 尚未匹配模型身份或设备信息的已知余量暂计入语音服务，采样同步后自动转归模型。
      // 保留缺失指标的未知状态，不把剩余显存错误分配给使用 CPU（中央处理器）的模型。
      if (!loaded.length || (['memory_rss_bytes', 'memory_private_commit_bytes', 'gpu_dedicated_bytes'] as const)
        .some(key => residual[key] !== 0)) {
        groups.set('voice-system', [...(groups.get('voice-system') ?? []), residual])
      }
    } else {
      groups.set(group, [...(groups.get(group) ?? []), residual])
    }
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

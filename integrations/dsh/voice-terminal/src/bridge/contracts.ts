/** Bridge state（桥接状态）：每个 Voice 会话独立持有上下文、激活和幂等水位。 */
import type { AgentHandle } from '@deepseek-ai/dsh-agent'
import type { Volatile } from '@deepseek-ai/cordis'
import type { Json } from '../shared/protocol.js'
import type { CallbackScope } from '../shared/callback.js'
export interface Config {
  /** HTTP route prefix（HTTP 路由前缀）. */
  readonly routePrefix: string
  /** Maximum request body size（请求体最大字节数）. */
  readonly maxBodyBytes: number
  /** Selected workspace id（所选工作区标识）. */
  readonly workspaceId: Volatile<string>
  /** Project fallback workspace id（项目备用工作区标识）. */
  readonly defaultWorkspaceId: Volatile<string>
  /** Model provider id（模型供应方标识）. */
  readonly provider: Volatile<string>
  /** Model id（模型标识）. */
  readonly model: Volatile<string>
  /** Reasoning effort id（推理强度标识）. */
  readonly reasoningEffort: Volatile<string>
  /** Settings fallback notice（设置回退提示）. */
  readonly fallbackNotice: Volatile<string>
}

export interface StoredRequest { digest: string; response: Json }
export interface ObserverRecord { trace_revision: number; type: string; data: Json }
export interface PendingContext {
  messageId: string
  update: 'snapshot' | 'delta'
  fromRevision: number
  revision: number
  frames: Json[]
  /** 下一回合激活标识；存在时须在合并后保留激活正文。 */
  activationId?: string | undefined
  /** 门禁暂缓的最晚版本；用于在下一回合标明来源。 */
  deferredThrough?: number | undefined
}
export interface PendingReminder { messageId: string }
export interface Binding {
  voiceSessionId: string
  dshSessionId: string
  epoch: number
  revision: number
  state: 'active' | 'retired'
  activationId?: string | undefined
  protocolRecoveryAttempted: boolean
  protocolRecoveryPending: boolean
  contextDigest?: string | undefined
  handle: AgentHandle | undefined
  /** Callback scope（回调生命周期）：组件卸载时取消后台网络任务。 */
  callbacks?: CallbackScope | undefined
  requests: Map<string, StoredRequest>
  records: ObserverRecord[]
  traceRevision: number
  agentState: string
  committedSteps: number
  pendingTools: Map<string, string>
  pendingContext?: PendingContext | undefined
  /** 排队消息标识到上下文版本的映射，用于记录实际领取水位。 */
  queuedMessages?: Map<string, number> | undefined
  /** 已同步但尚未排入模型消息的完整区间。 */
  heldContext?: PendingContext | undefined
  /** 此绑定是否实际运行过侧载门禁。 */
  gateObserved?: boolean | undefined
  /** 已排队引导的最新版本。 */
  queuedRevision?: number | undefined
  /** DSH 已领取的最新版本。 */
  claimedRevision?: number | undefined
  /** 本激活已暂缓的最新版本。 */
  deferredRevision?: number | undefined
  reminderIntervalMs: number
  activationStartedAt?: number | undefined
  nextReminderMs: number
  reminderTimer?: ReturnType<typeof setTimeout> | undefined
  pendingReminder?: PendingReminder | undefined
  /** exportSnapshot（完整归档快照）：分页期间保持稳定，不受后续事件影响。 */
  exportSnapshot?: { id: string; records: Json[] } | undefined
}

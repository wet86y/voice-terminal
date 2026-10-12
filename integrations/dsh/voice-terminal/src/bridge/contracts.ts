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

/**
 * RuntimeActivity（实时活动）：投影当前运行中可展开的思考、工具详情、作业输出及授权请求。
 * stream（活动流）、phase（阶段）与 tool_status（工具状态）共同描述条目生命周期；正文经凭据脱敏。
 */
export type RuntimeActivity =
  | { stream: 'thinking'; phase: 'delta'; /** item_id（思考块标识）。 */ item_id: string; /** delta（思考文本增量）：保留空白，最多 2000 字符。 */ delta: string }
  | { stream: 'thinking'; phase: 'completed'; /** item_id（已结束思考块标识）。 */ item_id: string }
  | { stream: 'tool' | 'output'; phase: 'started' | 'delta' | 'completed' | 'failed'; /** item_id（调用或作业标识）。 */ item_id: string; /** tool_name（工具或作业名称）。 */ tool_name: string; /** tool_status（执行状态）。 */ tool_status: 'running' | 'completed' | 'failed'; /** text（参数、结果或作业详情）。 */ text?: string; /** delta（输出增量）。 */ delta?: string }
  | { stream: 'approval'; phase: 'started' | 'completed' | 'failed'; /** item_id（权限卡片标识）。 */ item_id: string; /** approval_id（待决权限标识）。 */ approval_id: string; /** tool_name（待授权工具）。 */ tool_name: string; /** text（请求内容）。 */ text: string }

export interface Binding {
  /** toolArguments（参数缓存）：仅用于权限卡片显示，不改变工具行为。 */
  toolArguments?: Map<string, string>
  /** pendingApprovals（待决权限）：取消或切换运行时必须结算。 */
  pendingApprovals?: Map<string, { activationId: string; settle: (outcome: 'allowed-once' | 'rejected' | 'cancelled') => void }>
  voiceSessionId: string
  dshSessionId: string
  epoch: number
  revision: number
  state: 'active' | 'retired'
  activationId?: string | undefined
  /** activationOrigin（激活来源）：区分语音请求与已领取的定时任务。 */
  activationOrigin?: 'voice' | 'schedule' | undefined
  /** activationReady（定时激活确认）：控制工具必须先取得可靠的启动回执。 */
  activationReady?: Promise<Record<string, Json>> | undefined
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
  /** action_seq（动作序号）：当前根 Agent 激活内实时动作的递增序号。 */
  actionSeq: number
  /** actionQueue（动作回调队列）：按发生顺序发送实时动作回调。 */
  actionQueue: Promise<void>
  /** assistantStreamRevision（实时流修订水位）：在根 Agent 生命周期内递增，拒收旧帧。 */
  assistantStreamRevision: number
  /** currentThinkingItemId（当前思考项）：将同一思考块的增量合并。 */
  currentThinkingItemId?: string | undefined
  /** currentThinkingAttemptId（当前推理尝试）：用于结束当前思考项。 */
  currentThinkingAttemptId?: string | undefined
  /** currentThinkingActivationId（当前思考激活）：迟到刷新仍绑定原运行。 */
  currentThinkingActivationId?: string | undefined
  /** pendingThinkingDelta（待发送思考增量）：短暂合并高频流片段。 */
  pendingThinkingDelta: string
  /** thinkingTimer（思考刷新定时器）：合并短时间内连续到达的片段。 */
  thinkingTimer?: ReturnType<typeof setTimeout> | undefined
  /** terminalActivationId（已结束激活标识）：接收终态工具的最终结果。 */
  terminalActivationId?: string | undefined
  /** pendingToolActivations（工具激活映射）：工具结果仍绑定工具开始时的运行。 */
  pendingToolActivations: Map<string, string>
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

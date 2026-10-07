import type { Context } from '@deepseek-ai/cordis'
import type {
  ContextMessageNode, ConversationNodeDefinition,
} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { ChatConversationViewNode } from '@deepseek-ai/dsh-client-ui-chat/client'

const BRIDGE_PLUGIN_NAME = 'voice-terminal-shadow-bridge'
const BRIDGE_SOURCE_KIND = `plugin:${BRIDGE_PLUGIN_NAME}`

/** Exact content and source recorded for one Voice Terminal injection（语音插件注入内容和来源）. */
export interface VoiceTerminalContextNodeData {
  /** Durable session event sequence（持久会话事件序号）. */
  readonly seq: number
  /** Durable session event time（持久会话事件时间）. */
  readonly time: number
  /** Exact blocks sent to the model（实际注入模型的完整内容块）. */
  readonly content: ContextMessageNode['content']
  /** Original message source metadata（原始消息来源信息）. */
  readonly source: unknown
}

interface VoiceTerminalContextState {
  /** Immutable payload rendered in the transcript（对话页呈现的完整载荷）. */
  readonly data: VoiceTerminalContextNodeData
  /** Whether DSH already renders this input as a wake notice（DSH 是否已将该消息呈现为唤醒提示）. */
  readonly isWakeNotice: boolean
}

/** Small structural view of the DSH inbox claim state（DSH 收件箱认领状态）. */
interface InboxClaimState {
  /** IDs currently claimed by the Agent inbox（Agent 收件箱当前认领的消息标识）. */
  readonly currentClaimed: ReadonlySet<string>
  /** Sequence of the latest inbox claim（最近一次收件箱认领的事件序号）. */
  readonly claimSeq: number
  /** Whether the latest claim includes human input（最近一次认领是否包含用户输入）. */
  readonly claimedHuman: boolean
}

declare module '@deepseek-ai/dsh-client-ui-chat/client' {
  interface ChatNodeDataMap {
    /** Voice Terminal injected context, rendered separately from human input（语音插件注入内容）. */
    'voice-terminal-context': VoiceTerminalContextNodeData
  }
}

function sourceRecord(source: unknown): Record<string, unknown> | null {
  return typeof source === 'object' && source !== null && !Array.isArray(source)
    ? source as Record<string, unknown>
    : null
}

function isVoiceTerminalSource(source: unknown): boolean {
  const record = sourceRecord(source)
  if (record === null) return false
  return record.kind === BRIDGE_SOURCE_KIND
    || (record.kind === 'plugin' && record.plugin === BRIDGE_PLUGIN_NAME)
}

/** Project non-waking bridge messages into a visible Chat node（将未触发新轮次的桥接消息呈现为可见节点）. */
export const voiceTerminalContextDefinition: ConversationNodeDefinition<VoiceTerminalContextState> = {
  kind: 'voice-terminal-bridge-context',
  target: 'chat',
  match: event => {
    if (event.type !== 'user/message' || !isVoiceTerminalSource(event.data.source)) return null
    return { id: String(event.data.id), role: 'start' }
  },
  start: (_context, match, reader) => {
    const event = match.event
    if (event.type !== 'user/message') throw new Error('Voice Terminal context requires user/message')
    const id = String(event.data.id)
    const nextTurn = reader.previous<InboxClaimState>('inbox-next-turn')?.state
    const nextStep = reader.previous<InboxClaimState>('inbox-next-step')?.state
    const location = match.location
    const turnStart = location.kind === 'step' ? location.turn.start?.seq : undefined
    const isIdleSteer = location.kind === 'step' && location.step.step === 1
      && turnStart !== undefined
      && (nextStep?.claimSeq ?? -1) > turnStart
      && (nextTurn?.claimSeq ?? -1) < turnStart
      && nextStep?.claimedHuman === false
      && nextStep.currentClaimed.has(id)
    const isWakeNotice = nextTurn?.currentClaimed.has(id) === true || isIdleSteer
    return {
      data: {
        seq: event.seq,
        time: event.time,
        content: event.data.content,
        source: event.data.source,
      },
      isWakeNotice,
    }
  },
  update: context => context.state,
  buildViewNode: (context): ChatConversationViewNode | null => {
    const state = context.state
    const start = context.start
    if (state === undefined || state.isWakeNotice || start === undefined) return null
    const location = start.location
    return {
      key: context.key,
      kind: 'voice-terminal-context',
      id: context.id,
      target: 'chat',
      anchorSeq: state.data.seq,
      location,
      visibility: 'visible',
      data: state.data,
    }
  },
}

/** Register the bridge-context projection for this plugin lifetime（注册桥接上下文投影）. */
export function registerVoiceTerminalContextNode(ctx: Context): () => void {
  return ctx.uiConversation.events.register(voiceTerminalContextDefinition)
}

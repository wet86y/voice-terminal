import type { UserMessage } from '@deepseek-ai/dsh-llm'
import { MessageId } from '@deepseek-ai/dsh-llm'
import type { ContextFormed } from '@deepseek-ai/dsh-llm'
declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    /** Bridge source（桥接来源）：与历史消息标识一致。 */
    'plugin:voice-terminal-shadow-bridge': { kind: 'plugin:voice-terminal-shadow-bridge' }
    /** Schedule source（任务来源）：公开调度日志的来源类型。 */
    schedule: { kind: 'schedule' } & ContextFormed
  }
}
/** Durable input construction（持久输入构造）：仅构造消息和工具幂等标识。 */
import { randomUUID } from 'node:crypto'
import { digest } from '../shared/protocol.js'
import { BRIDGE_NAME as name } from '../shared/identity.js'
export function createUserMessage(input: Pick<UserMessage, 'content' | 'source'>, id: string = randomUUID()): UserMessage {
  return Object.freeze({ ...input, id: MessageId(id), role: 'user' })
}

export function createBridgeUserMessage(content: UserMessage['content'], id?: string): UserMessage {
  return createUserMessage({ content, source: { kind: `plugin:${name}` } }, id)
}

/** Stable idempotency identity（稳定幂等标识） for one DSH tool side effect. */
export function stableBridgeToolId(scope: 'request' | 'event' | 'early-silence', dshSessionId: string, callId: unknown): string {
  return digest({ scope: `shadow-tool-${scope}`, dsh_session_id: dshSessionId, call_id: String(callId) })
}

/** Clear only the activation that initiated an asynchronous terminal callback（仅清理发起异步终态回调的激活）. */
export function clearActivationIfCurrent(binding: { activationId?: string | undefined; terminalActivationId?: string | undefined }, activationId: string): boolean {
  if (binding.activationId !== activationId) return false
  binding.terminalActivationId = activationId
  delete binding.activationId
  return true
}

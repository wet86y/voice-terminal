/** Task delivery（定时任务投递）：从官方持久事件恢复提交身份，不复制消息正文。 */
import { z } from 'zod'
import type { ProjectionDefinition } from '@deepseek-ai/dsh-session-projection'
import { digest } from '../shared/protocol.js'

type TaskDeliveries = Record<string, string>

declare module '@deepseek-ai/dsh-session-projection/types' {
  interface SessionProjectionStateMap {
    /** voiceTaskDeliveries（已提交任务身份）：触发编号到正文摘要，可从既有事件重建。 */
    voiceTaskDeliveries: TaskDeliveries
  }
}

export const taskDeliveryProjection = {
  key: 'voiceTaskDeliveries', stateVersion: 1,
  stateSchema: z.record(z.string(), z.string()),
  init: (): TaskDeliveries => ({}),
  apply(state, event) {
    const messages = event.type === 'agent/inbox/spliced' ? event.data.inserted
      : event.type === 'user/message' ? [event.data] : []
    let next = state
    for (const message of messages) {
      if (message.source?.kind !== 'schedule' || Object.hasOwn(next, message.id)) continue
      // 收件箱插入已经提交副作用，即使稍后取消，也不能通过重投恢复已取消任务。
      if (next === state) next = { ...state }
      Object.defineProperty(next, message.id, {
        value: digest(message.content), enumerable: true, configurable: true, writable: true,
      })
    }
    return next
  },
} satisfies ProjectionDefinition<'voiceTaskDeliveries', TaskDeliveries>

/** Image history（图片历史）：从持久事件折叠接收身份，普通整理不会抹掉同步水位。 */
import { z } from 'zod'
import type { ProjectionDefinition } from '@deepseek-ai/dsh-session-projection'
import type { Context } from '../shared/services.js'
import { record } from '../shared/json-record.js'
import type { Binding } from './contracts.js'

type VoiceContextState = Record<string, Record<string, string>>
declare module '@deepseek-ai/dsh-session-projection/types' {
  interface SessionProjectionStateMap {
    /** voiceContextFrames（语音共享历史身份）：会话及代次到消息摘要的持久索引。 */
    voiceContextFrames: VoiceContextState
  }
}

export const voiceContextProjection = {
  key: 'voiceContextFrames', stateVersion: 1,
  stateSchema: z.record(z.string(), z.record(z.string(), z.string())),
  init: (): VoiceContextState => ({}),
  apply(state, event) {
    const marker = event.type === 'user/message' ? record(record(event.data.source).voice_context)
      : event.type === 'tool/result' && !event.data.message.isError ? record(record(event.data.meta).voice_context) : {}
    if (typeof marker.voice_session_id !== 'string' || typeof marker.epoch !== 'number') return state
    const scope = `${marker.voice_session_id}:${marker.epoch}`
    const prior = state[scope] ?? {}, next = { ...prior }
    for (const [key, hash] of Object.entries(record(marker.frames))) {
      if (typeof hash === 'string') next[key] = hash
    }
    if (Object.keys(next).every(key => next[key] === prior[key])) return state
    return { ...state, [scope]: next }
  },
} satisfies ProjectionDefinition<'voiceContextFrames', VoiceContextState>

export function registerVoiceContextProjection(ctx: Context): void {
  ctx.sessionProjections.register(voiceContextProjection)
}

export function restoreVoiceContextFrames(ctx: Context, binding: Binding): Map<string, string> {
  const session = binding.handle!.agent.session
  const state = ctx.sessionProjections.stateOf(session, 'voiceContextFrames')
  return new Map(Object.entries(state?.[`${binding.voiceSessionId}:${binding.epoch}`] ?? {}))
}

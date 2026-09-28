import { createHash } from 'node:crypto'

/** Stable event identity（稳定事件标识） derived from an existing DSH tool call. */
export function stableVoiceEventId(sessionId: string, callId: unknown): string {
  return createHash('sha256')
    .update(`voice-event\0${sessionId}\0${String(callId)}`)
    .digest('hex')
}

import type { ConversationEntry, ConversationSnapshotPayload } from './types'

/**
 * Client state of the server-ordered conversation log.
 *
 * The rail is a chat surface whose order must not be inferred on the client.
 * The server assigns one monotonic ``seq`` per conversation line (a user turn,
 * a settled assistant speech, a system-owned check such as the KWS wake
 * acknowledgement), so this store only has to apply snapshots, upsert single
 * entries and keep the order stable.
 *
 * Two rules make the order stable for the life of a page:
 * - An entry keeps the position of its identity.  Re-delivering the same
 *   ``turn_id`` / ``speech_id`` refreshes its text and status without moving it
 *   (the server publishes the same ``seq`` for such a refresh).
 * - A snapshot replaces the whole view.  It is the reconnect/reset authority,
 *   so nothing is ever merged across a session boundary.
 */
export class ConversationLog {
  #byKey = new Map<string, ConversationEntry>()
  #head = 0

  /** Server-ordered entries, oldest first. */
  entries(): ConversationEntry[] {
    return [...this.#byKey.values()].sort((left, right) => left.seq - right.seq)
  }

  latestSeq(): number {
    return this.#head
  }

  size(): number {
    return this.#byKey.size
  }

  /** Identity of one conversation line; mirrors the server's key. */
  static keyOf(entry: Pick<ConversationEntry, 'kind' | 'conversation_id' | 'turn_id' | 'speech_id'>): string {
    return entry.kind === 'user_turn'
      ? `turn\u0000${entry.conversation_id}\u0000${entry.turn_id}`
      : `speech\u0000${entry.conversation_id}\u0000${entry.speech_id}`
  }

  /** Replace the whole view from an authoritative snapshot. */
  applySnapshot(payload: ConversationSnapshotPayload | null | undefined): void {
    if (!payload || !Array.isArray(payload.entries)) return
    this.#byKey = new Map()
    let highest = 0
    for (const entry of payload.entries) {
      if (!isUsable(entry)) continue
      this.#byKey.set(ConversationLog.keyOf(entry), entry)
      highest = Math.max(highest, entry.seq)
    }
    this.#head = Math.max(highest, numberOr(payload.latest_seq, 0))
  }

  /**
   * Apply one incremental line.
   *
   * Unknown identities are inserted at their ``seq``; known ones are refreshed
   * in place, which is what lets a later settlement of an already visible turn
   * correct its status without reordering the rail.
   */
  applyEntry(entry: ConversationEntry | null | undefined): boolean {
    if (!isUsable(entry)) return false
    const key = ConversationLog.keyOf(entry)
    const existing = this.#byKey.get(key)
    if (existing) {
      const next = { ...existing, ...entry, seq: existing.seq }
      this.#byKey.set(key, next)
      this.#head = Math.max(this.#head, next.seq)
      return true
    }
    this.#byKey.set(key, entry)
    this.#head = Math.max(this.#head, entry.seq)
    return true
  }

  /** Whether the current turn (or speech) is already represented in the log. */
  hasTurn(turnId: string): boolean {
    if (!turnId) return false
    for (const entry of this.#byKey.values()) {
      if (entry.turn_id === turnId) return true
    }
    return false
  }

  hasSpeech(conversationId: string, speechId: string): boolean {
    if (!speechId) return false
    for (const entry of this.#byKey.values()) {
      if (entry.speech_id === speechId && entry.conversation_id === conversationId) return true
    }
    return false
  }

  clear(): void {
    this.#byKey = new Map()
    this.#head = 0
  }
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/** A usable line carries a numeric order and an identifying kind. */
function isUsable(value: unknown): value is ConversationEntry {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<ConversationEntry>
  if (candidate.kind !== 'user_turn' && candidate.kind !== 'assistant_speech' && candidate.kind !== 'system_speech') {
    return false
  }
  return typeof candidate.seq === 'number' && Number.isFinite(candidate.seq)
}

/**
 * Read one ``conversation.append`` payload.
 *
 * The envelope restores ``turn_id`` when the payload carried it only as the
 * message identity, so both shapes are accepted.
 */
export function readConversationEntry(payload: unknown, envelopeTurnId?: string | null): ConversationEntry | null {
  if (!payload || typeof payload !== 'object') return null
  const value = payload as Record<string, unknown>
  const kind = value.kind
  if (kind !== 'user_turn' && kind !== 'assistant_speech' && kind !== 'system_speech') return null
  const seq = typeof value.seq === 'number' && Number.isFinite(value.seq) ? value.seq : null
  if (seq === null) return null
  return {
    seq,
    kind,
    conversation_id: readString(value, 'conversation_id') ?? '',
    turn_id: readString(value, 'turn_id') ?? readString({ turn_id: envelopeTurnId }, 'turn_id') ?? '',
    speech_id: readString(value, 'speech_id') ?? '',
    text: readString(value, 'text') ?? '',
    status: readString(value, 'status') ?? '',
    delivery: readDelivery(value.delivery),
    termination: readString(value, 'termination') ?? '',
    at: readString(value, 'at') ?? '',
  }
}

function readString(value: Record<string, unknown>, key: string): string | null {
  const candidate = value[key]
  return typeof candidate === 'string' ? candidate : null
}

function readDelivery(value: unknown): { status?: string; termination?: string; heard_text?: string } {
  if (!value || typeof value !== 'object') return {}
  const source = value as Record<string, unknown>
  const delivery: { status?: string; termination?: string; heard_text?: string } = {}
  if (typeof source.status === 'string') delivery.status = source.status
  if (typeof source.termination === 'string') delivery.termination = source.termination
  if (typeof source.heard_text === 'string') delivery.heard_text = source.heard_text
  return delivery
}

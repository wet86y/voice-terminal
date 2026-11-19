/**
 * Observation cache and the staleness boundary. Every observation stores the
 * remote window's identity facts plus content hashes; before any mutating
 * action the executor re-observes the window over SSH and this store
 * compares. Identity, tree, or pixel drift — or an expired observation —
 * refuses the action, so the model must re-observe instead of acting on a
 * remote screen that changed since it last looked.
 *
 * 派生自 pureexe/dsh-windows-remote-ssh，固定提交 b640f268c1e1ee1134e6fa5b51f15736b2bab26b。
 * 修改：以设备和代次替代 SSH（安全外壳协议）地址，观察标识按实例隔离。
 * 许可证见 licenses/dsh-windows-remote-ssh-MIT.txt（上游许可）。
 */

import { createHash, randomUUID } from 'node:crypto'
import type { DeviceTarget, ObservationConfig, Rect, WindowSnapshot } from './types.js'

/** One cached observation, immutable after construction. */
export interface ObservationRecord {
  id: string
  windowId: number
  processId: number
  executablePath: string | null
  title: string
  className: string
  rect: Rect
  treeHash: string
  shotHash: string
  elementCount: number
  /** Wall-clock capture time in milliseconds. */
  observedAt: number
  limits: Readonly<{ maxElements: number; maxDepth: number }> // limits（原观察的元素数量和深度上限）。
  /**
   * The SSH target this observation came from. A later action citing this
   * observation replays against this exact target — never whatever target
   * that action's own call happens to resolve to — so a conversation that
   * addresses more than one remote host can never have an action for host A
   * silently misapplied against host B.
   */
  target: DeviceTarget // target（观察所属设备及连接、执行器代次）。
}

/** The staleness verdict: ok, or a stable refusal code plus a model-readable detail. */
export type FreshnessVerdict =
  | { ok: true }
  | { ok: false; code: 'UNKNOWN_OBSERVATION' | 'STALE_DEVICE' | 'EXPIRED' | 'STALE_IDENTITY' | 'STALE_TREE' | 'STALE_PIXELS'; detail: string }

/**
 * Hash the observable state and a unique nonce into an observation id.
 *
 * @param snapshot - the snapshot facts.
 * @returns a hex observation id.
 */
export function observationIdOf(snapshot: WindowSnapshot, target: DeviceTarget): string {
  const material = JSON.stringify([
    target.deviceId,
    target.connectionGeneration,
    target.executorGeneration,
    randomUUID(),
    snapshot.windowId,
    snapshot.processId,
    snapshot.executablePath,
    snapshot.title,
    snapshot.className,
    snapshot.rect,
    snapshot.treeHash,
    snapshot.shotHash,
  ])
  return createHash('sha256').update(material).digest('hex').slice(0, 32)
}

/** Bounded LRU cache of observations, plus the staleness verdict logic. */
export class ObservationStore {
  private readonly entries = new Map<string, ObservationRecord>()

  constructor(private readonly config: ObservationConfig) {
    if (!Number.isSafeInteger(config.maxCachedObservations) || config.maxCachedObservations < 1
      || !Number.isSafeInteger(config.maxObservationAgeMs) || config.maxObservationAgeMs < 1) {
      throw new Error('观察缓存数量与有效期必须为正整数。')
    }
  }

  /**
   * Cache one observation and return it.
   *
   * @param snapshot - the snapshot facts it came from.
   * @param target - the SSH target it was captured from.
   * @returns the stored record (id = hash of the state).
   */
  record(snapshot: WindowSnapshot, target: DeviceTarget,
    limits = { maxElements: 100, maxDepth: 16 }): ObservationRecord {
    if (!target.deviceId || target.deviceId.length > 128 || target.deviceId !== target.deviceId.trim()
      || !Number.isSafeInteger(target.connectionGeneration) || target.connectionGeneration < 1
      || !Number.isSafeInteger(target.executorGeneration) || target.executorGeneration < 1) {
      throw new Error('观察设备或代次无效。')
    }
    const record: ObservationRecord = {
      id: observationIdOf(snapshot, target),
      windowId: snapshot.windowId,
      processId: snapshot.processId,
      executablePath: snapshot.executablePath,
      title: snapshot.title,
      className: snapshot.className,
      rect: Object.freeze({ ...snapshot.rect }),
      treeHash: snapshot.treeHash,
      shotHash: snapshot.shotHash,
      elementCount: snapshot.elementCount,
      observedAt: Date.now(),
      limits: Object.freeze({ ...limits }),
      target: Object.freeze({ ...target }),
    }
    Object.freeze(record)
    this.entries.delete(record.id)
    this.entries.set(record.id, record)
    while (this.entries.size > this.config.maxCachedObservations) {
      const oldest = this.entries.keys().next().value
      if (oldest === undefined) break
      this.entries.delete(oldest)
    }
    return record
  }

  /**
   * Look one observation up by id.
   *
   * @param id - the observation id an action cites.
   * @returns the record, or undefined when unknown or evicted.
   */
  get(id: string): ObservationRecord | undefined {
    return this.entries.get(id)
  }

  /**
   * Compare a fresh re-observation against the cited record. The verdict is
   * `ok` only when identity (window/pid/exe/title/class/rect), the tree hash
   * (when `checkTree` is true and `staleCheckTree` is on), and the pixel hash
   * (when `staleCheckPixels` is on and the window doesn't match
   * `staleCheckPixelsExemptMatchers`) all still match, and the record is not
   * older than `maxObservationAgeMs`.
   *
   * @param record - the observation the action cites.
   * @param fresh - the re-observation captured immediately before the action.
   * @param now - wall-clock now (injectable for tests).
   * @param checkTree - checkTree（是否比较界面树摘要）。
   * @param checkPixels - checkPixels（是否比较窗口画面摘要）。
   * 窗口动作只需复核窗口，元素动作由辅助进程重新定位明确的 UIA RuntimeId（界面元素运行时编号）。
   * 坐标和按键动作依赖窗口内容，因此仍比较树及画面。
   * @returns the verdict.
   */
  verify(record: ObservationRecord, fresh: WindowSnapshot, target: DeviceTarget,
    now: number = Date.now(), checkTree = true, checkPixels = true): FreshnessVerdict {
    if (this.entries.get(record.id) !== record) {
      return { ok: false, code: 'UNKNOWN_OBSERVATION', detail: '观察不存在或已经被替换。' }
    }
    if (record.target.deviceId !== target.deviceId
      || record.target.connectionGeneration !== target.connectionGeneration
      || record.target.executorGeneration !== target.executorGeneration) {
      return { ok: false, code: 'STALE_DEVICE', detail: '观察所属设备或代次已变化。' }
    }
    if (now < record.observedAt || now - record.observedAt > this.config.maxObservationAgeMs) {
      return {
        ok: false,
        code: 'EXPIRED',
        detail: `observation ${record.id} is older than ${this.config.maxObservationAgeMs} ms`,
      }
    }
    const identityChanged = fresh.windowId !== record.windowId
      || fresh.processId !== record.processId
      || fresh.executablePath !== record.executablePath
      || fresh.title !== record.title
      || fresh.className !== record.className
      || fresh.rect.x !== record.rect.x || fresh.rect.y !== record.rect.y
      || fresh.rect.width !== record.rect.width || fresh.rect.height !== record.rect.height
    if (identityChanged) {
      return {
        ok: false,
        code: 'STALE_IDENTITY',
        detail: `window ${record.windowId} no longer matches observation ${record.id} (title/class/process or rect changed)`,
      }
    }
    if (checkTree && this.config.staleCheckTree && fresh.treeHash !== record.treeHash) {
      return {
        ok: false,
        code: 'STALE_TREE',
        detail: `the accessibility tree of window ${record.windowId} changed since observation ${record.id}`,
      }
    }
    if (checkPixels && this.config.staleCheckPixels && fresh.shotHash !== record.shotHash) {
      const pixelCheckExempt = this.config.staleCheckPixelsExemptMatchers.some(matcher => {
        matcher.lastIndex = 0
        const titleMatches = matcher.test(fresh.title)
        matcher.lastIndex = 0
        return titleMatches || (fresh.executablePath !== null && matcher.test(fresh.executablePath))
      })
      if (!pixelCheckExempt) {
        return {
          ok: false,
          code: 'STALE_PIXELS',
          detail: `the pixels of window ${record.windowId} changed since observation ${record.id}`,
        }
      }
    }
    return { ok: true }
  }
}

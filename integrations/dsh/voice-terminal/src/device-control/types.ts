import type { Json } from '../shared/protocol.js'
import type { AttachmentStore } from '@deepseek-ai/dsh-attachment'

/** DeviceTarget（设备目标）：由已认证设备管理器提供，不含连接凭据。 */
export interface DeviceTarget {
  deviceId: string // deviceId（稳定设备编号）。
  connectionGeneration: number // connectionGeneration（连接代次）。
  executorGeneration: number // executorGeneration（执行器代次）。
}

/** Rect（窗口矩形）：屏幕坐标和尺寸。 */
export interface Rect { x: number; y: number; width: number; height: number }

/** WindowSnapshot（窗口快照）：动作前重新观察的身份和内容摘要。 */
export interface WindowSnapshot {
  windowId: number
  processId: number
  executablePath: string | null
  title: string
  className: string
  rect: Rect
  foreground: boolean
  treeHash: string
  shotHash: string
  elementCount: number
}

/** ObservationConfig（观察配置）：限定缓存容量和新鲜度检查。 */
export interface ObservationConfig {
  maxCachedObservations: number
  maxObservationAgeMs: number
  staleCheckTree: boolean
  staleCheckPixels: boolean
  staleCheckPixelsExemptMatchers: readonly RegExp[]
}

/** DeviceToolBinding（设备工具绑定）：只含桥接激活和根代理身份。 */
export interface DeviceToolBinding {
  state: 'active' | 'retired' // state（绑定是否仍活跃）。
  activationId?: string | undefined // activationId（当前语音任务激活标识）。
  voiceSessionId: string // voiceSessionId（语音会话编号）。
  dshSessionId: string // dshSessionId（DSH 会话编号）。
  epoch: number // epoch（会话世代）。
  revision: number // revision（权威上下文修订号）。
  activationReady?: Promise<unknown> | undefined // activationReady（激活受理屏障）。
  handle?: { agent?: { id?: string } } | undefined // handle（绑定的 DSH 根代理句柄）。
  attachments?: AttachmentStore | undefined // attachments（DSH 原生内容附件存储）。
}

/** DeviceToolCallback（设备回调）：沿用桥接拥有的认证、取消和清理路径。 */
export type DeviceToolCallback = (path: string, payload: Record<string, Json>, signal?: AbortSignal) => Promise<Record<string, Json>>

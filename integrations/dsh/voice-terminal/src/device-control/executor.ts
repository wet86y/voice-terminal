/**
 * DeviceActionExecutor（设备动作执行器）：延续上游观察复核、授权和进程校验边界。
 * 行为来源：pureexe/dsh-windows-remote-ssh 固定提交 b640f268c1e1ee1134e6fa5b51f15736b2bab26b 的 src/actions.ts。
 * 许可证见 licenses/dsh-windows-remote-ssh-MIT.txt（上游许可）。
 */
import { ObservationStore, type ObservationRecord } from './observe.js'
import type { DeviceTarget, WindowSnapshot } from './types.js'

/** WindowActionName（窗口动作名称）：受观察与授权约束的操作。 */
export type WindowActionName = 'click' | 'type' | 'scroll' | 'key' | 'move' | 'window_control' | 'invoke' | 'multi_action'

/** WindowAction（窗口动作）：由观察固定目标，不携带 SSH（安全外壳协议）凭据。 */
export interface WindowAction {
  requestId: string // requestId（幂等请求编号）。
  basedOn: string // basedOn（动作依据的观察编号）。
  deviceId: string // deviceId（请求的设备编号，必须与观察一致）。
  name: WindowActionName // name（动作名称）。
  arguments: Readonly<Record<string, unknown>> // arguments（动作参数）。
  checkTree: boolean // checkTree（是否复核整棵界面树）。
}

/** DeviceActionBackend（设备动作后端）：由统一设备路由提供。 */
export interface DeviceActionBackend {
  currentTarget(deviceId: string): DeviceTarget | undefined
  observe(record: ObservationRecord, signal: AbortSignal): Promise<WindowSnapshot>
  execute(action: WindowAction, observation: ObservationRecord, target: DeviceTarget, signal: AbortSignal): Promise<unknown>
}

/** DeviceActionApproval（设备动作授权）：由现有 DSH（代理执行宿主）审批链实现。 */
export interface DeviceActionApproval {
  approve(action: WindowAction, observation: ObservationRecord, signal: AbortSignal): Promise<boolean>
}

/** DeviceActionError（设备动作错误）：区分未执行拒绝与执行后未知。 */
export class DeviceActionError extends Error {
  constructor(readonly code: string, message: string) { super(message); this.name = 'DeviceActionError' }
}

const WINDOW_ACTIONS = new Set<WindowActionName>(['click', 'type', 'scroll', 'key', 'move', 'window_control', 'invoke', 'multi_action'])

function sameTarget(a: DeviceTarget | undefined, b: DeviceTarget): boolean {
  return a !== undefined && a.deviceId === b.deviceId && a.connectionGeneration === b.connectionGeneration
    && a.executorGeneration === b.executorGeneration
}

function immutableJson(value: unknown): unknown {
  if (value === null || typeof value !== 'object') return value
  for (const item of Object.values(value)) immutableJson(item)
  return Object.freeze(value)
}

function copyAction(action: WindowAction): WindowAction {
  if (!action || typeof action.requestId !== 'string' || !action.requestId || action.requestId.length > 128
    || typeof action.basedOn !== 'string' || !action.basedOn || typeof action.deviceId !== 'string' || !action.deviceId
    || !WINDOW_ACTIONS.has(action.name) || typeof action.checkTree !== 'boolean'
    || !action.arguments || typeof action.arguments !== 'object' || Array.isArray(action.arguments)) {
    throw new DeviceActionError('INVALID_ACTION', '设备动作参数无效。')
  }
  let encoded: string
  try {
    encoded = JSON.stringify(action, (_key, value: unknown) => {
      if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('invalid number')
      if (typeof value === 'undefined' || typeof value === 'function' || typeof value === 'symbol') throw new Error('invalid value')
      return value
    })
  } catch {
    throw new DeviceActionError('INVALID_ACTION', '设备动作不能编码为 JSON（结构化数据）。')
  }
  if (Buffer.byteLength(encoded, 'utf8') > 256 * 1024) throw new DeviceActionError('INVALID_ACTION', '设备动作参数过大。')
  return immutableJson(JSON.parse(encoded)) as WindowAction
}

/** DeviceActionExecutor（设备动作执行器）：失败时绝不自动重发。 */
export class DeviceActionExecutor {
  constructor(private readonly observations: ObservationStore, private readonly backend: DeviceActionBackend,
    private readonly approval: DeviceActionApproval) {}

  async execute(input: WindowAction, signal: AbortSignal): Promise<unknown> {
    const action = copyAction(input)
    if (signal.aborted) throw new DeviceActionError('CANCELLED', '设备动作已取消。')
    const record = this.observations.get(action.basedOn)
    if (!record) throw new DeviceActionError('UNKNOWN_OBSERVATION', '观察不存在或已经过期。')
    if (record.target.deviceId !== action.deviceId) throw new DeviceActionError('STALE_DEVICE', '观察不属于指定设备。')

    const verify = async () => {
      const target = this.backend.currentTarget(record.target.deviceId)
      if (!sameTarget(target, record.target)) throw new DeviceActionError('STALE_DEVICE', '设备连接或执行器已变化。')
      const fresh = await this.backend.observe(record, signal)
      if (!sameTarget(this.backend.currentTarget(record.target.deviceId), record.target)) {
        throw new DeviceActionError('STALE_DEVICE', '观察期间设备连接或执行器已变化。')
      }
      const verdict = this.observations.verify(record, fresh, record.target, Date.now(), action.checkTree)
      if (!verdict.ok) throw new DeviceActionError(verdict.code, verdict.detail)
    }
    await verify()
    if (!await this.approval.approve(action, record, signal)) throw new DeviceActionError('DENIED', '设备动作未获授权。')
    if (signal.aborted) throw new DeviceActionError('CANCELLED', '设备动作已取消。')
    // 审批可能耗时；等待期间窗口或设备可能已经变化，必须再次核对。
    await verify()
    let result: unknown
    try {
      result = await this.backend.execute(action, record, record.target, signal)
    } catch {
      throw new DeviceActionError('OUTCOME_UNKNOWN', '设备动作结果未知，请查询原请求。')
    }
    try {
      if (!sameTarget(this.backend.currentTarget(record.target.deviceId), record.target)) throw new Error('设备连接已变化')
      const after = await this.backend.observe(record, signal)
      if (!sameTarget(this.backend.currentTarget(record.target.deviceId), record.target)
        || after.windowId !== record.windowId || after.processId !== record.processId
        || after.executablePath !== record.executablePath) throw new Error('执行后进程身份无法确认')
    } catch {
      // 动作已经发出，观察发生变化不能伪装为动作未执行。
      throw new DeviceActionError('OUTCOME_UNKNOWN', '设备动作已发出，但执行后状态无法确认。')
    }
    return result
  }
}

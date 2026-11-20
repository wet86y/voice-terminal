import { isRecord } from '../record'
import type { CustomInstruction, SettingsSyncState } from './types'

/** readSettingsSync（读取服务端设置依据）：不接受客户端推测的修订。 */
export function readSettingsSync(value: unknown): SettingsSyncState | undefined {
  if (!isRecord(value) || typeof value.instance_id !== 'string' || !value.instance_id
    || !Number.isSafeInteger(value.revision) || Number(value.revision) < 0) return
  return { instance_id: value.instance_id, revision: Number(value.revision) }
}

const equal = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right)
const copy = <T>(value: T): T => value === undefined ? value : JSON.parse(JSON.stringify(value))

/** mergeSettingsDraft（更新未编辑字段）：本地草稿只保留输入，不成为保存依据。 */
export function mergeSettingsDraft<T>(draft: T, previous: unknown, incoming: T): T {
  if (equal(draft, previous)) return copy(incoming)
  if (isRecord(draft) && !Array.isArray(draft) && isRecord(previous) && !Array.isArray(previous)
    && isRecord(incoming) && !Array.isArray(incoming)) {
    return Object.fromEntries(Object.keys(draft).map(key => [key,
      mergeSettingsDraft(draft[key], previous[key], incoming[key])])) as T
  }
  return copy(draft)
}

/** mergeInstructionDrafts（按条目合并显示）：启用状态始终取服务端，未提交正文仍可编辑。 */
export function mergeInstructionDrafts(drafts: CustomInstruction[], previous: CustomInstruction[],
  incoming: CustomInstruction[], busy: string[]): CustomInstruction[] {
  const before = new Map(previous.map(item => [item.id, item]))
  const latest = new Map(incoming.map(item => [item.id, item]))
  const result = drafts.flatMap(draft => {
    const saved = before.get(draft.id), next = latest.get(draft.id)
    if (busy.includes(draft.id)) return [copy(draft)]
    if (!next) return saved && equal(draft, saved) ? [] : [copy(draft)]
    const merged = mergeSettingsDraft(draft, saved, next)
    merged.enabled = next.enabled
    return [merged]
  })
  for (const item of incoming) {
    if (!drafts.some(draft => draft.id === item.id) && !before.has(item.id)) result.push(copy(item))
  }
  return result
}

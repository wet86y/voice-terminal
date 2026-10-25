/** interval helpers（间隔换算）：页面以整分钟编辑，服务端以秒保存。 */
export type WakeTaskIntervalParts = {
  /** hours（小时数）：间隔中的整小时。 */
  hours: string
  /** minutes（分钟数）：整小时以外的分钟。 */
  minutes: string
}

/** Convert editor fields（编辑框字段）to a supported whole-minute interval in seconds（秒）. */
export function wakeIntervalSeconds(hours: unknown, minutes: unknown): number | null {
  const hourText = String(hours ?? '')
  const minuteText = String(minutes ?? '')
  if (!/^\d+$/u.test(hourText) || !/^\d+$/u.test(minuteText)) return null
  const hourValue = Number(hourText)
  const minuteValue = Number(minuteText)
  if (!Number.isSafeInteger(hourValue) || !Number.isSafeInteger(minuteValue)
      || hourValue < 0 || hourValue > 24 || minuteValue < 0 || minuteValue > 59
      || (hourValue === 24 && minuteValue !== 0)) return null
  const totalMinutes = hourValue * 60 + minuteValue
  if (totalMinutes < 1 || totalMinutes > 24 * 60) return null
  return totalMinutes * 60
}

/** Parse an existing interval（已有间隔）into the page's hour/minute controls. */
export function wakeIntervalParts(value: string | null | undefined): WakeTaskIntervalParts {
  if (!value) return { hours: '1', minutes: '0' }
  const match = /^(\d+)\s*([smhd])$/iu.exec(value.trim())
  if (!match) return { hours: '1', minutes: '0' }
  const amount = Number(match[1])
  const seconds = amount * ({ s: 1, m: 60, h: 3600, d: 86400 } as Record<string, number>)[match[2]!.toLowerCase()]!
  if (!Number.isSafeInteger(seconds) || seconds < 1) return { hours: '1', minutes: '0' }
  // Legacy second-granularity rules are rounded up so editing them does not make them fire sooner.
  const totalMinutes = Math.ceil(seconds / 60)
  return { hours: String(Math.floor(totalMinutes / 60)), minutes: String(totalMinutes % 60) }
}

/** Validate trigger_count（总触发次数）as a positive safe integer. */
export function validWakeTriggerCount(value: unknown): boolean {
  const text = String(value ?? '')
  if (!/^\d+$/u.test(text)) return false
  const count = Number(text)
  return Number.isSafeInteger(count) && count >= 1
}

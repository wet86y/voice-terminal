/** deviceDisplayName（设备显示名称）：旧客户端只上报身份编号时，显示稳定的基础随机名称。 */
export function deviceDisplayName(name: string | null | undefined, deviceId: string): string {
  const value = name?.trim()
  if (value && value !== deviceId) return value
  const suffix = Array.from(deviceId).slice(-6).join('')
  return suffix ? `Voice Terminal ${suffix}` : 'Voice Terminal'
}

/** DeviceNameEntry（连接列表名称）：编号只用于稳定区分条目，不参与用户重命名。 */
export interface DeviceNameEntry {
  id: string // id（真实且唯一的设备身份编号）。
  name: string // name（允许重复的基础显示名称）。
}

/** connectionDisplayNames（连接列表名称去重）：只派生显示后缀，不改写身份、配对资料或原始名称。 */
export function connectionDisplayNames(entries: DeviceNameEntry[]): Map<string, string> {
  const groups = new Map<string, string[]>()
  for (const entry of entries) {
    const name = deviceDisplayName(entry.name, entry.id)
    groups.set(name, [...(groups.get(name) || []), entry.id])
  }
  const labels = new Map<string, string>()
  const reserved = new Set(groups.keys()) // reserved（已存在的基础名称，避免后缀碰撞）。
  for (const [name, ids] of groups) {
    if (ids.length === 1) { labels.set(ids[0], name); continue }
    let suffix = 1
    for (const id of [...ids].sort()) {
      let label = `${name} ${suffix++}`
      while (reserved.has(label)) label = `${name} ${suffix++}`
      reserved.add(label)
      labels.set(id, label)
    }
  }
  return labels
}

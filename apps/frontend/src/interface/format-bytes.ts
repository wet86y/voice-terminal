/** formatBytes（容量显示）：按大小切换单位，省略无意义的小数末尾零。 */
export function formatBytes(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  const unit = value >= 1024 ** 3 ? 3 : value >= 1024 ** 2 ? 2 : value >= 1024 ? 1 : 0
  return `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: unit ? 2 : 0, useGrouping: false }).format(value / 1024 ** unit)} ${['B', 'KB', 'MB', 'GB'][unit]}`
}

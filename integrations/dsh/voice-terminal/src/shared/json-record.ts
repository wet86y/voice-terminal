/** Historical JSON record（历史结构化记录）：读取旧版和新版日志的可选字段。 */
export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

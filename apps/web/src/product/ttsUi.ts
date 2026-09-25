/** Worker（工作进程）状态在设置页显示的中文文案。 */
export function workerStatusLabel(
  worker: { state?: 'stopped' | 'starting' | 'ready' | 'draining' | 'error' } | null | undefined,
  switching: boolean,
): string {
  if (switching) return '切换中'
  switch (worker?.state) {
    case 'starting': return '启动中'
    case 'ready': return '已就绪'
    case 'draining': return '排空中'
    case 'error': return '失败'
    default: return '已停止'
  }
}

import type { ApplicationSnapshot } from '../product/application-host'

const COMPONENT_NAMES: Record<string, string> = { // COMPONENT_NAMES（加载画面使用的简短组件名称）。
  host: '应用入口', web: '界面服务', setup: '运行环境', asr: '语音识别',
  analysis: '语音分析', voice: '语音服务', speaker: '声纹服务', dsh: '代理服务', desktop: '桌面窗口',
}

/** startupComponents（当前启动中的组件名称）：不把进程、阶段等诊断细节放到主页。 */
export function startupComponents(snapshot: ApplicationSnapshot | null): string | null {
  if (!snapshot) return '本地组件'
  if (snapshot.stopping || snapshot.state === 'stopped' || snapshot.startup_complete) return null
  const active = snapshot.components.filter(row => row.state === 'starting' || row.retrying) // active（当前正在启动或重试的组件）。
  if (active.length) return active.map(row => COMPONENT_NAMES[row.id] || row.name).join('、')
  const waiting = snapshot.components.find(row => row.state === 'waiting') // waiting（当前排在最前的待启动组件）。
  if (waiting) return COMPONENT_NAMES[waiting.id] || waiting.name
  return '本地组件'
}

/** startupActivity（当前启动内容）：同时呈现并行启动的组件，不显示诊断清单。 */
export function startupActivity(snapshot: ApplicationSnapshot | null): string {
  if (!snapshot) return '正在连接应用'
  if (snapshot.stopping) return '正在停止本地服务'
  if (snapshot.state === 'stopped') return '从程序入口重新打开即可启动'
  const starting = snapshot.components.filter(row => row.state === 'starting' || row.retrying)
  if (starting.length) {
    return starting.map(row => COMPONENT_NAMES[row.id] || row.name).join('、')
  }
  if (snapshot.startup_complete && !snapshot.core_available) {
    const failed = snapshot.components.find(row => ['failed', 'blocked'].includes(row.state))
    return failed ? `${COMPONENT_NAMES[failed.id] || failed.name}未能启动` : '请到本地组件查看运行情况'
  }
  const waiting = snapshot.components.find(row => row.state === 'waiting')
  return waiting ? '正在准备' + (COMPONENT_NAMES[waiting.id] || waiting.name) : '正在完成启动检查'
}

/** startupProgress（启动完成比例）：只使用宿主报告的真实组件计数。 */
export function startupProgress(snapshot: ApplicationSnapshot | null): number {
  if (!snapshot || snapshot.total_components <= 0) return 0
  return Math.max(0, Math.min(100, snapshot.completed_components / snapshot.total_components * 100))
}

/** LocalComponentState（本地组件状态）：用于状态颜色与操作可用性。 */
export type LocalComponentState = 'ready' | 'loading' | 'failed' | 'disabled'

/** LocalComponentRow（组件条目）：展示层只接收指标与选项，不直接调用业务。 */
export interface LocalComponentRow {
  /** id（稳定标识）与 name（组件名称）。 */
  id: string
  name: string
  /** state（状态类别）、status（状态文案）与 available（详情是否可用）。 */
  state: LocalComponentState
  status: string
  available: boolean
  /** retryable（是否允许重试）与 busy（是否正在恢复）。 */
  retryable?: boolean
  busy?: boolean
  /** runtime（推理或执行环境）、residentMemory（常驻内存）与 privateMemory（提交内存）。 */
  runtime: string
  residentMemory?: string
  privateMemory?: string
  /** vram（进程专用显存占用）：没有有效测量时省略。 */
  vram?: string
  /** description（详情说明）、error（异常原因）与 note（指标说明）。 */
  description: string
  error?: string
  note?: string
  /** location（当前运行位置）：没有可调整设备选项时显示。 */
  location?: string
  /** facts（补充指标）：label（名称）与 value（数值）。 */
  facts: Array<{ label: string; value: string }>
  /** device（运行位置选项）：value（草稿）、current（实际位置）、pending（尚未应用）。 */
  device?: {
    value: string
    current: string
    pending?: boolean
    draft?: boolean // draft（未保存设备草稿），不代表待启动生效配置。
    /** options（设备选项）：value（标识）、label（文案）、disabled（不可选）。 */
    options: Array<{ value: string; label: string; disabled?: boolean }>
    /** hint（设备限制说明）。 */
    hint?: string
  }
}

/** LocalComponentsModel（本地组件页面数据）：统一呈现总量、硬件与组件。 */
export interface LocalComponentsModel {
  /** status（整体状态）、state（状态类别）与 sampledAt（采样时间）。 */
  status: string
  state: LocalComponentState
  sampledAt: string
  /** resources（资源摘要）：id（标识）、label（名称）、used（已用）与 total（全部）。 */
  resources: Array<{ id: string; label: string; used?: string; total?: string }>
  /** rows（组件列表）、error（页面异常）与 refreshing（正在刷新）。 */
  rows: LocalComponentRow[]
  error?: string
  refreshing?: boolean
}

/** formatComponentMemory（内存格式）：界面仅使用 MB／GB，保留现有字节换算口径。 */
export function formatComponentMemory(value: number | null | undefined): string | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return undefined
  return value >= 1024 ** 3
    ? `${Number((value / 1024 ** 3).toFixed(2))} GB`
    : `${Number((value / 1024 ** 2).toFixed(1))} MB`
}
